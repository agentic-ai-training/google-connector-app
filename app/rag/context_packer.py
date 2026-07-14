def pack_context(documents: list[dict], max_tokens: int = 3000) -> str:
    remaining = max_tokens * 4
    chunks = []
    for doc in sorted(documents, key=lambda d: d.get("score", 0), reverse=True):
        chunk = f"[{doc.get('source','unknown')}] {doc.get('content','')}\n"
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    return "\n".join(chunks)
