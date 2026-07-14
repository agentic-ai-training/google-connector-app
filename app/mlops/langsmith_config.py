import os
from app.config.settings import get_settings
def configure_langsmith():
    s=get_settings(); os.environ["LANGCHAIN_TRACING_V2"]=s.langchain_tracing_v2; os.environ["LANGCHAIN_PROJECT"]=s.langchain_project
    if s.langchain_api_key: os.environ["LANGCHAIN_API_KEY"]=s.langchain_api_key
