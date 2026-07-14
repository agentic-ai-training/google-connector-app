from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from app.agents.state import AgentState
from app.agents.router import route_model_node, get_llm
from app.rag.context_packer import pack_context
from app.rag.retriever import hybrid_retrieve

SERVICES=("gmail","calendar","drive","docs","sheets","tasks","chat","contacts")
async def retrieve_context_node(state):
    try: docs=await hybrid_retrieve(state.get("message", ""))
    except Exception: docs=[]
    return {"retrieved_context":pack_context(docs),"tool_results":docs}
async def supervisor_node(state):
    text=state.get("message","").lower()
    aliases={"email":"gmail","mail":"gmail","event":"calendar","document":"docs","spreadsheet":"sheets","contact":"contacts"}
    service=next((s for s in SERVICES if s in text),None) or next((v for k,v in aliases.items() if k in text),"gmail")
    return {"service":service}
def route_to_subagent(state): return state.get("service","error")
async def service_node(state):
    try:
        prompt="You are a Google Workspace assistant. Use the supplied context, be concise, and never claim an action occurred unless a tool result confirms it."
        context=state.get("retrieved_context","")
        llm=get_llm(state.get("model_to_use","groq"))
        result=await llm.ainvoke([SystemMessage(content=f"{prompt}\nContext:\n{context}"),HumanMessage(content=state.get("message",""))])
        return {"output":result.content,"task_complete":True}
    except Exception as exc:
        return {
            "error": str(exc),
            "output": f"I couldn't complete that request: {exc}",
            "task_complete": False,
        }
async def error_handler_node(state): return {"output":f"I couldn't complete that request: {state.get('error','unknown error')}","task_complete":False}
async def respond_node(state): return {"output":state.get("output","")}

def build_agent_graph(pool=None):
    graph=StateGraph(AgentState)
    graph.add_node("route_model",route_model_node); graph.add_node("retrieve_context",retrieve_context_node); graph.add_node("supervisor",supervisor_node)
    for name in SERVICES: graph.add_node(f"{name}_agent",service_node)
    graph.add_node("error_handler",error_handler_node); graph.add_node("respond",respond_node)
    graph.set_entry_point("route_model"); graph.add_edge("route_model","retrieve_context"); graph.add_edge("retrieve_context","supervisor")
    graph.add_conditional_edges("supervisor",route_to_subagent,{**{s:f"{s}_agent" for s in SERVICES},"error":"error_handler"})
    for name in SERVICES: graph.add_edge(f"{name}_agent","respond")
    graph.add_edge("error_handler","respond"); graph.add_edge("respond",END)
    return graph.compile()

agent_graph=build_agent_graph()
