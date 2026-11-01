def test_gspread_client_uses_retrying_http_client(monkeypatch):
    import gspread

    from plan_cuidado.adapters import google_auth, net

    seen = {}

    def fake_authorize(creds, http_client=None, **kwargs):
        seen["http_client"] = http_client
        return "client"

    monkeypatch.setattr(gspread, "authorize", fake_authorize)
    assert google_auth.gspread_client(object()) == "client"
    assert seen["http_client"] is net.RetryingHTTPClient
