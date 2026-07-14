from app.config.settings import get_settings
DEEP_TERMS=("analyse","analyze","strategy","plan","long document","reason","compare","research")
async def route_model_node(state):
    text=state.get("message","").lower()
    return {"model_to_use":"deepseek" if any(t in text for t in DEEP_TERMS) else "groq"}
def get_llm(model_choice):
    settings=get_settings()
    if model_choice=="deepseek":
        if not settings.deepseek_api_key or "your_" in settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="deepseek-reasoner",api_key=settings.deepseek_api_key,base_url="https://api.deepseek.com")
    if not settings.groq_api_key or "your_" in settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    from langchain_groq import ChatGroq
    return ChatGroq(model="llama-3.3-70b-versatile",api_key=settings.groq_api_key,temperature=.3)
