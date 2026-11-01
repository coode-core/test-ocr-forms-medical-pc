"""Google Drive workspace: discovery by appProperties, folders, moves and config files."""

from __future__ import annotations

import io
import json

from plan_cuidado.adapters.net import GOOGLE_NUM_RETRIES, execute, is_network_error
from plan_cuidado.config import Workspace
from plan_cuidado.ports import DriveFile, TransientError

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
PDF_MIME = "application/pdf"
APP_KEY = "plan_cuidado"
ROOT_NAME = "Plan de Cuidado"
TRACKER_NAME = "Tracker Plan de Cuidado"
CHILDREN = {
    "inbox_id": ("01_inbox", "inbox"),
    "processed_id": ("02_processed", "processed"),
    "failed_id": ("03_failed", "failed"),
    "config_id": ("config", "config"),
    "sandbox_id": ("sandbox", "sandbox"),
}


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


class DriveApi:
    """Thin wrapper around googleapiclient's Drive v3 service."""

    def __init__(self, service):
        self.files = service.files()

    def _first(self, query: str) -> str | None:
        # Oldest first: if a retried create ever leaves a duplicate, the original is always used.
        result = execute(
            self.files.list(q=query, fields="files(id)", pageSize=1, spaces="drive", orderBy="createdTime")
        )
        found = result.get("files", [])
        return found[0]["id"] if found else None

    def find_by_app_property(self, value: str, parent_id: str | None = None) -> str | None:
        query = f"appProperties has {{ key='{APP_KEY}' and value='{_q(value)}' }} and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        return self._first(query)

    def find_by_name(self, name: str, parent_id: str, mime: str | None = None) -> str | None:
        query = f"name='{_q(name)}' and '{parent_id}' in parents and trashed=false"
        if mime:
            query += f" and mimeType='{mime}'"
        return self._first(query)

    def _create(self, body: dict, media=None) -> str:
        return execute(self.files.create(body=body, media_body=media, fields="id"))["id"]

    def create_folder(self, name: str, parent_id: str | None = None, app_property: str | None = None) -> str:
        body: dict = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        if app_property:
            body["appProperties"] = {APP_KEY: app_property}
        return self._create(body)

    def create_spreadsheet(self, name: str, parent_id: str, app_property: str | None = None) -> str:
        body: dict = {"name": name, "mimeType": SHEET_MIME, "parents": [parent_id]}
        if app_property:
            body["appProperties"] = {APP_KEY: app_property}
        return self._create(body)

    def list_children(self, parent_id: str, mime: str | None = None) -> list[dict]:
        query = f"'{parent_id}' in parents and trashed=false"
        if mime:
            query += f" and mimeType='{mime}'"
        out, token = [], None
        while True:
            result = execute(
                self.files.list(
                    q=query, fields="nextPageToken, files(id, name, webViewLink)", pageToken=token, orderBy="createdTime"
                )
            )
            out.extend(result.get("files", []))
            token = result.get("nextPageToken")
            if not token:
                return out

    def move(self, file_id: str, new_parent_id: str) -> None:
        current = execute(self.files.get(fileId=file_id, fields="parents")).get("parents", [])
        execute(
            self.files.update(fileId=file_id, addParents=new_parent_id, removeParents=",".join(current), fields="id")
        )

    def _media(self, text: str, mime: str):
        from googleapiclient.http import MediaIoBaseUpload

        return MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype=mime, resumable=False)

    def upload_text(self, name: str, parent_id: str, text: str, mime: str) -> str:
        return self._create({"name": name, "parents": [parent_id]}, self._media(text, mime))

    def update_text(self, file_id: str, text: str, mime: str) -> None:
        execute(self.files.update(fileId=file_id, media_body=self._media(text, mime), fields="id"))

    def download(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, self.files.get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=GOOGLE_NUM_RETRIES)
        return buf.getvalue()


def ensure_workspace(api) -> Workspace:
    """Find the workspace (marked with appProperties) or create the missing parts."""
    root = api.find_by_app_property("root") or api.create_folder(ROOT_NAME, None, "root")
    ids = {"root_id": root}
    for field, (name, marker) in CHILDREN.items():
        ids[field] = api.find_by_app_property(marker, root) or api.create_folder(name, root, marker)
    ids["tracker_id"] = api.find_by_app_property("tracker", root) or api.create_spreadsheet(
        TRACKER_NAME, root, "tracker"
    )
    return Workspace(**ids)


class GoogleDriveStore:
    def __init__(self, api, workspace: Workspace):
        self.api = api
        self.ws = workspace
        self._month_folders: dict[str, str] = {}

    def list_inbox(self) -> list[DriveFile]:
        return [
            DriveFile(f["id"], f["name"], f.get("webViewLink", ""))
            for f in self.api.list_children(self.ws.inbox_id, PDF_MIME)
        ]

    def download(self, file_id: str) -> bytes:
        try:
            return self.api.download(file_id)
        except Exception as exc:
            if is_network_error(exc):
                raise TransientError(f"Google Drive no responde: {type(exc).__name__}") from exc
            raise

    def list_done(self) -> list[DriveFile]:
        """PDFs already handled: 02_processed/<month>/ and 03_failed/."""
        out: list[DriveFile] = []
        for folder in self.api.list_children(self.ws.processed_id, FOLDER_MIME):
            for f in self.api.list_children(folder["id"], PDF_MIME):
                out.append(DriveFile(f["id"], f["name"], f.get("webViewLink", ""), f"02_processed/{folder['name']}"))
        for f in self.api.list_children(self.ws.failed_id, PDF_MIME):
            out.append(DriveFile(f["id"], f["name"], f.get("webViewLink", ""), "03_failed"))
        return out

    def move_to_inbox(self, file_id: str) -> None:
        self.api.move(file_id, self.ws.inbox_id)

    def move_to_processed(self, file_id: str, month: str) -> None:
        folder = self._month_folders.get(month)
        if folder is None:
            folder = self.api.find_by_name(month, self.ws.processed_id, FOLDER_MIME) or self.api.create_folder(
                month, self.ws.processed_id
            )
            self._month_folders[month] = folder
        self.api.move(file_id, folder)

    def move_to_failed(self, file_id: str, error: str) -> None:
        name = next((f["name"] for f in self.api.list_children(self.ws.inbox_id) if f["id"] == file_id), file_id)
        self.api.move(file_id, self.ws.failed_id)
        self.api.upload_text(f"{name}.error.txt", self.ws.failed_id, error, "text/plain")

    def read_config_json(self, name: str) -> dict | None:
        file_id = self.api.find_by_name(name, self.ws.config_id)
        if not file_id:
            return None
        text = self.api.download(file_id).decode("utf-8-sig")
        return json.loads(text) if text.strip() else None

    def write_config_json(self, name: str, data: dict) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        file_id = self.api.find_by_name(name, self.ws.config_id)
        if file_id:
            self.api.update_text(file_id, text, "application/json")
        else:
            self.api.upload_text(name, self.ws.config_id, text, "application/json")
