from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle, os

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/drive.labels.readonly",
]

def get_creds():
    creds = None
    if os.path.exists("token.pkl"):
        with open("token.pkl", "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("token.pkl", "wb") as f:
            pickle.dump(creds, f)
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
    print("\nAll 6 APIs verified. Phase 1 complete.")


if __name__ == "__main__":
    main()
