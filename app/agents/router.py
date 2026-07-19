from app.config.settings import get_settings
DEEP_TERMS=("analyse","analyze","strategy","plan","long document","reason","compare","research")
async def route_model_node(state):
    text=state.get("message","").lower()
    return {
        "model_to_use": "groq_reasoning"
        if any(term in text for term in DEEP_TERMS)
        else "groq_fast"
    }
def get_llm(model_choice, *, fallback=False):
    settings=get_settings()
    if not settings.groq_api_key or "your_" in settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    from langchain_groq import ChatGroq
    model = settings.groq_fallback_model if fallback else (
        settings.groq_reasoning_model
        if model_choice == "groq_reasoning"
        else settings.groq_fast_model
    )
    return ChatGroq(
        model=model,
        api_key=settings.groq_api_key,
        temperature=.3,
        timeout=45,
        max_retries=1,
        max_tokens=settings.groq_max_tokens,
    )


def get_model_name(model_choice, *, fallback=False):
    settings = get_settings()
    if fallback:
        return settings.groq_fallback_model
    return (
        settings.groq_reasoning_model
        if model_choice == "groq_reasoning"
        else settings.groq_fast_model
    )
