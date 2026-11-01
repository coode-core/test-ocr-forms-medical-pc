"""Google Forms API: create the sandbox clone and read its responses."""

from __future__ import annotations

from plan_cuidado.adapters.net import execute


class GoogleFormsApi:
    def __init__(self, service, drive_api=None, folder_id: str | None = None):
        self.forms = service.forms()
        self.drive_api = drive_api
        self.folder_id = folder_id

    def create_form(self, title: str, requests: list[dict]) -> tuple[str, str]:
        form = execute(self.forms.create(body={"info": {"title": title, "documentTitle": title}}))
        form_id = form["formId"]
        execute(self.forms.batchUpdate(formId=form_id, body={"requests": requests}))
        publish = getattr(self.forms, "setPublishSettings", None)
        if publish is not None:  # forms created by the API may start unpublished
            execute(
                publish(
                    formId=form_id,
                    body={"publishSettings": {"publishState": {"isPublished": True, "isAcceptingResponses": True}}},
                )
            )
        if self.drive_api and self.folder_id:
            self.drive_api.move(form_id, self.folder_id)
        form = execute(self.forms.get(formId=form_id))
        return form_id, form["responderUri"]

    def question_titles(self, form_id: str) -> dict[str, str]:
        form = execute(self.forms.get(formId=form_id))
        out = {}
        for item in form.get("items", []):
            question = item.get("questionItem", {}).get("question")
            if question:
                out[question["questionId"]] = item.get("title", "")
        return out

    def list_responses(self, form_id: str) -> list[dict]:
        responses, token = [], None
        while True:
            result = execute(self.forms.responses().list(formId=form_id, pageToken=token))
            responses.extend(result.get("responses", []))
            token = result.get("nextPageToken")
            if not token:
                break
        return sorted(responses, key=lambda r: r.get("createTime", ""))
