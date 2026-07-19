import base64
import json
import re
from email.mime.text import MIMEText
from urllib.parse import quote

import httpx

from app.config.settings import get_settings
from app.db import google_clients as google


_PRIVATE_PATTERN = re.compile(
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?i:api[_ -]?key|authorization|refresh[_ -]?token)\s*[:=]"
)


def proposal_markdown(proposal: dict) -> str:
    """Build a public artifact from curated fields only; evidence rows are excluded."""
    values = {
        "title": proposal["title"],
        "proposal_key": proposal["proposal_key"],
        "content_hash": proposal["content_hash"],
        "summary": proposal["sanitized_summary"],
        "exact_diff": proposal.get("exact_diff") or "No textual diff supplied.",
        "expected_impact": proposal.get("expected_impact") or {},
        "privacy_report": proposal.get("privacy_report") or {},
        "security_report": proposal.get("security_report") or {},
        "rollback_plan": proposal.get("rollback_plan") or {},
    }
    document = (
        f"# {values['title']}\n\n"
        f"Proposal: `{values['proposal_key']}`  \n"
        f"Frozen hash: `{values['content_hash']}`\n\n"
        f"## Sanitized summary\n\n{values['summary']}\n\n"
        f"## Exact candidate diff\n\n```diff\n{values['exact_diff']}\n```\n\n"
        f"## Expected impact\n\n```json\n{json.dumps(values['expected_impact'], indent=2)}\n```\n\n"
        f"## Privacy and security\n\n```json\n{json.dumps({'privacy': values['privacy_report'], 'security': values['security_report']}, indent=2)}\n```\n\n"
        f"## Rollback\n\n```json\n{json.dumps(values['rollback_plan'], indent=2)}\n```\n"
    )
    if _PRIVATE_PATTERN.search(document):
        raise ValueError("Public proposal contains an email address or secret-like field")
    return document


async def publish_github_draft(proposal: dict) -> dict:
    settings = get_settings()
    if not settings.github_proposal_token:
        raise RuntimeError("GITHUB_PROPOSAL_TOKEN is not configured")
    repository = settings.github_proposal_repository.strip("/")
    if repository.count("/") != 1:
        raise RuntimeError("GITHUB_PROPOSAL_REPOSITORY must be owner/repository")
    markdown = proposal_markdown(proposal)
    branch = f"governed/{proposal['proposal_key']}-{proposal['content_hash'][:8]}"
    path = f".improvement-proposals/{proposal['proposal_key']}.md"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_proposal_token}",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    base_url = f"https://api.github.com/repos/{repository}"
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        repo_response = await client.get(base_url)
        repo_response.raise_for_status()
        default_branch = repo_response.json()["default_branch"]
        ref_response = await client.get(
            f"{base_url}/git/ref/heads/{quote(default_branch, safe='')}"
        )
        ref_response.raise_for_status()
        base_sha = ref_response.json()["object"]["sha"]
        created_ref = await client.post(
            f"{base_url}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        if created_ref.status_code not in {201, 422}:
            created_ref.raise_for_status()
        content = base64.b64encode(markdown.encode()).decode()
        file_response = await client.put(
            f"{base_url}/contents/{path}",
            json={
                "message": f"docs: governed proposal {proposal['proposal_key']}",
                "content": content,
                "branch": branch,
            },
        )
        file_response.raise_for_status()
        pull_response = await client.post(
            f"{base_url}/pulls",
            json={
                "title": f"Governed proposal: {proposal['title']}",
                "head": branch, "base": default_branch, "draft": True,
                "body": (
                    "Human-reviewed, sanitized proposal. This draft does not contain "
                    "private run evidence and must not be auto-merged."
                ),
            },
        )
        pull_response.raise_for_status()
    payload = pull_response.json()
    return {"number": payload["number"], "url": payload["html_url"], "branch": branch}


def send_proposal_email(proposal: dict, recipient: str) -> dict:
    markdown = proposal_markdown(proposal)
    message = MIMEText(markdown, "plain", "utf-8")
    message["to"] = recipient
    message["subject"] = f"Review governed proposal: {proposal['title']}"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    result = google.gmail_service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    return {"message_id": result.get("id")}
