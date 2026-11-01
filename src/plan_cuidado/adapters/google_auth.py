"""Google OAuth (desktop app, loopback redirect) and API client factories."""

from __future__ import annotations

from plan_cuidado.config import GOOGLE_SCOPES, AppPaths


class AuthRequired(RuntimeError):
    pass


def load_credentials(paths: AppPaths, interactive: bool = False):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if paths.token.exists():
        creds = Credentials.from_authorized_user_file(str(paths.token), GOOGLE_SCOPES)
    if creds and creds.valid and set(GOOGLE_SCOPES) <= set(creds.scopes or []):
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            paths.token.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError:
            creds = None
    if not interactive:
        raise AuthRequired("La sesión de Google no está configurada o expiró. Ejecute `pc setup`.")
    if not paths.credentials.exists():
        raise AuthRequired(f"No se encontró credentials.json en {paths.credentials}")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(paths.credentials), GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    paths.token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def drive_service(creds):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def forms_service(creds):
    from googleapiclient.discovery import build

    return build("forms", "v1", credentials=creds, cache_discovery=False)


def gspread_client(creds):
    import gspread

    from plan_cuidado.adapters.net import RetryingHTTPClient

    return gspread.authorize(creds, http_client=RetryingHTTPClient)
