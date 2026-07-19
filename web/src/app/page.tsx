"use client";

import {FormEvent,useEffect,useState} from "react";
import FeedbackButtons from "@/components/FeedbackButtons";
import {
  API,AgentRun,beginGoogleLogin,currentUser,CurrentUser,disconnectGoogle,
  getToken,RunArtifact,useChat,
} from "@/hooks/useChat";

function StepMark({status}:{status:string}){
  return <span aria-label={status}>{status==="completed"?"✓":status==="failed"?"✗":status==="running"?"◉":"○"}</span>;
}

function ArtifactActions({run,artifact,onRefresh}:{
  run:AgentRun;artifact:RunArtifact;onRefresh:()=>Promise<void>;
}){
  const [message,setMessage]=useState("");
  const headers=()=>({"Content-Type":"application/json",Authorization:`Bearer ${getToken()??""}`});
  const act=async(action:"preserve"|"delete"|"cancel_event"|"retry_population"|"rollback_sharing")=>{
    setMessage("");
    const requested=await fetch(`${API}/runs/${run.id}/artifacts/${artifact.id}/cleanup-request`,{
      method:"POST",headers:headers(),body:JSON.stringify({action}),
    });
    const data=await requested.json();
    if(!requested.ok){setMessage(data.detail??"Unable to prepare artifact action");return;}
    if(data.action_hash){
      const label=action.replaceAll("_"," ");
      if(!window.confirm(`Confirm ${label} for ${artifact.artifact_type} ${artifact.external_id}?`)){
        setMessage("No change was made.");return;
      }
      const decided=await fetch(`${API}/runs/${run.id}/artifacts/${artifact.id}/cleanup-decision`,{
        method:"POST",headers:headers(),
        body:JSON.stringify({approved:true,action_hash:data.action_hash}),
      });
      const result=await decided.json();
      setMessage(result.status==="completed"?"Action completed.":result.result?.error??result.status);
    }else setMessage("Artifact retained.");
    await onRefresh();
  };
  return <div className="mt-1 flex flex-wrap gap-1 text-xs">
    <button className="rounded border px-2 py-1" onClick={()=>void act("preserve")}>Preserve</button>
    {artifact.safe_to_delete&&<button className="rounded border px-2 py-1" onClick={()=>void act("delete")}>Delete safely</button>}
    {artifact.artifact_type==="calendar"&&<button className="rounded border px-2 py-1" onClick={()=>void act("cancel_event")}>Cancel event</button>}
    {artifact.artifact_type==="drive"&&<button className="rounded border px-2 py-1" onClick={()=>void act("rollback_sharing")}>Roll back sharing</button>}
    {run.status==="partial"&&<button className="rounded border px-2 py-1" onClick={()=>void act("retry_population")}>Retry population</button>}
    {message&&<span className="self-center text-zinc-500">{message}</span>}
  </div>;
}

export default function Home(){
  const [session,setSession]=useState("");
  const [input,setInput]=useState("");
  const [user,setUser]=useState<CurrentUser|null>(null);
  const [authLoading,setAuthLoading]=useState(true);
  const [authError,setAuthError]=useState("");
  const [clarifications,setClarifications]=useState<Record<string,string>>({});
  const [pendingImprovements,setPendingImprovements]=useState(0);

  useEffect(()=>{
    let active=true;
    const fragment=new URLSearchParams(window.location.hash.slice(1));
    const returnedToken=fragment.get("access_token");
    const returnedError=fragment.get("oauth_error");
    if(returnedToken){
      localStorage.setItem("agent_token",returnedToken);
      history.replaceState(null,"",window.location.pathname);
    }
    if(returnedError){
      localStorage.removeItem("agent_token");
      queueMicrotask(()=>{if(active){setAuthError(returnedError);setAuthLoading(false);}});
      history.replaceState(null,"",window.location.pathname);
    }
    let id=localStorage.getItem("agent_session");
    if(!id){id=crypto.randomUUID();localStorage.setItem("agent_session",id);}
    queueMicrotask(()=>{if(active)setSession(id);});
    if(!getToken()){
      queueMicrotask(()=>{if(active)setAuthLoading(false);});
      return()=>{active=false;};
    }
    currentUser().then(value=>{
      if(!value.google_connected){
        localStorage.removeItem("agent_token");
        throw new Error(value.missing_scopes?.length
          ?"Reconnect Google once to approve the newly added Workspace permissions"
          :"Connect your Google account to continue");
      }
      if(active)setUser(value);
    }).catch(error=>{
      localStorage.removeItem("agent_token");
      if(active)setAuthError(error instanceof Error?error.message:"Sign-in failed");
    }).finally(()=>{if(active)setAuthLoading(false);});
    return()=>{active=false;};
  },[]);

  const chat=useChat(session);
  const {messages,sendMessage,streaming,error,currentRun,decide,clarify,cancel,resume,refreshRun}=chat;
  useEffect(()=>{
    if(!user?.admin)return;
    let active=true;
    const refresh=()=>void fetch(`${API}/admin/improvements-pending/count`,{
      headers:{Authorization:`Bearer ${getToken()??""}`},
    }).then(response=>response.ok?response.json():Promise.reject(new Error("pending-count")))
      .then(data=>{if(active)setPendingImprovements(data.total??0);}).catch(()=>undefined);
    refresh();
    const timer=window.setInterval(refresh,30000);
    return()=>{active=false;window.clearInterval(timer);};
  },[user?.admin]);

  const submit=(event:FormEvent)=>{
    event.preventDefault();
    const value=input.trim();
    if(value&&!streaming&&user){setInput("");void sendMessage(value);}
  };
  const disconnect=async()=>{await disconnectGoogle();setUser(null);};

  if(authLoading)return <main className="grid h-screen place-items-center">Checking your session…</main>;
  if(!user)return <main className="grid h-screen place-items-center bg-zinc-50 p-6 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50">
    <section className="max-w-lg rounded-2xl border bg-white p-8 text-center shadow-sm dark:bg-zinc-900">
      <h1 className="text-2xl font-semibold">Google Workspace Agent</h1>
      <p className="mt-3 text-zinc-600 dark:text-zinc-300">Sign in and grant access to use your own Gmail, Calendar, Drive, Docs, Sheets, Tasks, Chat, Contacts, and Google Meet.</p>
      {authError&&<p className="mt-3 text-red-500">{authError}</p>}
      <button onClick={beginGoogleLogin} className="mt-6 rounded-xl bg-blue-600 px-5 py-3 font-medium text-white">Sign in with Google</button>
    </section>
  </main>;

  return <main className="mx-auto flex h-screen max-w-4xl flex-col bg-zinc-50 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50">
    <header className="flex items-center justify-between border-b p-4">
      <span className="text-xl font-semibold">Google Workspace Agent</span>
      <span className="flex items-center gap-2 text-sm">
        <a href="/history" className="rounded-lg border px-3 py-2">History</a>
        {user.admin&&<a href="/admin/improvements" className="rounded-lg border px-3 py-2">Improvements{pendingImprovements>0&&<span className="ml-2 rounded-full bg-red-600 px-2 py-0.5 text-xs text-white">{pendingImprovements}</span>}</a>}
        <span>{user.email}</span>
        <button onClick={()=>void disconnect()} className="rounded-lg border px-3 py-2">Disconnect Google</button>
      </span>
    </header>
    {currentRun&&<section className="border-b bg-white p-4 text-sm dark:bg-zinc-900">
      <div className="flex items-center justify-between"><strong>Run {currentRun.id.slice(0,8)} · {currentRun.status.replaceAll("_"," ")}</strong><span>{Math.round(currentRun.functional_completion)}%</span></div>
      <div className="mt-2 h-2 overflow-hidden rounded bg-zinc-200"><div className="h-full bg-blue-600" style={{width:`${currentRun.functional_completion}%`}}/></div>
      <p className="mt-2 text-xs text-zinc-500">Phase: {currentRun.current_phase} · Services: {currentRun.plan?.services?.join(", ")||"general"} · RAG: {currentRun.plan?.rag_mode||"none"} · Deployment: {currentRun.deployment_version||"unknown"}</p>
      <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4"><span>Technical {Math.round(currentRun.technical_completion)}%</span><span>Functional {Math.round(currentRun.functional_completion)}%</span><span>User-visible {Math.round(currentRun.user_visible_completion)}%</span><span>Side effects {Math.round(currentRun.side_effect_integrity)}%</span></div>
      {(currentRun.models_used?.length??0)>0&&<p className="mt-2 text-xs">Models: {(currentRun.models_used??[]).join(", ")} · Tokens: {currentRun.input_tokens+currentRun.output_tokens}</p>}
      {currentRun.recent_events?.some(event=>event.event_type==="fallback_model_used")&&<p className="mt-2 text-xs text-amber-600">A validated fallback model was used for a safe step.</p>}
      <ol className="mt-3 space-y-1">{currentRun.steps.map(step=><li key={step.id}><StepMark status={step.status}/> {step.title}</li>)}</ol>
      {currentRun.artifacts?.length>0&&<div className="mt-3 rounded-lg border p-3"><strong>Verified artifacts and recovery</strong><ul className="mt-1 space-y-3">{currentRun.artifacts.map(artifact=><li key={artifact.id}>{artifact.url?.startsWith("https://")?<a className="text-blue-600 underline" href={artifact.url} target="_blank" rel="noreferrer">{artifact.artifact_type}: {artifact.external_id}</a>:<span>{artifact.artifact_type}: {artifact.external_id}</span>} <span className="text-zinc-500">({artifact.verification_status}; {artifact.cleanup_state})</span><ArtifactActions run={currentRun} artifact={artifact} onRefresh={refreshRun}/></li>)}</ul></div>}
      {currentRun.status==="awaiting_clarification"&&<div className="mt-4 rounded-xl border border-blue-400 bg-blue-50 p-3 text-blue-950"><strong>More information required</strong>{currentRun.clarification_questions?.map(question=><label key={question} className="mt-3 block"><span>{question}</span><input className="mt-1 w-full rounded border bg-white p-2" value={clarifications[question]??""} onChange={event=>setClarifications(values=>({...values,[question]:event.target.value}))}/></label>)}<button onClick={()=>void clarify(clarifications)} disabled={currentRun.clarification_questions?.some(question=>!clarifications[question]?.trim())} className="mt-3 rounded-lg bg-blue-600 px-4 py-2 text-white disabled:opacity-50">Apply answers</button></div>}
      {currentRun.status==="awaiting_approval"&&<div className="mt-4 rounded-xl border border-amber-400 bg-amber-50 p-3 text-amber-950"><strong>Confirmation required</strong><p className="mt-1">Review the exact high-risk external write before continuing.</p><div className="mt-3 flex gap-2"><button onClick={()=>void decide(true)} className="rounded-lg bg-amber-600 px-4 py-2 text-white">Approve and continue</button><button onClick={()=>void decide(false)} className="rounded-lg border px-4 py-2">Reject</button></div></div>}
      {["queued","running"].includes(currentRun.status)&&<button onClick={()=>void cancel()} className="mt-3 rounded-lg border px-3 py-2">Cancel run</button>}
      {["failed","partial"].includes(currentRun.status)&&<button onClick={()=>void resume()} className="mt-3 rounded-lg bg-blue-600 px-3 py-2 text-white">Resume from failed step</button>}
    </section>}
    <section className="flex-1 space-y-4 overflow-y-auto p-4">
      {messages.length===0&&<p className="text-center text-zinc-500">Ask me to work with Gmail, Calendar, Drive, Docs, Sheets, Tasks, Chat, Contacts, or Google Meet.</p>}
      {messages.map((message,index)=><div key={index} className={`flex ${message.role==="user"?"justify-end":"justify-start"}`}><div className={`max-w-[80%] rounded-2xl p-3 ${message.role==="user"?"bg-blue-600 text-white":"bg-zinc-200 dark:bg-zinc-800"}`}><p className="whitespace-pre-wrap">{message.content||"…"}</p>{message.role==="assistant"&&!streaming&&message.content&&<FeedbackButtons sessionId={session}/>}</div></div>)}
      {error&&<p className="text-red-500">{error}</p>}
    </section>
    <form onSubmit={submit} className="flex gap-2 border-t p-4"><input aria-label="Message" className="flex-1 rounded-xl border bg-transparent p-3" value={input} onChange={event=>setInput(event.target.value)} placeholder="Type a message…"/><button className="rounded-xl bg-blue-600 px-5 text-white disabled:opacity-50" disabled={!session||streaming||["awaiting_approval","awaiting_clarification"].includes(currentRun?.status??"")}>{streaming?"Working…":"Send"}</button></form>
  </main>;
}
