import datetime
import json


async def _embed(embedder, text):
    return await embedder.aembed_query((text or " ")[:6000])


async def persist_tool_result(name, args, result, pool, embedder):
    if not pool or not embedder or result is None:
        return
    if name in {"search_gmail", "get_gmail_message"}:
        rows = result if isinstance(result, list) else [result]
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            received = (
                datetime.datetime.fromisoformat(row["received_at"])
                if row.get("received_at") else None
            )
            vector = await _embed(
                embedder, f"{row.get('subject', '')} {row.get('body_plain', '')}"
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO gmail_messages
                       (id,thread_id,sender,sender_name,recipients,subject,body_plain,
                        body_html,labels,has_attachments,attachment_names,received_at,
                        is_read,is_starred,snippet,embedding)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                       ON CONFLICT(id) DO UPDATE SET
                        subject=excluded.subject,body_plain=excluded.body_plain,
                        body_html=excluded.body_html,labels=excluded.labels,
                        embedding=excluded.embedding,synced_at=now()""",
                    row["id"], row.get("thread_id"), row.get("sender"),
                    row.get("sender_name"), row.get("recipients", []),
                    row.get("subject"), row.get("body_plain"), row.get("body_html"),
                    row.get("labels", []), row.get("has_attachments", False),
                    row.get("attachment_names", []), received,
                    row.get("is_read", False), row.get("is_starred", False),
                    row.get("snippet"), vector,
                )
    elif name in {"list_calendar_events", "get_calendar_event",
                  "create_calendar_event", "update_calendar_event"}:
        rows = result if isinstance(result, list) else [result]
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            start = row.get("start", {})
            end = row.get("end", {})
            start_time = _google_time(start)
            end_time = _google_time(end)
            vector = await _embed(
                embedder, f"{row.get('summary', '')} {row.get('description', '')}"
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO calendar_events
                       (id,title,description,location,start_time,end_time,is_all_day,
                        attendees,organizer_email,meet_link,status,recurrence,embedding)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13)
                       ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                        description=excluded.description,start_time=excluded.start_time,
                        end_time=excluded.end_time,embedding=excluded.embedding,
                        synced_at=now()""",
                    row["id"], row.get("summary"), row.get("description"),
                    row.get("location"), start_time, end_time, "date" in start,
                    json.dumps(row.get("attendees", [])),
                    row.get("organizer", {}).get("email"), row.get("hangoutLink"),
                    row.get("status"), row.get("recurrence"), vector,
                )
    elif name in {"search_drive", "get_drive_file", "upload_drive_file"}:
        rows = result if isinstance(result, list) else [result]
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = row.get("content") or ""
            metadata = row.get("metadata") or row
            if not metadata.get("id"):
                continue
            vector = await _embed(
                embedder, f"{metadata.get('name', '')} {content}"
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO drive_documents
                       (id,name,mime_type,content,parent_folder,web_view_link,embedding)
                       VALUES($1,$2,$3,$4,$5,$6,$7)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                        mime_type=excluded.mime_type,content=excluded.content,
                        web_view_link=excluded.web_view_link,
                        embedding=excluded.embedding,synced_at=now()""",
                    metadata["id"], metadata.get("name"), metadata.get("mimeType"),
                    content, (metadata.get("parents") or [None])[0],
                    metadata.get("webViewLink"), vector,
                )
    elif name == "read_google_doc" and isinstance(result, str):
        document_id = args.get("document_id")
        vector = await _embed(embedder, result)
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO drive_documents(id,mime_type,content,embedding)
                   VALUES($1,'application/vnd.google-apps.document',$2,$3)
                   ON CONFLICT(id) DO UPDATE SET content=excluded.content,
                    embedding=excluded.embedding,synced_at=now()""",
                document_id, result, vector,
            )
    elif name in {"search_contacts", "get_contact"}:
        rows = result if isinstance(result, list) else [result]
        for item in rows:
            person = item.get("person", item) if isinstance(item, dict) else {}
            if not person.get("resourceName"):
                continue
            display_name = (person.get("names") or [{}])[0].get("displayName")
            emails = [value.get("value") for value in person.get("emailAddresses", [])]
            phones = [value.get("value") for value in person.get("phoneNumbers", [])]
            organization = (person.get("organizations") or [{}])[0]
            notes = "\n".join(
                value.get("value", "") for value in person.get("biographies", [])
            )
            photo_url = (person.get("photos") or [{}])[0].get("url")
            vector = await _embed(
                embedder,
                " ".join(filter(None, [display_name, *emails, organization.get("name"),
                                         organization.get("title"), notes])),
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO contacts
                       (id,display_name,emails,phone_numbers,organization,job_title,
                        notes,photo_url,embedding)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                       ON CONFLICT(id) DO UPDATE SET
                        display_name=excluded.display_name,emails=excluded.emails,
                        phone_numbers=excluded.phone_numbers,
                        organization=excluded.organization,
                        job_title=excluded.job_title,notes=excluded.notes,
                        photo_url=excluded.photo_url,embedding=excluded.embedding,
                        synced_at=now()""",
                    person["resourceName"], display_name, emails, phones,
                    organization.get("name"), organization.get("title"), notes,
                    photo_url, vector,
                )


def _google_time(value):
    if value.get("dateTime"):
        return datetime.datetime.fromisoformat(value["dateTime"])
    if value.get("date"):
        date = datetime.date.fromisoformat(value["date"])
        return datetime.datetime.combine(date, datetime.time(), datetime.timezone.utc)
    return None
