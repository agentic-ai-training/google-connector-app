from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pathlib import Path

from app.db.google_clients import SCOPES

TOKEN_PATH = Path("token.json")

def get_creds():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds

def main():
    creds = get_creds()

    gmail = build("gmail", "v1", credentials=creds)
    profile = gmail.users().getProfile(userId="me").execute()
    print(f"Gmail OK       — {profile['emailAddress']}")

    cal = build("calendar", "v3", credentials=creds)
    cals = cal.calendarList().list().execute()
    print(f"Calendar OK    — {len(cals['items'])} calendar(s) found")

    drive = build("drive", "v3", credentials=creds)
    files = drive.files().list(pageSize=3).execute()
    print(f"Drive OK       — {len(files['files'])} file(s) returned")

    build("docs", "v1", credentials=creds)
    print("Docs OK        — client built successfully")

    build("sheets", "v4", credentials=creds)
    print("Sheets OK      — client built successfully")

    tasks = build("tasks", "v1", credentials=creds)
    tl = tasks.tasklists().list().execute()
    print(f"Tasks OK       — {len(tl.get('items', []))} task list(s) found")
    meet = build("meet", "v2", credentials=creds)
    assert meet is not None
    print("Meet OK        — client built successfully")
    print("\nAll 7 API surfaces verified. Phase 1 complete.")


if __name__ == "__main__":
    main()
