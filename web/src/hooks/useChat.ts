"use client";

import {useEffect,useRef,useState} from "react";

export type Message={role:"user"|"assistant";content:string};
export type CurrentUser={email:string;admin?:boolean;google_connected:boolean;missing_scopes?:string[]};
export type RunStep={id:string;title:string;status:string;risk_level:string;requires_approval:boolean};
export type RunEvent={id:number;event_type:string;phase?:string;message?:string;created_at:string};
export type RunApproval={action_hash:string;action_summary:Record<string,unknown>;expires_at:string;status:string};
export type RunArtifact={id:string;artifact_type:string;external_id:string;url?:string;verification_status:string;cleanup_state:string;safe_to_delete:boolean};
export type AgentRun={
  id:string;status:string;current_phase:string;technical_completion:number;
  functional_completion:number;user_visible_completion:number;side_effect_integrity:number;
  plan?:{objective?:string;rag_mode?:string;services?:string[];estimated_max_tokens?:number};
  heartbeat_at?:string;models_used?:string[];input_tokens:number;output_tokens:number;
  error_category?:string;deployment_version?:string;
  result?:{output?:string};incident_summary?:{breaking_point?:string;primary_cause?:string;error?:string};
  clarification_questions?:string[];
  steps:RunStep[];artifacts:RunArtifact[];recent_events?:RunEvent[];approval?:RunApproval|null;
};
export const API=process.env.NEXT_PUBLIC_API_URL??"http://localhost:8000";

export function getToken(){
  return typeof window==="undefined"?null:localStorage.getItem("agent_token");
}

export function beginGoogleLogin(){
  const returnTo=window.location.origin;
  window.location.assign(`${API}/auth/google/login?return_to=${encodeURIComponent(returnTo)}`);
}

export async function currentUser():Promise<CurrentUser>{
  const token=getToken();
  if(!token)throw new Error("Not signed in");
  const response=await fetch(`${API}/auth/me`,{headers:{Authorization:`Bearer ${token}`}});
  if(!response.ok)throw new Error("Your session has expired");
  return response.json();
}

export async function disconnectGoogle(){
  const token=getToken();
  if(token)await fetch(`${API}/auth/google`,{method:"DELETE",headers:{Authorization:`Bearer ${token}`}});
  localStorage.removeItem("agent_token");
}

export function useChat(sessionId:string){
  const [messages,setMessages]=useState<Message[]>([]);
  const [streaming,setStreaming]=useState(false);
  const [error,setError]=useState("");
  const [currentRun,setCurrentRun]=useState<AgentRun|null>(null);
  const activeRun=useRef<string|null>(null);
  const storageKey=`agent_active_run:${sessionId}`;

  const authHeaders=()=>{
    const token=getToken();
    if(!token)throw new Error("Sign in with Google first");
    return {Authorization:`Bearer ${token}`};
  };

  const loadRun=async(runId:string)=>{
    const response=await fetch(`${API}/runs/${runId}`,{headers:authHeaders()});
    if(!response.ok)throw new Error(`Unable to load run (${response.status})`);
    return response.json() as Promise<AgentRun>;
  };

  const showFinal=(run:AgentRun)=>{
    const output=run.result?.output;
    const incident=run.incident_summary;
    const content=output||[
      run.status==="cancelled"?"The run was cancelled.":"I couldn't complete that request.",
      incident?.breaking_point?`Breaking point: ${incident.breaking_point}`:"",
      incident?.primary_cause?`Cause: ${incident.primary_cause}`:"",
      incident?.error?`Details: ${incident.error}`:"",
    ].filter(Boolean).join("\n");
    setMessages(items=>items.map((item,index)=>
      index===items.length-1?{...item,content}:item));
  };

  const monitor=async(runId:string)=>{
    activeRun.current=runId;
    localStorage.setItem(storageKey,runId);
    while(activeRun.current===runId){
      const run=await loadRun(runId);
      setCurrentRun(run);
      if(["completed","failed","partial","cancelled"].includes(run.status)){
        showFinal(run);activeRun.current=null;localStorage.removeItem(storageKey);
        setStreaming(false);return;
      }
      if(["awaiting_approval","awaiting_clarification"].includes(run.status)){setStreaming(false);return;}
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
  };

  useEffect(()=>{
    if(!sessionId||!getToken())return;
    let disposed=false;
    const restore=async()=>{
      try{
        let runId=localStorage.getItem(storageKey);
        if(!runId){
          const response=await fetch(`${API}/sessions/${encodeURIComponent(sessionId)}/runs`,{
            headers:{Authorization:`Bearer ${getToken()??""}`},
          });
          if(response.ok){
            const data=await response.json() as {runs:Array<{id:string;status:string}>};
            runId=data.runs.find(item=>!["completed","failed","partial","cancelled"].includes(item.status))?.id??null;
          }
        }
        if(!runId||disposed)return;
        const run=await loadRun(runId);
        if(disposed)return;
        setCurrentRun(run);
        if(["queued","running"].includes(run.status)){
          setStreaming(true);void monitor(runId).catch(value=>{
            if(!disposed){setError(value instanceof Error?value.message:"Unable to reconnect to run");setStreaming(false);}
          });
        }else if(["completed","failed","partial","cancelled"].includes(run.status)){
          localStorage.removeItem(storageKey);
        }
      }catch(value){
        if(!disposed)setError(value instanceof Error?value.message:"Unable to restore active run");
      }
    };
    void restore();
    return()=>{disposed=true;activeRun.current=null};
    // The run monitor deliberately restarts only when the durable session changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[sessionId,storageKey]);

  const sendMessage=async(content:string)=>{
    setError("");setStreaming(true);
    setMessages(m=>[...m,{role:"user",content},{role:"assistant",content:""}]);
    try{
      const response=await fetch(`${API}/runs`,{method:"POST",headers:{"Content-Type":"application/json",...authHeaders()},body:JSON.stringify({message:content,session_id:sessionId,idempotency_key:crypto.randomUUID()})});
      if(!response.ok){
        const detail=await response.json().catch(()=>({detail:`Request failed (${response.status})`}));
        throw new Error(detail.detail??`Request failed (${response.status})`);
      }
      const created=await response.json() as {run_id:string};
      localStorage.setItem(storageKey,created.run_id);
      const run=await loadRun(created.run_id);setCurrentRun(run);
      if(!["awaiting_approval","awaiting_clarification"].includes(run.status))await monitor(created.run_id);
      else setStreaming(false);
    }catch(e){setError(e instanceof Error?e.message:"Unknown error");setStreaming(false);}
  };

  const decide=async(approved:boolean)=>{
    if(!currentRun?.approval)return;
    setError("");
    const response=await fetch(`${API}/runs/${currentRun.id}/approve`,{
      method:"POST",headers:{"Content-Type":"application/json",...authHeaders()},
      body:JSON.stringify({approved,action_hash:currentRun.approval.action_hash}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Approval failed");}
    if(approved){setStreaming(true);await monitor(currentRun.id);}
    else{const run=await loadRun(currentRun.id);setCurrentRun(run);showFinal(run);localStorage.removeItem(storageKey);}
  };

  const cancel=async()=>{
    if(!currentRun)return;
    await fetch(`${API}/runs/${currentRun.id}/cancel`,{method:"POST",headers:authHeaders()});
    activeRun.current=null;localStorage.removeItem(storageKey);const run=await loadRun(currentRun.id);setCurrentRun(run);showFinal(run);setStreaming(false);
  };
  const clarify=async(answers:Record<string,string>)=>{
    if(!currentRun)return;
    setError("");
    const response=await fetch(`${API}/runs/${currentRun.id}/clarify`,{
      method:"POST",headers:{"Content-Type":"application/json",...authHeaders()},
      body:JSON.stringify({answers}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Clarification failed");}
    const run=await loadRun(currentRun.id);setCurrentRun(run);
    if(!["awaiting_clarification","awaiting_approval"].includes(run.status)){
      setStreaming(true);await monitor(currentRun.id);
    }
  };
  const resume=async()=>{
    if(!currentRun)return;
    setError("");
    const response=await fetch(`${API}/runs/${currentRun.id}/resume`,{
      method:"POST",headers:{"Content-Type":"application/json",...authHeaders()},
      body:JSON.stringify({retry_failed_step:true}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Resume failed");}
    setStreaming(true);await monitor(currentRun.id);
  };
  const refreshRun=async()=>{
    if(!currentRun)return;
    setCurrentRun(await loadRun(currentRun.id));
  };
  return{messages,sendMessage,streaming,error,currentRun,decide,clarify,cancel,resume,refreshRun};
}

export async function sendFeedback(sessionId:string,rating:number){
  const token=getToken();
  if(!token)throw new Error("Sign in with Google first");
  await fetch(`${API}/feedback`,{method:"POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`},body:JSON.stringify({session_id:sessionId,rating})});
}
