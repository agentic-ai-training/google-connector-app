import base64
import hashlib
import io
from datetime import datetime, timedelta
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from langchain_core.tools import tool
from app.db import google_clients as g
from app.tools.base import instrument_tool, tool_run_id


def _request_id(action, *values):
    canonical = "|".join([str(tool_run_id.get() or "legacy"), action,
                           *(str(value) for value in values)])
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _existing_drive_resource(request_id):
    escaped = request_id.replace("'", "\\'")
    files = g.drive_service.files().list(
        q=("appProperties has { key='agentRequestId' and "
           f"value='{escaped}' }} and trashed=false"),
        pageSize=1, fields="files(id,name,mimeType,webViewLink)",
    ).execute().get("files", [])
    return files[0] if files else None

def _headers(msg): return {h["name"].lower():h["value"] for h in msg.get("payload",{}).get("headers",[])}
def _decode(data):
    return base64.urlsafe_b64decode(data + "===").decode(errors="replace")


def _parts(payload):
    yield payload
    for part in payload.get("parts", []):
        yield from _parts(part)


def _body(payload, mime="text/plain"):
    values = []
    for part in _parts(payload):
        if part.get("mimeType", "text/plain").startswith(mime):
            data = part.get("body", {}).get("data")
            if data:
                values.append(_decode(data))
    return "\n".join(values)
def _gmail(msg):
    h = _headers(msg)
    sender_name, sender = parseaddr(h.get("from", ""))
    recipients = [address for _, address in getaddresses([
        h.get("to", ""), h.get("cc", ""), h.get("bcc", "")
    ]) if address]
    try:
        received = parsedate_to_datetime(h.get("date", "")).isoformat()
    except (TypeError, ValueError):
        received = None
    attachments = [
        part.get("filename") for part in _parts(msg.get("payload", {}))
        if part.get("filename")
    ]
    labels = msg.get("labelIds", [])
    return {
        "id": msg["id"], "thread_id": msg.get("threadId"), "sender": sender,
        "sender_name": sender_name, "recipients": recipients,
        "subject": h.get("subject"), "body_plain": _body(msg.get("payload", {})),
        "body_html": _body(msg.get("payload", {}), "text/html"),
        "labels": labels, "has_attachments": bool(attachments),
        "attachment_names": attachments, "snippet": msg.get("snippet"),
        "received_at": received, "is_read": "UNREAD" not in labels,
        "is_starred": "STARRED" in labels,
    }

@tool("search_gmail", description="Google Workspace operation")
def search_gmail(query:str,max_results:int=10,after_date:str|None=None):
    q=f"{query} after:{after_date}" if after_date else query
    ids=g.gmail_service.users().messages().list(userId="me",q=q,maxResults=max_results).execute().get("messages",[])
    return [_gmail(g.gmail_service.users().messages().get(userId="me",id=x["id"],format="full").execute()) for x in ids]


@tool(
    "list_recent_gmail_senders",
    description=(
        "List recent Gmail senders using message metadata only. Use this instead of "
        "search_gmail when the task needs sender names/addresses rather than bodies."
    ),
)
def list_recent_gmail_senders(
    max_results: int = 20,
    query: str = "-in:sent",
    unique: bool = True,
    scan_limit: int = 200,
):
    requested = max(1, min(int(max_results), 100))
    remaining = max(requested, min(int(scan_limit), 500))
    senders = []
    seen = set()
    scanned = 0
    page_token = None
    while len(senders) < requested and scanned < remaining:
        response = g.gmail_service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(100, remaining - scanned),
            pageToken=page_token,
        ).execute()
        message_ids = response.get("messages", [])
        if not message_ids:
            break
        for item in message_ids:
            if scanned >= remaining or len(senders) >= requested:
                break
            message = g.gmail_service.users().messages().get(
                userId="me",
                id=item["id"],
                format="metadata",
                metadataHeaders=["From", "Date"],
                fields="id,threadId,payload/headers",
            ).execute()
            scanned += 1
            headers = _headers(message)
            sender_name, sender_email = parseaddr(headers.get("from", ""))
            normalized = sender_email.strip().lower()
            if not normalized or (unique and normalized in seen):
                continue
            seen.add(normalized)
            try:
                received = parsedate_to_datetime(headers.get("date", "")).isoformat()
            except (TypeError, ValueError):
                received = None
            senders.append({
                "message_id": message["id"],
                "thread_id": message.get("threadId"),
                "sender_name": sender_name.strip() or normalized,
                "sender_email": normalized,
                "received_at": received,
            })
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return {
        "senders": senders,
        "requested": requested,
        "returned": len(senders),
        "unique": unique,
        "scanned": scanned,
    }


@tool(
    "count_gmail_senders",
    description=(
        "Count unique Gmail senders from metadata only, optionally within a Gmail "
        "category and the current local day. This never retrieves message bodies."
    ),
)
def count_gmail_senders(
    category: str | None = None,
    period: str = "all",
    timezone: str | None = None,
    max_messages: int = 500,
):
    allowed_categories = {"primary", "promotions", "social", "updates", "forums"}
    normalized_category = category.casefold().strip() if category else None
    if normalized_category and normalized_category not in allowed_categories:
        raise ValueError(f"Unsupported Gmail category: {category}")
    if period not in {"all", "today"}:
        raise ValueError("period must be 'all' or 'today'")

    query_parts = ["-in:sent"]
    if normalized_category:
        query_parts.append(f"category:{normalized_category}")
    local_date = None
    if period == "today":
        if not timezone:
            raise ValueError("timezone is required when period is today")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone}") from exc
        now = datetime.now(zone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        query_parts.extend([
            f"after:{int(start.timestamp())}",
            f"before:{int(end.timestamp())}",
        ])
        local_date = start.date().isoformat()

    limit = max(1, min(int(max_messages), 2_000))
    seen = set()
    scanned = 0
    page_token = None
    exhausted = False
    while scanned < limit:
        response = g.gmail_service.users().messages().list(
            userId="me",
            q=" ".join(query_parts),
            maxResults=min(100, limit - scanned),
            pageToken=page_token,
        ).execute()
        message_ids = response.get("messages", [])
        if not message_ids:
            exhausted = True
            break
        for item in message_ids:
            if scanned >= limit:
                break
            message = g.gmail_service.users().messages().get(
                userId="me",
                id=item["id"],
                format="metadata",
                metadataHeaders=["From"],
                fields="id,payload/headers",
            ).execute()
            scanned += 1
            _, sender_email = parseaddr(_headers(message).get("from", ""))
            normalized = sender_email.strip().casefold()
            if normalized:
                seen.add(normalized)
        page_token = response.get("nextPageToken")
        if not page_token:
            exhausted = True
            break

    return {
        "unique_sender_count": len(seen),
        "messages_scanned": scanned,
        "category": normalized_category,
        "period": period,
        "timezone": timezone,
        "local_date": local_date,
        "complete": exhausted,
        "scan_limit": limit,
    }


@tool("get_gmail_message", description="Google Workspace operation")
def get_gmail_message(message_id:str): return _gmail(g.gmail_service.users().messages().get(userId="me",id=message_id,format="full").execute())
@tool("send_gmail", description="Google Workspace operation")
def send_gmail(to:str,subject:str,body:str,cc:str|None=None):
    request_id = _request_id('gmail',to,subject,body,cc)
    existing = g.gmail_service.users().messages().list(
        userId="me", q=f"rfc822msgid:{request_id}@google-connector-agent", maxResults=1
    ).execute().get("messages", [])
    if existing:
        return g.gmail_service.users().messages().get(
            userId="me", id=existing[0]["id"], format="minimal"
        ).execute()
    msg=MIMEText(body); msg["to"]=to; msg["subject"]=subject
    msg["Message-ID"] = f"<{request_id}@google-connector-agent>"
    if cc: msg["cc"]=cc
    return g.gmail_service.users().messages().send(userId="me",body={"raw":base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
@tool("reply_gmail", description="Google Workspace operation")
def reply_gmail(thread_id:str,message_id:str,body:str):
    old=g.gmail_service.users().messages().get(userId="me",id=message_id,format="metadata").execute(); h=_headers(old)
    msg=MIMEText(body); msg["to"]=h.get("reply-to",h.get("from","")); msg["subject"]="Re: "+h.get("subject",""); msg["In-Reply-To"]=h.get("message-id","")
    return g.gmail_service.users().messages().send(userId="me",body={"raw":base64.urlsafe_b64encode(msg.as_bytes()).decode(),"threadId":thread_id}).execute()
@tool("label_gmail", description="Google Workspace operation")
def label_gmail(message_id:str,add_labels:list[str]|None=None,remove_labels:list[str]|None=None): return g.gmail_service.users().messages().modify(userId="me",id=message_id,body={"addLabelIds":add_labels or [],"removeLabelIds":remove_labels or []}).execute()
@tool("trash_gmail", description="Google Workspace operation")
def trash_gmail(message_id:str): return g.gmail_service.users().messages().trash(userId="me",id=message_id).execute()
@tool("list_gmail_threads", description="Google Workspace operation")
def list_gmail_threads(query:str="",max_results:int=10): return g.gmail_service.users().threads().list(userId="me",q=query,maxResults=max_results).execute().get("threads",[])

@tool("list_calendar_events", description="Google Workspace operation")
def list_calendar_events(start_date:str,end_date:str,calendar_id:str="primary"): return g.calendar_service.events().list(calendarId=calendar_id,timeMin=start_date,timeMax=end_date,singleEvents=True,orderBy="startTime").execute().get("items",[])
@tool("get_calendar_event", description="Google Workspace operation")
def get_calendar_event(event_id:str,calendar_id:str="primary"): return g.calendar_service.events().get(calendarId=calendar_id,eventId=event_id).execute()
@tool("create_calendar_event", description="Google Workspace operation")
def create_calendar_event(title:str,start_datetime:str,end_datetime:str,attendees:list[str]|None=None,description:str|None=None,add_meet:bool=True,calendar_id:str="primary",timezone:str|None=None):
    request_id = _request_id('calendar',calendar_id,title,start_datetime,end_datetime,attendees,description,timezone)
    existing = g.calendar_service.events().list(
        calendarId=calendar_id, privateExtendedProperty=f"agentRequestId={request_id}",
        maxResults=1, singleEvents=True,
    ).execute().get("items", [])
    if existing:
        return existing[0]
    body={"summary":title,"start":{"dateTime":start_datetime,**({"timeZone":timezone} if timezone else {})},"end":{"dateTime":end_datetime,**({"timeZone":timezone} if timezone else {})},"description":description,"attendees":[{"email":x} for x in attendees or []],"extendedProperties":{"private":{"agentRequestId":request_id}}}
    if add_meet: body["conferenceData"]={"createRequest":{"requestId":f"agent-{request_id}"}}
    return g.calendar_service.events().insert(calendarId=calendar_id,body=body,conferenceDataVersion=1,sendUpdates="all").execute()
@tool("update_calendar_event", description="Google Workspace operation")
def update_calendar_event(event_id:str,title:str|None=None,start_datetime:str|None=None,end_datetime:str|None=None,description:str|None=None,calendar_id:str="primary",timezone:str|None=None,attendees:list[str]|None=None,add_meet:bool|None=None):
    body={};
    if title is not None: body["summary"]=title
    if start_datetime is not None: body["start"]={"dateTime":start_datetime,**({"timeZone":timezone} if timezone else {})}
    if end_datetime is not None: body["end"]={"dateTime":end_datetime,**({"timeZone":timezone} if timezone else {})}
    if description is not None: body["description"]=description
    if attendees is not None: body["attendees"]=[{"email":x} for x in attendees]
    if add_meet: body["conferenceData"]={"createRequest":{"requestId":f"agent-{_request_id('calendar-update',calendar_id,event_id)}"}}
    return g.calendar_service.events().patch(calendarId=calendar_id,eventId=event_id,body=body,conferenceDataVersion=1 if add_meet else 0,sendUpdates="all").execute()
@tool("delete_calendar_event", description="Google Workspace operation")
def delete_calendar_event(event_id:str,calendar_id:str="primary"):
    g.calendar_service.events().delete(
        calendarId=calendar_id, eventId=event_id,
    ).execute()
    return {"eventId": event_id, "calendarId": calendar_id, "deleted": True}
@tool("check_calendar_availability", description="Google Workspace operation")
def check_calendar_availability(start_datetime:str,end_datetime:str,attendee_emails:list[str]|None=None): return g.calendar_service.freebusy().query(body={"timeMin":start_datetime,"timeMax":end_datetime,"items":[{"id":x} for x in attendee_emails or ["primary"]]}).execute()

@tool("search_drive", description="Google Workspace operation")
def search_drive(query:str,mime_type:str|None=None,max_results:int=10):
    q=f"fullText contains '{query.replace(chr(39),chr(92)+chr(39))}' and trashed=false"+(f" and mimeType='{mime_type}'" if mime_type else "")
    return g.drive_service.files().list(q=q,pageSize=max_results,fields="files(id,name,mimeType,webViewLink,modifiedTime,parents)").execute().get("files",[])
@tool("get_drive_file", description="Google Workspace operation")
def get_drive_file(file_id:str):
    metadata = g.drive_service.files().get(fileId=file_id,fields="*").execute()
    mime = metadata.get("mimeType", "")
    content = None
    if mime == "application/vnd.google-apps.document":
        content = g.drive_service.files().export(
            fileId=file_id, mimeType="text/plain"
        ).execute().decode(errors="replace")
    elif mime == "application/vnd.google-apps.spreadsheet":
        content = g.drive_service.files().export(
            fileId=file_id, mimeType="text/csv"
        ).execute().decode(errors="replace")
    elif mime.startswith("text/"):
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buffer, g.drive_service.files().get_media(fileId=file_id)
        )
        done = False
        while not done:
            _, done = downloader.next_chunk()
        content = buffer.getvalue().decode(errors="replace")
    return {"metadata": metadata, "content": content}
@tool("upload_drive_file", description="Google Workspace operation")
def upload_drive_file(file_path:str,parent_folder_id:str|None=None,name:str|None=None): return g.drive_service.files().create(body={"name":name or Path(file_path).name,**({"parents":[parent_folder_id]} if parent_folder_id else {})},media_body=MediaFileUpload(file_path),fields="id,name,webViewLink").execute()
@tool("share_drive_file", description="Google Workspace operation")
def share_drive_file(file_id:str,email:str,role:str="reader",permission_type:str="user"):
    existing=g.drive_service.permissions().list(fileId=file_id,fields="permissions(id,type,role,emailAddress,domain,deleted)").execute().get("permissions",[])
    principal_key="domain" if permission_type=="domain" else "emailAddress"
    for permission in existing:
        if (permission.get("type")==permission_type and permission.get("role")==role
                and permission.get(principal_key)==email and not permission.get("deleted")):
            return permission
    return g.drive_service.permissions().create(fileId=file_id,body={"type":permission_type,"role":role,principal_key:email},sendNotificationEmail=permission_type=="user",fields="id,type,role,emailAddress,domain,deleted").execute()
@tool("move_drive_file", description="Google Workspace operation")
def move_drive_file(file_id:str,new_folder_id:str):
    old=g.drive_service.files().get(fileId=file_id,fields="parents").execute().get("parents",[])
    return g.drive_service.files().update(fileId=file_id,addParents=new_folder_id,removeParents=",".join(old),fields="id,parents").execute()
@tool("trash_drive_file", description="Move a Google Drive file to trash")
def trash_drive_file(file_id:str):
    return g.drive_service.files().update(
        fileId=file_id, body={"trashed": True}, fields="id,name,trashed,webViewLink"
    ).execute()

def _doc_text(doc): return "".join(e.get("textRun",{}).get("content","") for s in doc.get("body",{}).get("content",[]) for e in s.get("paragraph",{}).get("elements",[]))
@tool("read_google_doc", description="Google Workspace operation")
def read_google_doc(document_id:str): return _doc_text(g.docs_service.documents().get(documentId=document_id).execute())
@tool("create_google_doc", description="Google Workspace operation")
def create_google_doc(title:str,content:str|None=None):
    request_id = _request_id("doc", title, content)
    existing = _existing_drive_resource(request_id)
    if existing:
        return {"documentId": existing["id"],
                "link": existing.get("webViewLink") or f"https://docs.google.com/document/d/{existing['id']}/edit"}
    doc=g.docs_service.documents().create(body={"title":title}).execute()
    if content: g.docs_service.documents().batchUpdate(documentId=doc["documentId"],body={"requests":[{"insertText":{"location":{"index":1},"text":content}}]}).execute()
    g.drive_service.files().update(
        fileId=doc["documentId"], body={"appProperties":{"agentRequestId":request_id}},
        fields="id",
    ).execute()
    return {**doc,"link":f"https://docs.google.com/document/d/{doc['documentId']}/edit"}
@tool("append_to_google_doc", description="Google Workspace operation")
def append_to_google_doc(document_id:str,content:str):
    doc=g.docs_service.documents().get(documentId=document_id).execute(); index=doc["body"]["content"][-1]["endIndex"]-1
    return g.docs_service.documents().batchUpdate(documentId=document_id,body={"requests":[{"insertText":{"location":{"index":index},"text":content}}]}).execute()

@tool("read_google_sheet", description="Google Workspace operation")
def read_google_sheet(spreadsheet_id:str,range:str="Sheet1"): return g.sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id,range=range).execute().get("values",[])
@tool("write_google_sheet", description="Google Workspace operation")
def write_google_sheet(spreadsheet_id:str,range:str,values:list[list]): return g.sheets_service.spreadsheets().values().update(spreadsheetId=spreadsheet_id,range=range,valueInputOption="USER_ENTERED",body={"values":values}).execute()
@tool("append_to_google_sheet", description="Google Workspace operation")
def append_to_google_sheet(spreadsheet_id:str,values:list[list],sheet_name:str="Sheet1"): return g.sheets_service.spreadsheets().values().append(spreadsheetId=spreadsheet_id,range=sheet_name,valueInputOption="USER_ENTERED",insertDataOption="INSERT_ROWS",body={"values":values}).execute()
@tool("create_google_sheet", description="Google Workspace operation")
def create_google_sheet(title:str):
    request_id = _request_id("sheet", title)
    existing = _existing_drive_resource(request_id)
    if existing:
        return {"spreadsheetId": existing["id"],
                "spreadsheetUrl": existing.get("webViewLink") or
                f"https://docs.google.com/spreadsheets/d/{existing['id']}/edit"}
    sheet = g.sheets_service.spreadsheets().create(
        body={"properties":{"title":title}}, fields="spreadsheetId,spreadsheetUrl"
    ).execute()
    g.drive_service.files().update(
        fileId=sheet["spreadsheetId"],
        body={"appProperties":{"agentRequestId":request_id}}, fields="id",
    ).execute()
    return sheet

@tool("list_tasks", description="Google Workspace operation")
def list_tasks(tasklist_id:str="@default",show_completed:bool=False): return g.tasks_service.tasks().list(tasklist=tasklist_id,showCompleted=show_completed,showHidden=show_completed).execute().get("items",[])
@tool("create_task", description="Google Workspace operation")
def create_task(title:str,notes:str|None=None,due_date:str|None=None,tasklist_id:str="@default"): return g.tasks_service.tasks().insert(tasklist=tasklist_id,body={"title":title,"notes":notes,"due":due_date}).execute()
@tool("complete_task", description="Google Workspace operation")
def complete_task(task_id:str,tasklist_id:str="@default"): return g.tasks_service.tasks().patch(tasklist=tasklist_id,task=task_id,body={"status":"completed"}).execute()

@tool("search_contacts", description="Google Workspace operation")
def search_contacts(query:str,max_results:int=10): return g.people_service.people().searchContacts(query=query,readMask="names,emailAddresses,phoneNumbers,organizations,biographies,photos",pageSize=max_results).execute().get("results",[])
@tool("get_contact", description="Google Workspace operation")
def get_contact(email:str): return g.people_service.people().searchContacts(query=email,readMask="names,emailAddresses,phoneNumbers,organizations,biographies,photos",pageSize=10).execute().get("results",[])
@tool("list_chat_spaces", description="Google Workspace operation")
def list_chat_spaces(): return g.chat_service.spaces().list().execute().get("spaces",[])
@tool("send_chat_message", description="Google Workspace operation")
def send_chat_message(space_id:str,text:str): return g.chat_service.spaces().messages().create(parent=space_id,body={"text":text},requestId=_request_id("chat",space_id,text)).execute()

def _meet_space_name(meeting_code_or_name: str) -> str:
    return (meeting_code_or_name if meeting_code_or_name.startswith("spaces/")
            else f"spaces/{meeting_code_or_name}")

@tool("create_meet_space", description="Create an instant Google Meet space and return its joining URL and meeting code")
def create_meet_space():
    return g.meet_service.spaces().create(body={}).execute()

@tool("get_meet_space", description="Get a Google Meet space by meeting code or spaces/... resource name")
def get_meet_space(meeting_code_or_name: str):
    return g.meet_service.spaces().get(
        name=_meet_space_name(meeting_code_or_name)
    ).execute()

@tool("list_meet_conferences", description="List recent Google Meet conference records")
def list_meet_conferences(max_results: int = 10):
    return g.meet_service.conferenceRecords().list(
        pageSize=max_results
    ).execute().get("conferenceRecords", [])

@tool("list_meet_participants", description="List participants for a Google Meet conferenceRecords/... resource")
def list_meet_participants(conference_record: str, max_results: int = 100):
    parent = (conference_record if conference_record.startswith("conferenceRecords/")
              else f"conferenceRecords/{conference_record}")
    return g.meet_service.conferenceRecords().participants().list(
        parent=parent, pageSize=max_results
    ).execute().get("participants", [])


_TOOL_NAMES = (
    "search_gmail", "list_recent_gmail_senders", "count_gmail_senders",
    "get_gmail_message", "send_gmail", "reply_gmail",
    "label_gmail", "trash_gmail", "list_gmail_threads",
    "list_calendar_events", "get_calendar_event", "create_calendar_event",
    "update_calendar_event", "delete_calendar_event", "check_calendar_availability",
    "search_drive", "get_drive_file", "upload_drive_file", "share_drive_file",
    "move_drive_file", "read_google_doc", "create_google_doc",
    "trash_drive_file",
    "append_to_google_doc", "read_google_sheet", "write_google_sheet",
    "append_to_google_sheet", "create_google_sheet", "list_tasks", "create_task",
    "complete_task", "search_contacts", "get_contact", "list_chat_spaces",
    "send_chat_message", "create_meet_space", "get_meet_space",
    "list_meet_conferences", "list_meet_participants",
)


def registered_tool_names() -> set[str]:
    """Return the authoritative public tool identifiers for validation/docs."""
    return set(_TOOL_NAMES)


for _name in _TOOL_NAMES:
    globals()[_name] = instrument_tool(globals()[_name])
