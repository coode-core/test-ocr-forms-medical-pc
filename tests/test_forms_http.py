import httpx
import pytest

from plan_cuidado.adapters.forms_http import HttpFormSubmitter, fetch_form_html, form_requires_login, is_confirmation_page

OK_HTML = '<div class="freebirdFormviewerViewResponseConfirmationMessage">Se registró tu respuesta.</div>'


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_submit_posts_form_encoded_payload_with_repeats():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, text=OK_HTML)

    s = HttpFormSubmitter(client(handler))
    r = s.submit("https://docs.google.com/forms/d/e/X/formResponse", [("entry.1", "A B"), ("entry.2", "X"), ("entry.2", "Z")], "")
    assert r.ok
    assert seen["method"] == "POST"
    assert seen["body"] == "entry.1=A+B&entry.2=X&entry.2=Z"


def test_submit_rejects_non_confirmation_pages():
    s = HttpFormSubmitter(client(lambda req: httpx.Response(200, text="<html>form again</html>")))
    r = s.submit("https://x/formResponse", [], "")
    assert not r.ok and "confirmación" in r.detail


def test_submit_http_error():
    s = HttpFormSubmitter(client(lambda req: httpx.Response(400, text="bad")))
    r = s.submit("https://x/formResponse", [], "")
    assert not r.ok and "400" in r.detail


def test_confirmation_markers():
    assert is_confirmation_page(OK_HTML)
    assert is_confirmation_page("<p>Your response has been recorded.</p>")
    assert not is_confirmation_page("<p>hola</p>")


def test_fetch_form_html_and_login_detection(form_html):
    c = client(lambda req: httpx.Response(200, text=form_html))
    html, final_url = fetch_form_html("https://docs.google.com/forms/d/e/X/viewform", c)
    assert "FB_PUBLIC_LOAD_DATA_" in html
    assert not form_requires_login(final_url, html)
    assert form_requires_login("https://accounts.google.com/v3/signin/identifier?continue=x", "<html></html>")


# --------------------------------------------------------------------------- retries


@pytest.fixture
def no_sleep(monkeypatch):
    from plan_cuidado.adapters import net

    monkeypatch.setattr(net, "_sleep", lambda s: None)


def sequence_handler(outcomes):
    calls = []

    def handler(request):
        calls.append(request.method)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handler, calls


def test_new_client_retries_connection_attempts():
    from plan_cuidado.adapters.forms_http import CONNECT_RETRIES, new_client

    with new_client() as c:
        assert c._transport._pool._retries == CONNECT_RETRIES >= 3


def test_fetch_form_retries_transient_errors(form_html, no_sleep):
    handler, calls = sequence_handler(
        [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.Response(503), httpx.Response(200, text=form_html)]
    )
    html, _ = fetch_form_html("https://docs.google.com/forms/d/e/X/viewform", client(handler))
    assert "FB_PUBLIC_LOAD_DATA_" in html
    assert len(calls) == 4


def test_fetch_form_gives_up_and_raises(no_sleep):
    handler, calls = sequence_handler([httpx.ConnectTimeout("t")])
    with pytest.raises(httpx.ConnectTimeout):
        fetch_form_html("https://docs.google.com/forms/d/e/X/viewform", client(handler))
    assert len(calls) > 1


def test_submit_retries_only_connect_phase_errors(no_sleep):
    handler, calls = sequence_handler(
        [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.Response(200, text=OK_HTML)]
    )
    r = HttpFormSubmitter(client(handler)).submit("https://x/formResponse", [("entry.1", "A")], "")
    assert r.ok and len(calls) == 3


def test_submit_never_retries_after_request_may_have_been_sent(no_sleep):
    for exc in (httpx.ReadTimeout("t"), httpx.RemoteProtocolError("eof"), httpx.ReadError("ssl eof")):
        handler, calls = sequence_handler([exc, httpx.Response(200, text=OK_HTML)])
        r = HttpFormSubmitter(client(handler)).submit("https://x/formResponse", [("entry.1", "A")], "")
        assert not r.ok and "red" in r.detail
        assert len(calls) == 1, exc


def test_submit_does_not_retry_server_errors(no_sleep):
    handler, calls = sequence_handler([httpx.Response(503), httpx.Response(200, text=OK_HTML)])
    r = HttpFormSubmitter(client(handler)).submit("https://x/formResponse", [], "")
    assert not r.ok and len(calls) == 1
