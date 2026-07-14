import os
import pickle
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks", "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts", "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly", "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.external_request", "https://www.googleapis.com/auth/drive.labels.readonly",
]
def _load_creds():
    if not os.path.exists("token.pkl"):
        return None
    with open("token.pkl", "rb") as fh:
        creds = pickle.load(fh)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open("token.pkl", "wb") as fh:
            pickle.dump(creds, fh)
    return creds

_creds = _load_creds()
class _MissingCredentialsService:
    def __init__(self, api):
        self.api = api

    def __getattr__(self, name):
        raise RuntimeError(
            f"Google credentials are unavailable; cannot use {self.api} API"
        )


def _service(name, version):
    if _creds is None:
        return _MissingCredentialsService(name)
    return build(name, version, credentials=_creds, cache_discovery=False)

gmail_service = _service("gmail", "v1")
calendar_service = _service("calendar", "v3")
drive_service = _service("drive", "v3")
docs_service = _service("docs", "v1")
sheets_service = _service("sheets", "v4")
tasks_service = _service("tasks", "v1")
chat_service = _service("chat", "v1")
people_service = _service("people", "v1")
drive_activity_service = _service("driveactivity", "v2")
meet_service = _service("meet", "v2")
script_service = _service("script", "v1")
drive_labels_service = _service("drivelabels", "v2")
