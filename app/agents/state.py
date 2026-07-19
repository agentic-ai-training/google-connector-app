import operator
from typing import Annotated, List, TypedDict
from langchain_core.messages import BaseMessage
class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    message: str
    output: str
    session_id: str
    user_id: str
    current_tool: str
    tool_results: list
    retrieved_context: str
    model_to_use: str
    error: str
    task_complete: bool
    service: str
    services: list[str]
    system_prompt: str
    prompt_id: str | None
    assignment_id: str | None
    rag_decision: dict
    run_id: str
    step_id: str
    forced_service: str
    operational_context: str
    risk_level: str
    allow_small_fallback: bool
