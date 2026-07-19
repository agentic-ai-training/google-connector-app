import re


PROMPT_INJECTION_LINE = re.compile(
    r"(?i)((ignore|override|disregard).{0,40}(instruction|system|developer)|"
    r"(reveal|print|return).{0,30}(secret|token|password|system prompt)|"
    r"(send|upload|share).{0,40}(all files|credentials|tokens))"
)


def sanitize_untrusted_content(value: str) -> tuple[str, int]:
    safe = []
    removed = 0
    for line in str(value).replace("<", "‹").replace(">", "›").splitlines():
        if PROMPT_INJECTION_LINE.search(line):
            safe.append("[potential prompt-injection instruction removed]")
            removed += 1
        else:
            safe.append(line)
    return "\n".join(safe), removed


def pack_context(documents: list[dict], max_tokens: int = 3000) -> str:
    remaining = max_tokens * 4
    chunks = []
    for doc in sorted(documents, key=lambda d: d.get("score", 0), reverse=True):
        content, removed = sanitize_untrusted_content(doc.get("content", ""))
        warning = f" [removed_instructions={removed}]" if removed else ""
        chunk = f"[{doc.get('source','unknown')}{warning}] {content}\n"
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    return "\n".join(chunks)
