import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import tiktoken


@dataclass
class Chunk:
    content: str
    index: int
    heading: str | None = None
    parent_id: str | None = None
    parent_content: str | None = None
    parent_heading: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True)
class ChunkingPolicy:
    """Versionable chunk boundaries; structured records intentionally ignore text size."""

    name: str = "source-aware-character-v2"
    target_tokens: int | None = None
    overlap_tokens: int = 0
    target_characters: int = 1800
    overlap_characters: int = 180
    sheet_rows: int = 25


DEFAULT_POLICY = ChunkingPolicy()
EXPERIMENT_POLICIES = {
    size: ChunkingPolicy(
        name=f"source-aware-token-{size}-v1",
        target_tokens=size,
        overlap_tokens=max(32, size // 10),
    )
    for size in (256, 512, 768, 1024)
}
_TOKENIZER = tiktoken.get_encoding("cl100k_base")


def _character_windows(text: str, size: int, overlap: int) -> list[str]:
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


def _token_windows(text: str, size: int, overlap: int) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    tokens = _TOKENIZER.encode(normalized)
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("Token window requires size>0 and 0<=overlap<size")
    output = []
    start = 0
    while start < len(tokens):
        end = min(len(tokens), start + size)
        output.append(_TOKENIZER.decode(tokens[start:end]).strip())
        if end >= len(tokens):
            break
        start = end - overlap
    return output


def _windows(text: str, policy: ChunkingPolicy) -> list[str]:
    if policy.target_tokens is not None:
        return _token_windows(text, policy.target_tokens, policy.overlap_tokens)
    return _character_windows(
        text, policy.target_characters, policy.overlap_characters,
    )


def _parent_policy(policy: ChunkingPolicy) -> ChunkingPolicy:
    """Create a larger, non-overlapping generation-context boundary."""
    if policy.target_tokens is not None:
        return ChunkingPolicy(
            name=f"{policy.name}-parent",
            target_tokens=min(2048, max(1024, policy.target_tokens * 3)),
            overlap_tokens=0,
        )
    return ChunkingPolicy(
        name=f"{policy.name}-parent",
        target_characters=max(5000, policy.target_characters * 3),
        overlap_characters=0,
    )


def token_count(text: str) -> int:
    """Return the reproducible proxy-token count used by offline policy experiments."""
    return len(_TOKENIZER.encode(text))


def clean_email_body(body: str) -> str:
    body = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.split(
        r"(?im)^\s*(?:On .+ wrote:|From:\s|[-_]{2,}\s*Original Message\s*[-_]{2,})",
        body,
    )[0]
    body = re.split(r"(?im)^\s*(?:--\s*$|Sent from my )", body)[0]
    return " ".join(body.split())


def chunk_gmail(item: dict, policy: ChunkingPolicy = DEFAULT_POLICY) -> list[Chunk]:
    body = clean_email_body(item.get("body_plain") or item.get("body") or "")
    prefix = (
        f"Subject: {item.get('subject', '')}\n"
        f"From: {item.get('sender', '')}\n"
        f"Received: {item.get('received_at', '')}\n"
    )
    material = body or item.get("snippet") or item.get("subject") or ""
    output = []
    thread_id = item.get("thread_id")
    message_id = item.get("id")
    base_parent = str(
        f"{thread_id}:{message_id}" if thread_id and message_id
        else (thread_id or message_id or "message")
    )
    for parent_index, parent_body in enumerate(
        _windows(material, _parent_policy(policy)) or [material]
    ):
        parent_id = base_parent if parent_index == 0 else f"{base_parent}:{parent_index}"
        parent_content = prefix + parent_body
        for part in _windows(parent_body, policy) or [parent_body]:
            if not part.strip():
                continue
            output.append(Chunk(
                content=prefix + part, index=len(output), parent_id=parent_id,
                parent_content=parent_content, parent_heading=item.get("subject"),
                metadata={
                    "thread_id": item.get("thread_id"), "sender": item.get("sender"),
                    "received_at": item.get("received_at"),
                    "labels": item.get("labels", []), "parent_index": parent_index,
                },
            ))
    return output


def chunk_document(item: dict, policy: ChunkingPolicy = DEFAULT_POLICY) -> list[Chunk]:
    content = item.get("content") or item.get("text") or ""
    title = item.get("name") or item.get("title") or "Document"
    sections = re.split(r"(?m)(?=^#{1,6}\s+)", content)
    output = []
    for section_index, section in enumerate(sections):
        if not section.strip():
            continue
        first = section.strip().splitlines()[0]
        heading = first.lstrip("#").strip() if first.startswith("#") else title
        for parent_index, parent_part in enumerate(
            _windows(section, _parent_policy(policy))
        ):
            parent_id = f"section-{section_index}-parent-{parent_index}"
            parent_content = f"Title: {title}\nSection: {heading}\n{parent_part}"
            for part in _windows(parent_part, policy):
                output.append(Chunk(
                    content=f"Title: {title}\nSection: {heading}\n{part}",
                    index=len(output), heading=heading, parent_id=parent_id,
                    parent_content=parent_content, parent_heading=heading,
                    metadata={"mime_type": item.get("mime_type"),
                              "web_view_link": item.get("web_view_link"),
                              "modified_time": (item.get("modified_time") or
                                                item.get("modifiedTime")),
                              "section_index": section_index,
                              "parent_index": parent_index},
                ))
    return output


def chunk_sheet(item: dict, policy: ChunkingPolicy = DEFAULT_POLICY) -> list[Chunk]:
    values = item.get("values") or item.get("rows") or []
    if not values:
        return []
    headers = [str(value) for value in values[0]]
    output = []
    for offset in range(1, len(values), policy.sheet_rows):
        rows = values[offset:offset + policy.sheet_rows]
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


def chunk_pdf(item: dict, policy: ChunkingPolicy = DEFAULT_POLICY) -> list[Chunk]:
    """Chunk already-extracted PDF layout without flattening unrelated columns."""
    pages = item.get("pages") or []
    if not pages:
        return [Chunk(
            content=chunk.content, index=chunk.index, heading=chunk.heading,
            parent_id=chunk.parent_id,
            metadata={**chunk.metadata, "page_number": None,
                      "layout_available": False, "ocr": bool(item.get("ocr"))},
        ) for chunk in chunk_document(item, policy)]
    output = []
    for page_index, page in enumerate(pages, 1):
        page_number = page.get("page_number", page_index)
        blocks = page.get("blocks") or ([{"text": page.get("text", "")}] if page.get("text") else [])
        for block in blocks:
            text = block.get("text", "").strip()
            if not text:
                continue
            for part in _windows(text, policy):
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


def chunk_meet_transcript(
    item: dict, policy: ChunkingPolicy = DEFAULT_POLICY,
) -> list[Chunk]:
    turns = item.get("turns") or item.get("speaker_turns") or []
    if not turns:
        return chunk_document(item, policy)
    output = []
    buffer = []
    for turn in turns:
        speaker = turn.get("speaker") or "Unknown speaker"
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        line = f"{speaker}: {text}"
        candidate = "\n".join([*buffer, line])
        boundary_reached = (
            token_count(candidate) > policy.target_tokens
            if policy.target_tokens is not None
            else sum(len(value) for value in buffer) + len(line)
            > policy.target_characters
        )
        if boundary_reached and buffer:
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


def chunks_for_source(
    source_type: str, item: dict, policy: ChunkingPolicy = DEFAULT_POLICY,
) -> list[Chunk]:
    if source_type == "gmail":
        return chunk_gmail(item, policy)
    if source_type in {"drive", "docs"}:
        return chunk_document(item, policy)
    if source_type == "pdf":
        return chunk_pdf(item, policy)
    if source_type == "meet_transcript":
        return chunk_meet_transcript(item, policy)
    if source_type == "sheets":
        return chunk_sheet(item, policy)
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
    return chunk_document(item, policy)
