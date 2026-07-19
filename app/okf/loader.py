import hashlib
import json
import re
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)")


def load_bundle(root: Path) -> tuple[list[dict], list[str]]:
    documents = []
    errors = []
    paths = {path.relative_to(root).as_posix() for path in root.rglob("*.md")}
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        raw = path.read_text(encoding="utf-8")
        match = FRONTMATTER.match(raw)
        if not match:
            errors.append(f"{relative}: missing YAML frontmatter")
            continue
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            errors.append(f"{relative}: invalid YAML: {exc}")
            continue
        if not metadata.get("type"):
            errors.append(f"{relative}: required field 'type' is missing")
        body = match.group(2).strip()
        for target in MARKDOWN_LINK.findall(body):
            if target.startswith(("http://", "https://", "/")):
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved_relative = resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                errors.append(f"{relative}: link escapes bundle: {target}")
                continue
            if resolved_relative not in paths:
                errors.append(f"{relative}: broken link: {target}")
        content_hash = hashlib.sha256(raw.encode()).hexdigest()
        documents.append({
            "id": relative,
            "concept_type": metadata.get("type", "invalid"),
            "title": metadata.get("title") or path.stem.replace("-", " ").title(),
            "description": metadata.get("description"),
            "resource": metadata.get("resource"),
            "tags": metadata.get("tags") or [],
            "owner": metadata.get("owner"),
            "version": str(metadata.get("version", "1")),
            "visibility": metadata.get("visibility", "public"),
            "content": body,
            "content_hash": content_hash,
            "metadata": metadata,
        })
    return documents, errors


def section_chunks(document: dict) -> list[dict]:
    sections = []
    heading = document["title"]
    current = []
    for line in document["content"].splitlines():
        if line.startswith("#") and current:
            sections.append((heading, "\n".join(current).strip()))
            heading = line.lstrip("#").strip()
            current = [line]
        else:
            if line.startswith("#"):
                heading = line.lstrip("#").strip()
            current.append(line)
    if current:
        sections.append((heading, "\n".join(current).strip()))
    return [
        {
            "heading": title,
            "content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "chunk_index": index,
        }
        for index, (title, content) in enumerate(sections) if content
    ]


async def sync_bundle(pool, root: Path | None = None):
    root = root or Path(__file__).resolve().parents[2] / "knowledge"
    documents, errors = load_bundle(root)
    if errors:
        raise ValueError("Invalid OKF bundle:\n" + "\n".join(errors))
    async with pool.acquire() as conn:
        async with conn.transaction():
            for document in documents:
                await conn.execute(
                    """INSERT INTO okf_documents
                       (id,visibility,concept_type,title,description,resource,tags,
                        owner,version,content,content_hash,metadata,trusted,published_at)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,TRUE,now())
                       ON CONFLICT(id) DO UPDATE SET visibility=excluded.visibility,
                        concept_type=excluded.concept_type,title=excluded.title,
                        description=excluded.description,resource=excluded.resource,
                        tags=excluded.tags,owner=excluded.owner,version=excluded.version,
                        content=excluded.content,content_hash=excluded.content_hash,
                        metadata=excluded.metadata,updated_at=now()""",
                    document["id"], document["visibility"], document["concept_type"],
                    document["title"], document["description"], document["resource"],
                    document["tags"], document["owner"], document["version"],
                    document["content"], document["content_hash"],
                    json.dumps(document["metadata"], default=str),
                )
                await conn.execute("DELETE FROM okf_chunks WHERE document_id=$1", document["id"])
                for chunk in section_chunks(document):
                    await conn.execute(
                        """INSERT INTO okf_chunks
                           (document_id,heading,chunk_index,content,content_hash,
                            chunker_version,metadata)
                           VALUES($1,$2,$3,$4,$5,'okf-structure-v1',$6::jsonb)""",
                        document["id"], chunk["heading"], chunk["chunk_index"],
                        chunk["content"], chunk["content_hash"],
                        json.dumps({"concept_type": document["concept_type"],
                                    "tags": document["tags"]}),
                    )
    return len(documents)
