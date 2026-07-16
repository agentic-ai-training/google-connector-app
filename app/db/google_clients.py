import os
import pickle
import json
from contextvars import ContextVar
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from app.config.settings import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks", "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts", "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly", "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.external_request", "https://www.googleapis.com/auth/drive.labels.readonly",
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
]
def _load_creds():
    settings = get_settings()
    if settings.google_token_json:
        creds = Credentials.from_authorized_user_info(
            json.loads(settings.google_token_json), SCOPES
        )
    elif os.path.exists(settings.google_token_path):
        with open(settings.google_token_path, "rb") as fh:
            creds = pickle.load(fh)
    else:
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not settings.google_token_json:
            with open(settings.google_token_path, "wb") as fh:
                pickle.dump(creds, fh)
    return creds

_creds = _load_creds()
request_google_credentials: ContextVar[Credentials | None] = ContextVar(
    "request_google_credentials", default=None
)
class _MissingCredentialsService:
    def __init__(self, api):
        self.api = api

    def __getattr__(self, name):
        raise RuntimeError(
            f"Google credentials are unavailable; cannot use {self.api} API"
        )


class _UserScopedService:
    def __init__(self, name, version):
        self.name = name
        self.version = version

    def __getattr__(self, attribute):
        credentials = request_google_credentials.get() or _creds
        if credentials is None:
            return getattr(_MissingCredentialsService(self.name), attribute)
        service = build(
            self.name,
            self.version,
            credentials=credentials,
            cache_discovery=False,
        )
        return getattr(service, attribute)


def _service(name, version):
    return _UserScopedService(name, version)

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
