import base64
from email.mime.text import MIMEText
from pathlib import Path
from googleapiclient.http import MediaFileUpload
from langchain_core.tools import tool
from app.db import google_clients as g

def _headers(msg): return {h["name"].lower():h["value"] for h in msg.get("payload",{}).get("headers",[])}
def _body(payload):
    data=payload.get("body",{}).get("data")
    if data:
        return base64.urlsafe_b64decode(data+"===").decode(errors="replace")
    return "\n".join(_body(p) for p in payload.get("parts",[]) if p.get("mimeType","text/plain").startswith("text/plain"))
def _gmail(msg):
    h=_headers(msg); return {"id":msg["id"],"thread_id":msg.get("threadId"),"sender":h.get("from"),"recipients":[h.get("to","")],"subject":h.get("subject"),"body_plain":_body(msg.get("payload",{})),"labels":msg.get("labelIds",[]),"snippet":msg.get("snippet"),"received_at":h.get("date")}

@tool("search_gmail", description="Google Workspace operation")
def search_gmail(query:str,max_results:int=10,after_date:str|None=None):
    q=f"{query} after:{after_date}" if after_date else query
    ids=g.gmail_service.users().messages().list(userId="me",q=q,maxResults=max_results).execute().get("messages",[])
    return [_gmail(g.gmail_service.users().messages().get(userId="me",id=x["id"],format="full").execute()) for x in ids]
@tool("get_gmail_message", description="Google Workspace operation")
def get_gmail_message(message_id:str): return _gmail(g.gmail_service.users().messages().get(userId="me",id=message_id,format="full").execute())
@tool("send_gmail", description="Google Workspace operation")
def send_gmail(to:str,subject:str,body:str,cc:str|None=None):
    msg=MIMEText(body); msg["to"]=to; msg["subject"]=subject
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
def create_calendar_event(title:str,start_datetime:str,end_datetime:str,attendees:list[str]|None=None,description:str|None=None,add_meet:bool=True):
    body={"summary":title,"start":{"dateTime":start_datetime},"end":{"dateTime":end_datetime},"description":description,"attendees":[{"email":x} for x in attendees or []]}
    if add_meet: body["conferenceData"]={"createRequest":{"requestId":f"agent-{abs(hash((title,start_datetime))) }"}}
    return g.calendar_service.events().insert(calendarId="primary",body=body,conferenceDataVersion=1,sendUpdates="all").execute()
@tool("update_calendar_event", description="Google Workspace operation")
def update_calendar_event(event_id:str,title:str|None=None,start_datetime:str|None=None,end_datetime:str|None=None,description:str|None=None):
    body={};
    if title is not None: body["summary"]=title
    if start_datetime is not None: body["start"]={"dateTime":start_datetime}
    if end_datetime is not None: body["end"]={"dateTime":end_datetime}
    if description is not None: body["description"]=description
    return g.calendar_service.events().patch(calendarId="primary",eventId=event_id,body=body).execute()
@tool("delete_calendar_event", description="Google Workspace operation")
def delete_calendar_event(event_id:str,calendar_id:str="primary"): g.calendar_service.events().delete(calendarId=calendar_id,eventId=event_id).execute(); return "Event deleted"
@tool("check_calendar_availability", description="Google Workspace operation")
def check_calendar_availability(start_datetime:str,end_datetime:str,attendee_emails:list[str]|None=None): return g.calendar_service.freebusy().query(body={"timeMin":start_datetime,"timeMax":end_datetime,"items":[{"id":x} for x in attendee_emails or ["primary"]]}).execute()

@tool("search_drive", description="Google Workspace operation")
def search_drive(query:str,mime_type:str|None=None,max_results:int=10):
    q=f"fullText contains '{query.replace(chr(39),chr(92)+chr(39))}' and trashed=false"+(f" and mimeType='{mime_type}'" if mime_type else "")
    return g.drive_service.files().list(q=q,pageSize=max_results,fields="files(id,name,mimeType,webViewLink,modifiedTime,parents)").execute().get("files",[])
@tool("get_drive_file", description="Google Workspace operation")
def get_drive_file(file_id:str): return g.drive_service.files().get(fileId=file_id,fields="*").execute()
@tool("upload_drive_file", description="Google Workspace operation")
def upload_drive_file(file_path:str,parent_folder_id:str|None=None,name:str|None=None): return g.drive_service.files().create(body={"name":name or Path(file_path).name,**({"parents":[parent_folder_id]} if parent_folder_id else {})},media_body=MediaFileUpload(file_path),fields="id,name,webViewLink").execute()
@tool("share_drive_file", description="Google Workspace operation")
def share_drive_file(file_id:str,email:str,role:str="reader"): return g.drive_service.permissions().create(fileId=file_id,body={"type":"user","role":role,"emailAddress":email},sendNotificationEmail=True).execute()
@tool("move_drive_file", description="Google Workspace operation")
def move_drive_file(file_id:str,new_folder_id:str):
    old=g.drive_service.files().get(fileId=file_id,fields="parents").execute().get("parents",[])
    return g.drive_service.files().update(fileId=file_id,addParents=new_folder_id,removeParents=",".join(old),fields="id,parents").execute()

def _doc_text(doc): return "".join(e.get("textRun",{}).get("content","") for s in doc.get("body",{}).get("content",[]) for e in s.get("paragraph",{}).get("elements",[]))
@tool("read_google_doc", description="Google Workspace operation")
def read_google_doc(document_id:str): return _doc_text(g.docs_service.documents().get(documentId=document_id).execute())
@tool("create_google_doc", description="Google Workspace operation")
def create_google_doc(title:str,content:str|None=None):
    doc=g.docs_service.documents().create(body={"title":title}).execute()
    if content: g.docs_service.documents().batchUpdate(documentId=doc["documentId"],body={"requests":[{"insertText":{"location":{"index":1},"text":content}}]}).execute()
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
def create_google_sheet(title:str): return g.sheets_service.spreadsheets().create(body={"properties":{"title":title}},fields="spreadsheetId,spreadsheetUrl").execute()

@tool("list_tasks", description="Google Workspace operation")
def list_tasks(tasklist_id:str="@default",show_completed:bool=False): return g.tasks_service.tasks().list(tasklist=tasklist_id,showCompleted=show_completed,showHidden=show_completed).execute().get("items",[])
@tool("create_task", description="Google Workspace operation")
def create_task(title:str,notes:str|None=None,due_date:str|None=None,tasklist_id:str="@default"): return g.tasks_service.tasks().insert(tasklist=tasklist_id,body={"title":title,"notes":notes,"due":due_date}).execute()
@tool("complete_task", description="Google Workspace operation")
def complete_task(task_id:str,tasklist_id:str="@default"): return g.tasks_service.tasks().patch(tasklist=tasklist_id,task=task_id,body={"status":"completed"}).execute()

@tool("search_contacts", description="Google Workspace operation")
def search_contacts(query:str,max_results:int=10): return g.people_service.people().searchContacts(query=query,readMask="names,emailAddresses,phoneNumbers,organizations,biographies,photos",pageSize=max_results).execute().get("results",[])
@tool("get_contact", description="Google Workspace operation")
def get_contact(email:str): return search_contacts.func(email,10)
@tool("list_chat_spaces", description="Google Workspace operation")
def list_chat_spaces(): return g.chat_service.spaces().list().execute().get("spaces",[])
@tool("send_chat_message", description="Google Workspace operation")
def send_chat_message(space_id:str,text:str): return g.chat_service.spaces().messages().create(parent=space_id,body={"text":text}).execute()
