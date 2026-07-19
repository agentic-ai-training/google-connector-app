import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    content: str
    index: int
    heading: str | None = None
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


def _windows(text: str, size: int = 1800, overlap: int = 180) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind(". ", start, end), text.rfind("\n", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def clean_email_body(body: str) -> str:
    body = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.split(
        r"(?im)^\s*(?:On .+ wrote:|From:\s|[-_]{2,}\s*Original Message\s*[-_]{2,})",
        body,
    )[0]
    body = re.split(r"(?im)^\s*(?:--\s*$|Sent from my )", body)[0]
    return " ".join(body.split())


def chunk_gmail(item: dict) -> list[Chunk]:
    body = clean_email_body(item.get("body_plain") or item.get("body") or "")
    prefix = (
        f"Subject: {item.get('subject', '')}\n"
        f"From: {item.get('sender', '')}\n"
        f"Received: {item.get('received_at', '')}\n"
    )
    parts = _windows(body) or [item.get("snippet") or item.get("subject") or ""]
    return [
        Chunk(
            content=prefix + part, index=index, parent_id=item.get("thread_id"),
            metadata={
                "thread_id": item.get("thread_id"), "sender": item.get("sender"),
                "received_at": item.get("received_at"), "labels": item.get("labels", []),
            },
        )
        for index, part in enumerate(parts) if part.strip()
    ]


def chunk_document(item: dict) -> list[Chunk]:
    content = item.get("content") or item.get("text") or ""
    title = item.get("name") or item.get("title") or "Document"
    sections = re.split(r"(?m)(?=^#{1,6}\s+)", content)
    output = []
    for section in sections:
        if not section.strip():
            continue
        first = section.strip().splitlines()[0]
        heading = first.lstrip("#").strip() if first.startswith("#") else title
        for part in _windows(section):
            output.append(Chunk(
                content=f"Title: {title}\nSection: {heading}\n{part}",
                index=len(output), heading=heading,
                metadata={"mime_type": item.get("mime_type"),
                          "web_view_link": item.get("web_view_link")},
            ))
    return output


def chunk_sheet(item: dict) -> list[Chunk]:
    values = item.get("values") or item.get("rows") or []
    if not values:
        return []
    headers = [str(value) for value in values[0]]
    output = []
    for offset in range(1, len(values), 25):
        rows = values[offset:offset + 25]
        lines = [" | ".join(headers)] + [" | ".join(map(str, row)) for row in rows]
        output.append(Chunk(
            content="\n".join(lines), index=len(output),
            heading=item.get("sheet_name", "Sheet1"),
            metadata={"range_start": offset + 1, "range_end": offset + len(rows)},
        ))
    return output


def chunk_chat(item: dict) -> list[Chunk]:
    text = item.get("text") or ""
    return [Chunk(
        content=(f"Sender: {item.get('sender_email', '')}\n"
                 f"Time: {item.get('created_at', '')}\n{text}"),
        index=0, parent_id=item.get("thread_id"),
        metadata={"space_id": item.get("space_id"), "thread_id": item.get("thread_id")},
    )] if text else []


def chunk_pdf(item: dict) -> list[Chunk]:
    """Chunk already-extracted PDF layout without flattening unrelated columns."""
    pages = item.get("pages") or []
    if not pages:
        return [Chunk(
            content=chunk.content, index=chunk.index, heading=chunk.heading,
            parent_id=chunk.parent_id,
            metadata={**chunk.metadata, "page_number": None,
                      "layout_available": False, "ocr": bool(item.get("ocr"))},
        ) for chunk in chunk_document(item)]
    output = []
    for page_index, page in enumerate(pages, 1):
        page_number = page.get("page_number", page_index)
        blocks = page.get("blocks") or ([{"text": page.get("text", "")}] if page.get("text") else [])
        for block in blocks:
            text = block.get("text", "").strip()
            if not text:
                continue
            for part in _windows(text):
                output.append(Chunk(
                    content=f"Page {page_number}\n{part}", index=len(output),
                    heading=block.get("heading"),
                    metadata={"page_number": page_number, "bbox": block.get("bbox"),
                              "column": block.get("column"),
                              "ocr": bool(block.get("ocr", page.get("ocr", False))),
                              "layout_available": True},
                ))
        for table_index, table in enumerate(page.get("tables") or []):
            rows = table.get("rows") or []
            if rows:
                output.append(Chunk(
                    content="\n".join(" | ".join(map(str, row)) for row in rows),
                    index=len(output), heading=f"Table {table_index + 1}",
                    metadata={"page_number": page_number, "bbox": table.get("bbox"),
                              "content_type": "table", "layout_available": True},
                ))
    return output


def chunk_meet_transcript(item: dict) -> list[Chunk]:
    turns = item.get("turns") or item.get("speaker_turns") or []
    if not turns:
        return chunk_document(item)
    output = []
    buffer = []
    for turn in turns:
        speaker = turn.get("speaker") or "Unknown speaker"
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        line = f"{speaker}: {text}"
        if sum(len(value) for value in buffer) + len(line) > 1800 and buffer:
            output.append(Chunk(
                content="\n".join(buffer), index=len(output),
                parent_id=item.get("conference_id"),
                metadata={"conference_id": item.get("conference_id"),
                          "content_type": "transcript"},
            ))
            buffer = []
        buffer.append(line)
    if buffer:
        output.append(Chunk(
            content="\n".join(buffer), index=len(output),
            parent_id=item.get("conference_id"),
            metadata={"conference_id": item.get("conference_id"),
                      "content_type": "transcript"},
        ))
    return output


def chunk_structured(item: dict, fields: list[str]) -> list[Chunk]:
    content = "\n".join(f"{field}: {item.get(field)}" for field in fields
                        if item.get(field) not in (None, "", []))
    return [Chunk(content=content, index=0, metadata=item)] if content else []


def chunks_for_source(source_type: str, item: dict) -> list[Chunk]:
    if source_type == "gmail":
        return chunk_gmail(item)
    if source_type in {"drive", "docs"}:
        return chunk_document(item)
    if source_type == "pdf":
        return chunk_pdf(item)
    if source_type == "meet_transcript":
        return chunk_meet_transcript(item)
    if source_type == "sheets":
        return chunk_sheet(item)
    if source_type == "chat":
        return chunk_chat(item)
    if source_type in {"calendar", "meet"}:
        return chunk_structured(
            item, ["title", "start_time", "end_time", "attendees", "meet_link", "status"],
        )
    if source_type == "contacts":
        return chunk_structured(item, ["display_name", "emails", "organization", "job_title"])
    if source_type == "tasks":
        return chunk_structured(item, ["title", "notes", "status", "due_date"])
    return chunk_document(item)
