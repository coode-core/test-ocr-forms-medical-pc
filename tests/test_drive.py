import json

from plan_cuidado.adapters.drive import FOLDER_MIME, GoogleDriveStore, ensure_workspace


class FakeDriveApi:
    def __init__(self):
        self.files = {}  # id -> dict(name, parent, mime, app, content)
        self.creates = 0

    def _new(self, name, parent, mime, app=None, content=b""):
        self.creates += 1
        fid = f"id{len(self.files) + 1}"
        self.files[fid] = dict(name=name, parent=parent, mime=mime, app=app, content=content)
        return fid

    def find_by_app_property(self, value, parent_id=None):
        for fid, f in self.files.items():
            if f["app"] == value and (parent_id is None or f["parent"] == parent_id):
                return fid
        return None

    def find_by_name(self, name, parent_id, mime=None):
        for fid, f in self.files.items():
            if f["name"] == name and f["parent"] == parent_id and (mime is None or f["mime"] == mime):
                return fid
        return None

    def create_folder(self, name, parent_id=None, app_property=None):
        return self._new(name, parent_id, FOLDER_MIME, app_property)

    def create_spreadsheet(self, name, parent_id, app_property=None):
        return self._new(name, parent_id, "application/vnd.google-apps.spreadsheet", app_property)

    def list_children(self, parent_id, mime=None):
        return [
            {"id": fid, "name": f["name"], "webViewLink": f"https://drive/{fid}"}
            for fid, f in self.files.items()
            if f["parent"] == parent_id and (mime is None or f["mime"] == mime)
        ]

    def move(self, file_id, new_parent_id):
        self.files[file_id]["parent"] = new_parent_id

    def upload_text(self, name, parent_id, text, mime):
        return self._new(name, parent_id, mime, content=text.encode("utf-8"))

    def update_text(self, file_id, text, mime):
        self.files[file_id]["content"] = text.encode("utf-8")

    def download(self, file_id):
        return self.files[file_id]["content"]


def test_ensure_workspace_creates_then_reuses():
    api = FakeDriveApi()
    ws = ensure_workspace(api)
    created = api.creates
    assert created == 7  # root + 5 folders + tracker
    assert api.files[ws.root_id]["app"] == "root"
    assert api.files[ws.inbox_id]["name"] == "01_inbox"
    assert api.files[ws.inbox_id]["parent"] == ws.root_id
    # second PC / second run: everything is discovered, nothing created
    assert ensure_workspace(api) == ws
    assert api.creates == created


def test_ensure_workspace_repairs_missing_child():
    api = FakeDriveApi()
    ws = ensure_workspace(api)
    del api.files[ws.sandbox_id]
    ws2 = ensure_workspace(api)
    assert ws2.root_id == ws.root_id and ws2.sandbox_id != ws.sandbox_id


def test_store_moves_and_config(tmp_path):
    api = FakeDriveApi()
    ws = ensure_workspace(api)
    pdf = api._new("p.pdf", ws.inbox_id, "application/pdf", content=b"%PDF")
    store = GoogleDriveStore(api, ws)
    (f,) = store.list_inbox()
    assert f.id == pdf and f.link == f"https://drive/{pdf}"
    store.move_to_processed(pdf, "2026-08")
    month = api.files[pdf]["parent"]
    assert api.files[month]["name"] == "2026-08" and api.files[month]["parent"] == ws.processed_id
    pdf2 = api._new("q.pdf", ws.inbox_id, "application/pdf")
    store.move_to_processed(pdf2, "2026-08")
    assert api.files[pdf2]["parent"] == month  # month folder reused

    pdf3 = api._new("r.pdf", ws.inbox_id, "application/pdf")
    store.move_to_failed(pdf3, "boom")
    assert api.files[pdf3]["parent"] == ws.failed_id
    err = api.find_by_name("r.pdf.error.txt", ws.failed_id)
    assert api.files[err]["content"].decode("utf-8") == "boom"
    assert pdf3 in api.files  # never deleted

    assert store.read_config_json("static_data.json") is None
    store.write_config_json("static_data.json", {"valores": {"a": "ñ"}})
    store.write_config_json("static_data.json", {"valores": {"a": "é"}})
    assert store.read_config_json("static_data.json") == {"valores": {"a": "é"}}
    assert len([f for f in api.files.values() if f["name"] == "static_data.json"]) == 1


def test_read_config_tolerates_utf8_bom():
    api = FakeDriveApi()
    ws = ensure_workspace(api)
    api._new("static_data.json", ws.config_id, "application/json", content="﻿{}".encode("utf-8"))
    assert GoogleDriveStore(api, ws).read_config_json("static_data.json") == {}
    assert json.dumps({}) == "{}"


# --------------------------------------------------------------------------- DriveApi retries / idempotency


class RecordingRequest:
    def __init__(self, log, kwargs, result):
        self.log, self.kwargs, self.result = log, kwargs, result

    def execute(self, **execute_kwargs):
        self.log.append((self.kwargs, execute_kwargs))
        return self.result


class RecordingFiles:
    def __init__(self):
        self.log = []

    def list(self, **kwargs):
        return RecordingRequest(self.log, kwargs, {"files": [{"id": "F1"}]})

    def create(self, **kwargs):
        return RecordingRequest(self.log, kwargs, {"id": "NEW"})

    def get(self, **kwargs):
        return RecordingRequest(self.log, kwargs, {"parents": ["P0"]})

    def update(self, **kwargs):
        return RecordingRequest(self.log, kwargs, {"id": "U"})


class RecordingService:
    def __init__(self):
        self._files = RecordingFiles()

    def files(self):
        return self._files


def test_drive_api_retries_every_call_and_discovers_oldest_first():
    from plan_cuidado.adapters import net
    from plan_cuidado.adapters.drive import DriveApi

    service = RecordingService()
    api = DriveApi(service)
    assert api.find_by_app_property("root") == "F1"
    assert api.create_folder("x") == "NEW"
    api.move("F1", "P1")
    log = service.files().log
    assert log and all(ex == {"num_retries": net.GOOGLE_NUM_RETRIES} for _, ex in log)
    # duplicates left by a retried create never change which folder is used
    assert log[0][0]["orderBy"] == "createdTime"


class FlakyDriveApi(FakeDriveApi):
    """Network drops after `fail_after` creations (mid-way through `pc setup`)."""

    def __init__(self, fail_after):
        super().__init__()
        self.fail_after = fail_after

    def _new(self, *args, **kwargs):
        if self.fail_after is not None and self.creates >= self.fail_after:
            raise ConnectionResetError(54, "Connection reset by peer")
        return super()._new(*args, **kwargs)


def test_setup_rerun_after_midway_failure_completes_without_duplicates():
    import pytest

    api = FlakyDriveApi(fail_after=3)  # root + 2 children created, then the network fails
    with pytest.raises(ConnectionResetError):
        ensure_workspace(api)
    assert api.creates == 3

    api.fail_after = None  # user re-runs `pc setup`
    ws = ensure_workspace(api)
    assert api.creates == 7
    assert len(api.files) == 7
    assert len({f["app"] for f in api.files.values()}) == 7  # one of each marker
    assert all(api.files[getattr(ws, f)]["parent"] == ws.root_id for f in ("inbox_id", "sandbox_id", "tracker_id"))
    assert ensure_workspace(api) == ws and api.creates == 7


def test_list_done_and_move_back_to_inbox():
    api = FakeDriveApi()
    ws = ensure_workspace(api)
    store = GoogleDriveStore(api, ws)
    a = api._new("a.pdf", ws.inbox_id, "application/pdf")
    b = api._new("b.pdf", ws.inbox_id, "application/pdf")
    store.move_to_processed(a, "2026-08")
    store.move_to_failed(b, "boom")
    done = {f.name: f for f in store.list_done()}
    assert set(done) == {"a.pdf", "b.pdf"}  # the .error.txt is not a PDF
    assert done["a.pdf"].location == "02_processed/2026-08" and done["b.pdf"].location == "03_failed"
    store.move_to_inbox(a)
    assert api.files[a]["parent"] == ws.inbox_id


def test_download_network_failure_becomes_transient():
    import pytest

    from plan_cuidado.ports import TransientError

    class DownApi(FakeDriveApi):
        def download(self, file_id):
            raise ConnectionResetError(54, "reset")

    api = DownApi()
    store = GoogleDriveStore(api, ensure_workspace(api))
    with pytest.raises(TransientError):
        store.download("x")
