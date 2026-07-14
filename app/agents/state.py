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
