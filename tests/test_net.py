import errno
import socket
import ssl

import httpx
import pytest
import requests

from plan_cuidado.adapters import net


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(net, "_sleep", slept.append)
    return slept


def flaky(failures, exc_factory, result="ok"):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc_factory()
        return result

    return fn, calls


def test_with_retries_recovers_from_transient_errors(no_sleep):
    fn, calls = flaky(2, lambda: ConnectionError("reset"))
    assert net.with_retries(fn, should_retry=net.is_network_error, attempts=4) == "ok"
    assert calls["n"] == 3
    assert len(no_sleep) == 2 and no_sleep[0] < no_sleep[1]  # exponential backoff


def test_with_retries_gives_up_after_attempts():
    fn, calls = flaky(10, lambda: TimeoutError("slow"))
    with pytest.raises(TimeoutError):
        net.with_retries(fn, should_retry=net.is_network_error, attempts=3)
    assert calls["n"] == 3


def test_with_retries_does_not_retry_other_errors():
    fn, calls = flaky(10, lambda: ValueError("bug"))
    with pytest.raises(ValueError):
        net.with_retries(fn, should_retry=net.is_network_error, attempts=4)
    assert calls["n"] == 1


class FakeRequest:
    def __init__(self):
        self.kwargs = None

    def execute(self, **kwargs):
        self.kwargs = kwargs
        return {"ok": True}


def test_execute_uses_central_num_retries():
    req = FakeRequest()
    assert net.execute(req) == {"ok": True}
    assert req.kwargs == {"num_retries": net.GOOGLE_NUM_RETRIES}
    assert net.GOOGLE_NUM_RETRIES >= 3


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"),
        TimeoutError("t"),
        socket.timeout("t"),
        ConnectionResetError(54, "reset"),
        socket.gaierror(8, "nodename nor servname provided"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        requests.exceptions.ConnectionError("x"),
        requests.exceptions.ReadTimeout("x"),
    ],
)
def test_network_errors_are_recognized(exc):
    assert net.is_network_error(exc)


def test_library_specific_network_errors_are_recognized():
    import httplib2
    import openai
    from google.auth.exceptions import TransportError

    assert net.is_network_error(httplib2.ServerNotFoundError("no dns"))
    assert net.is_network_error(TransportError("refresh failed"))
    assert net.is_network_error(openai.APIConnectionError(request=httpx.Request("POST", "https://x")))


def test_wrapped_network_error_is_recognized():
    try:
        try:
            raise ssl.SSLEOFError(8, "eof")
        except ssl.SSLError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert net.is_network_error(outer)


def test_non_network_errors_are_not_recognized():
    assert not net.is_network_error(ValueError("x"))
    assert not net.is_network_error(KeyError("x"))
    assert not net.is_network_error(FileNotFoundError(2, "missing"))
    assert not net.is_network_error(PermissionError(13, "denied"))


def _http_error(status):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    return HttpError(Response({"status": status}), b"{}")


def test_google_server_errors_count_as_transient_but_client_errors_do_not():
    assert net.is_network_error(_http_error(503))
    assert net.is_network_error(_http_error(429))
    assert not net.is_network_error(_http_error(404))
    assert not net.is_network_error(_http_error(403))


# --------------------------------------------------------------------------- gspread client


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload or {"error": {"code": status, "message": "x", "status": "x"}}
        self.text = "x"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def gspread_client(outcomes):
    session = FakeSession(outcomes)
    return net.RetryingHTTPClient(auth=None, session=session), session


def test_gspread_client_retries_connection_errors_and_server_errors():
    client, session = gspread_client(
        [requests.exceptions.SSLError("eof"), FakeResponse(503), FakeResponse(429), FakeResponse(200)]
    )
    response = client.request("get", "https://sheets.googleapis.com/v4/spreadsheets/X/values/A1")
    assert response.status_code == 200
    assert len(session.calls) == 4


def test_gspread_client_does_not_retry_client_errors():
    import gspread

    client, session = gspread_client([FakeResponse(404), FakeResponse(200)])
    with pytest.raises(gspread.exceptions.APIError):
        client.request("get", "https://sheets.googleapis.com/v4/spreadsheets/X")
    assert len(session.calls) == 1


def test_gspread_append_is_not_retried_when_it_may_have_been_applied():
    client, session = gspread_client([requests.exceptions.ReadTimeout("slow"), FakeResponse(200)])
    with pytest.raises(requests.exceptions.ReadTimeout):
        client.request("post", "https://sheets.googleapis.com/v4/spreadsheets/X/values/Tracker:append")
    assert len(session.calls) == 1

    client, session = gspread_client([FakeResponse(503), FakeResponse(200)])
    import gspread

    with pytest.raises(gspread.exceptions.APIError):
        client.request("post", "https://sheets.googleapis.com/v4/spreadsheets/X/values/Tracker:append")
    assert len(session.calls) == 1


def test_gspread_append_is_retried_when_it_never_reached_the_server():
    client, session = gspread_client(
        [requests.exceptions.ConnectTimeout("connect"), FakeResponse(429), FakeResponse(200)]
    )
    response = client.request("post", "https://sheets.googleapis.com/v4/spreadsheets/X/values/Tracker:append")
    assert response.status_code == 200
    assert len(session.calls) == 3
