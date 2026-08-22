from app.config.settings import get_settings
DEEP_TERMS=(
    "analyse", "analyze", "strategy", "plan", "long document", "reason",
    "compare", "research", "essay", "roadmap", "application",
)
async def route_model_node(state):
    contract = state.get("content_contract") or {}
    if contract.get("requested"):
        return {
            "model_to_use": (
                "runtime_reasoning"
                if contract.get("complexity") in {"medium", "high"}
                else "runtime_fast"
            )
        }
    text=state.get("message","").lower()
    return {
        "model_to_use": "runtime_reasoning"
        if any(term in text for term in DEEP_TERMS)
        else "runtime_fast"
    }
def get_llm(
    model_choice, *, fallback=False, max_tokens=None, temperature=.3,
    rate_limiter=None,
):
    settings=get_settings()
    provider = settings.runtime_model_provider.strip().casefold()
    if provider != "gemini":
        raise RuntimeError(f"Unsupported runtime model provider: {provider or 'empty'}")
    if not settings.runtime_api_key or "your_" in settings.runtime_api_key:
        raise RuntimeError("RUNTIME_API_KEY is not configured")
    from langchain_google_genai import ChatGoogleGenerativeAI
    model = settings.runtime_fallback_model if fallback else (
        settings.runtime_reasoning_model
        if model_choice in {"runtime_reasoning", "groq_reasoning"}
        else settings.runtime_fast_model
    )
    return ChatGoogleGenerativeAI(
        model=model,
        api_key=settings.runtime_api_key,
        temperature=temperature,
        request_timeout=45,
        retries=1,
        max_tokens=max_tokens or settings.runtime_max_tokens,
        rate_limiter=rate_limiter,
    )


def get_model_name(model_choice, *, fallback=False):
    settings = get_settings()
    if fallback:
        return settings.runtime_fallback_model
    return (
        settings.runtime_reasoning_model
        if model_choice in {"runtime_reasoning", "groq_reasoning"}
        else settings.runtime_fast_model
    )
