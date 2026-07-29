"use client";

import {FormEvent,useEffect,useState} from "react";
import FeedbackButtons from "@/components/FeedbackButtons";
import {
  API,AgentRun,beginGoogleLogin,CONTROL_FRONTEND_URL,currentUser,CurrentUser,
  disconnectGoogle,frontendCandidate,getToken,handoffFrontend,
  IS_CANDIDATE_FRONTEND,RunArtifact,useChat,
} from "@/hooks/useChat";

function StepMark({status}:{status:string}){
  return <span aria-label={status}>{status==="completed"?"✓":status==="failed"?"✗":status==="running"?"◉":"○"}</span>;
}

const COMMON_TIMEZONES=[
  "Asia/Kolkata","UTC","Europe/London","Europe/Paris","Asia/Dubai",
  "Asia/Singapore","Asia/Tokyo","America/New_York","America/Chicago",
  "America/Denver","America/Los_Angeles","Australia/Sydney",
];
const isTimezoneQuestion=(question:string)=>question.toLocaleLowerCase().includes("timezone");
const browserTimezone=()=>Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC";
const TERMINAL_RUN_STATUSES=new Set(["completed","failed","partial","cancelled"]);

function formatDuration(milliseconds:number){
  const seconds=Math.max(0,Math.floor(milliseconds/1000));
  const hours=Math.floor(seconds/3600);
  const minutes=Math.floor((seconds%3600)/60);
  const remaining=seconds%60;
  return hours>0?`${hours}h ${minutes}m ${remaining}s`
    :minutes>0?`${minutes}m ${remaining}s`:`${remaining}s`;
}

function elapsedDuration(run:AgentRun,now:number){
  const started=Date.parse(run.queued_at??run.started_at??"");
  if(!Number.isFinite(started))return run.elapsed_duration_ms??0;
  const completed=run.completed_at?Date.parse(run.completed_at):now;
  return Number.isFinite(completed)
    ?Math.max(0,completed-started):(run.elapsed_duration_ms??0);
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
  const [clarificationDraft,setClarificationDraft]=useState<{
    runId:string;answers:Record<string,string>;
  }>({runId:"",answers:{}});
  const [pendingImprovements,setPendingImprovements]=useState(0);
  const [clock,setClock]=useState(()=>Date.now());
  const [ragSyncing,setRagSyncing]=useState(false);
  const [ragSyncMessage,setRagSyncMessage]=useState("");

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
    currentUser().then(async value=>{
      if(!value.google_connected){
        localStorage.removeItem("agent_token");
        throw new Error(value.missing_scopes?.length
          ?"Reconnect Google once to approve the newly added Workspace permissions"
          :"Connect your Google account to continue");
      }
      if(active)setUser(value);
      try{
        const candidate=await frontendCandidate();
        if(!active)return;
        if(candidate.eligible&&candidate.url){
          if(new URL(candidate.url).origin!==window.location.origin){
            handoffFrontend(candidate.url,candidate.candidate_version);
          }
        }else if(IS_CANDIDATE_FRONTEND&&CONTROL_FRONTEND_URL&&
          new URL(CONTROL_FRONTEND_URL).origin!==window.location.origin){
          handoffFrontend(CONTROL_FRONTEND_URL);
        }
      }catch{
        // Candidate discovery is non-critical; the control UI remains available.
      }
    }).catch(error=>{
      localStorage.removeItem("agent_token");
      if(active)setAuthError(error instanceof Error?error.message:"Sign-in failed");
    }).finally(()=>{if(active)setAuthLoading(false);});
    return()=>{active=false;};
  },[]);

  const chat=useChat(session);
  const {messages,sendMessage,streaming,error,currentRun,decide,clarify,cancel,resume,refreshRun}=chat;
  const currentRunId=currentRun?.id;
  const currentRunStatus=currentRun?.status;
  useEffect(()=>{
    if(!currentRunId||!currentRunStatus||TERMINAL_RUN_STATUSES.has(currentRunStatus))return;
    const timer=window.setInterval(()=>setClock(Date.now()),1000);
    return()=>window.clearInterval(timer);
  },[currentRunId,currentRunStatus]);
  const clarifications=currentRun?.id===clarificationDraft.runId
    ?clarificationDraft.answers:{};
  const clarificationValue=(question:string)=>clarifications[question]
    ??(isTimezoneQuestion(question)?browserTimezone():"");
  const clarificationAnswers=Object.fromEntries(
    (currentRun?.clarification_questions??[]).map(
      question=>[question,clarificationValue(question)],
    ),
  );
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
  const syncKnowledge=async()=>{
    setRagSyncing(true);setRagSyncMessage("");
    try{
      const response=await fetch(`${API}/runs/rag/sync`,{
        method:"POST",
        headers:{
          "Content-Type":"application/json",
          Authorization:`Bearer ${getToken()??""}`,
        },
        body:JSON.stringify({
          sources:["gmail","drive","calendar"],
          max_items_per_source:25,
          requeue_known_failures:true,
        }),
      });
      const payload=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(
        typeof payload?.detail==="string"?payload.detail:"Knowledge indexing failed",
      );
      setRagSyncMessage(
        payload?.created===false
          ?"Your existing private indexing job is still active."
          :"Private source-aware indexing was queued and will continue if you leave this page.",
      );
      await refreshRun();
    }catch(error){
      setRagSyncMessage(error instanceof Error?error.message:"Knowledge indexing failed");
    }finally{setRagSyncing(false);}
  };
  const totalModelInput=(currentRun?.model_usage??[]).reduce(
    (total,item)=>total+item.input_tokens,0,
  );
  const totalModelOutput=(currentRun?.model_usage??[]).reduce(
    (total,item)=>total+item.output_tokens,0,
  );
  const hasDetailedModelUsage=(currentRun?.model_usage?.length??0)>0;
  const displayedInputTokens=hasDetailedModelUsage
    ?totalModelInput:(currentRun?.input_tokens??0);
  const displayedOutputTokens=hasDetailedModelUsage
    ?totalModelOutput:(currentRun?.output_tokens??0);
  const conversationContext=currentRun?.planning_diagnostics?.conversation_context;
  const contextSources=conversationContext?.source_run_ids??[];
  const ragRetrievals=currentRun?.rag_retrievals??[];
  const ragReturned=ragRetrievals.reduce((sum,item)=>sum+item.returned_count,0);
  const ragUsed=ragRetrievals.reduce((sum,item)=>sum+item.used_count,0);
  const ragSources=[...new Set(ragRetrievals.flatMap(item=>item.source_types??[]))];
  const ragMode=currentRun?.plan?.rag_mode??"none";
  const ragStatus=ragMode==="none"
    ?"not requested for this live/direct operation"
    :ragRetrievals.length>0
      ?`${ragMode} (${ragUsed}/${ragReturned} results used${ragSources.length?`; sources: ${ragSources.join(", ")}`:""})`
      :currentRun?.rag_index_status?.ready
        ?`${ragMode} (retrieval has not completed or recorded evidence)`
        :`${ragMode} (this user has no ready indexed corpus)`;
  const ragIndex=currentRun?.rag_index_status;
  const privateIndexStatus=ragIndex?.ready
    ?`${ragIndex.sources.reduce((sum,item)=>sum+item.embedded_chunks,0)} embedded chunks`
    :ragIndex?.latest_sync
      ?`sync ${ragIndex.latest_sync.status}`
      :"not indexed";

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
        <button title="Explicitly index recent Gmail, Drive, and Calendar content for your private Knowledge RAG corpus" disabled={ragSyncing} onClick={()=>void syncKnowledge()} className="rounded-lg border px-3 py-2 disabled:opacity-50">{ragSyncing?"Indexing…":"Index my Workspace"}</button>
        <span>{user.email}</span>
        <button onClick={()=>void disconnect()} className="rounded-lg border px-3 py-2">Disconnect Google</button>
      </span>
    </header>
    {ragSyncMessage&&<p className="border-b bg-blue-50 px-4 py-2 text-xs text-blue-900 dark:bg-blue-950 dark:text-blue-100">{ragSyncMessage}</p>}
    {currentRun&&<section className="border-b bg-white p-4 text-sm dark:bg-zinc-900">
      <div className="flex items-center justify-between"><strong>Run {currentRun.id.slice(0,8)} · {currentRun.status.replaceAll("_"," ")}</strong><span>{Math.round(currentRun.functional_completion)}%</span></div>
      <div className="mt-2 h-2 overflow-hidden rounded bg-zinc-200"><div className="h-full bg-blue-600" style={{width:`${currentRun.functional_completion}%`}}/></div>
      <p className="mt-2 text-xs text-zinc-500">Phase: {currentRun.current_phase} · Services: {currentRun.plan?.services?.join(", ")||"general"} · Knowledge RAG: {ragStatus} · Private index: {privateIndexStatus} · Conversation context: {conversationContext?.prior_context_included?`${conversationContext.mode||"referential"} from ${contextSources.map(id=>id.slice(0,8)).join(", ")}`:"standalone"} · Deployment: {currentRun.deployment_version||"unknown"}</p>
      {(currentRun.okf_retrievals?.length??0)>0&&<p className="mt-1 text-xs text-zinc-500">Operational OKF: {Array.from(new Set(currentRun.okf_retrievals?.flatMap(item=>item.document_ids)??[])).join(", ")} · Versions: {Array.from(new Set(currentRun.okf_retrievals?.flatMap(item=>item.okf_versions)??[])).join(", ")} · Policy: {currentRun.okf_selection_events?.at(-1)?.payload?.selection_policy??"lexical legacy"}</p>}
      <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4"><span>Technical {Math.round(currentRun.technical_completion)}%</span><span>Functional {Math.round(currentRun.functional_completion)}%</span><span>User-visible {Math.round(currentRun.user_visible_completion)}%</span><span>Side effects {Math.round(currentRun.side_effect_integrity)}%</span></div>
      <div className="mt-3 rounded-lg border p-3 text-xs">
        <p><strong>{TERMINAL_RUN_STATUSES.has(currentRun.status)?"Total time":"Elapsed time"}:</strong> {formatDuration(elapsedDuration(currentRun,clock))} · Recorded step time: {formatDuration(currentRun.active_duration_ms??0)}</p>
        <p className="mt-1"><strong>LLM tokens:</strong> {(displayedInputTokens+displayedOutputTokens).toLocaleString()} total · {displayedInputTokens.toLocaleString()} input · {displayedOutputTokens.toLocaleString()} output</p>
        {hasDetailedModelUsage?<ul className="mt-1 space-y-1">
          {currentRun.model_usage?.map(usage=><li key={usage.model}><strong>{usage.model}</strong>: {(usage.input_tokens+usage.output_tokens).toLocaleString()} tokens ({usage.input_tokens.toLocaleString()} input · {usage.output_tokens.toLocaleString()} output · {usage.calls} {usage.calls===1?"call":"calls"})</li>)}
        </ul>:(currentRun.models_used?.length??0)>0
          ?<p className="mt-1">Models: {(currentRun.models_used??[]).join(", ")} (per-model allocation is unavailable for this legacy run)</p>
          :<p className="mt-1 text-zinc-500">{TERMINAL_RUN_STATUSES.has(currentRun.status)?"No LLM calls were used; this run followed a deterministic path.":"No LLM calls have been recorded yet."}</p>}
      </div>
      {currentRun.recent_events?.some(event=>event.event_type==="fallback_model_used")&&<p className="mt-2 text-xs text-amber-600">A validated fallback model was used for a safe step.</p>}
      <ol className="mt-3 space-y-1">{currentRun.steps.map(step=><li key={step.id}><StepMark status={step.status}/> {step.title}</li>)}</ol>
      {currentRun.artifacts?.length>0&&<div className="mt-3 rounded-lg border p-3"><strong>Verified artifacts and recovery</strong><ul className="mt-1 space-y-3">{currentRun.artifacts.map(artifact=><li key={artifact.id}>{artifact.url?.startsWith("https://")?<a className="text-blue-600 underline" href={artifact.url} target="_blank" rel="noreferrer">{artifact.artifact_type}: {artifact.external_id}</a>:<span>{artifact.artifact_type}: {artifact.external_id}</span>} <span className="text-zinc-500">({artifact.verification_status}; {artifact.cleanup_state})</span><ArtifactActions run={currentRun} artifact={artifact} onRefresh={refreshRun}/></li>)}</ul></div>}
      {currentRun.status==="awaiting_clarification"&&<div className="mt-4 rounded-xl border border-blue-400 bg-blue-50 p-3 text-blue-950"><strong>More information required</strong>{currentRun.clarification_questions?.map(question=><label key={question} className="mt-3 block"><span>{question}</span>{isTimezoneQuestion(question)?<select aria-label={question} className="mt-1 w-full rounded border bg-white p-2" value={clarificationValue(question)} onChange={event=>setClarificationDraft(draft=>({runId:currentRun.id,answers:{...(draft.runId===currentRun.id?draft.answers:{}),[question]:event.target.value}}))}>{[browserTimezone(),...COMMON_TIMEZONES].filter((value,index,values)=>values.indexOf(value)===index).map(value=><option key={value} value={value}>{value}</option>)}</select>:<input className="mt-1 w-full rounded border bg-white p-2" value={clarificationValue(question)} onChange={event=>setClarificationDraft(draft=>({runId:currentRun.id,answers:{...(draft.runId===currentRun.id?draft.answers:{}),[question]:event.target.value}}))}/>}</label>)}<button onClick={()=>void clarify(clarificationAnswers)} disabled={currentRun.clarification_questions?.some(question=>!clarificationValue(question).trim())} className="mt-3 rounded-lg bg-blue-600 px-4 py-2 text-white disabled:opacity-50">Apply answers</button></div>}
      {currentRun.status==="awaiting_approval"&&<div className="mt-4 rounded-xl border border-amber-400 bg-amber-50 p-3 text-amber-950"><strong>Confirmation required</strong><p className="mt-1">Approve only these external writes for this immutable plan.</p><ul className="mt-2 list-disc space-y-1 pl-5">{((currentRun.approval?.action_summary?.actions as Array<{service?:string;operation?:string;arguments?:Record<string,unknown>}>|undefined)??[]).map((action,index)=><li key={`${action.service}-${action.operation}-${index}`}><span className="font-medium">{action.service} · {action.operation}</span>{action.arguments&&Object.keys(action.arguments).length>0?<span>: {Object.entries(action.arguments).map(([key,value])=>`${key}=${Array.isArray(value)?value.join(", "):String(value)}`).join(" · ")}</span>:<span>: exact details will be resolved before any external call</span>}</li>)}</ul><p className="mt-2 text-xs">If typed arguments are incomplete, the bounded planner may prepare them before the first external attempt. It cannot silently retry a failed write.</p><div className="mt-3 flex gap-2"><button onClick={()=>void decide(true)} className="rounded-lg bg-amber-600 px-4 py-2 text-white">Approve and continue</button><button onClick={()=>void decide(false)} className="rounded-lg border px-4 py-2">Reject</button></div></div>}
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
