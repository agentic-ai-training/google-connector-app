"use client";

import {FormEvent,useCallback,useEffect,useState} from "react";
import Link from "next/link";
import {API,currentUser,getToken} from "@/hooks/useChat";

type CodingRun={
  id:string;repository:string;base_ref:string;request_excerpt:string;status:string;
  current_phase:string;planner_model:string;model_policy_version:string;
  tool_policy_version:string;approval_status:string;approval_action_hash?:string;
  approval_expires_at?:string;publication_status:string;pull_request_url?:string;
  ci_check_url?:string;input_tokens:number;output_tokens:number;attempt_count:number;
  error_category?:string;error_message?:string;created_at:string;updated_at:string;
  approval_preview?:{changes?:Array<{path?:string}>};
  steps?:Array<{id:string;sequence_no:number;phase:string;tool_name?:string;status:string;duration_ms?:number}>;
};

const terminal=new Set(["completed","failed","blocked","cancelled"]);

export default function CodingAgentPage(){
  const [runs,setRuns]=useState<CodingRun[]>([]);
  const [selected,setSelected]=useState<CodingRun|null>(null);
  const [repository,setRepository]=useState("agentic-ai-training/google-connector-app");
  const [request,setRequest]=useState("");
  const [consent,setConsent]=useState(false);
  const [message,setMessage]=useState("Checking administrator session…");
  const headers=useCallback(()=>({
    "Content-Type":"application/json",Authorization:`Bearer ${getToken()??""}`,
  }),[]);

  const load=useCallback(async()=>{
    const response=await fetch(`${API}/coding/runs?limit=50`,{headers:headers()});
    const body=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(body.detail??"Unable to load coding runs");
    setRuns(body.runs??[]);
  },[headers]);

  useEffect(()=>{
    let active=true;
    currentUser().then(user=>{
      if(!user.admin)throw new Error("The hosted coding pilot is administrator-only.");
      if(active)setMessage("");
      return load();
    }).catch(error=>{if(active)setMessage(error instanceof Error?error.message:"Unable to load");});
    return()=>{active=false;};
  },[load]);

  useEffect(()=>{
    if(!runs.some(run=>!terminal.has(run.status)))return;
    const timer=window.setInterval(()=>void load().catch(()=>undefined),5000);
    return()=>window.clearInterval(timer);
  },[load,runs]);

  const open=async(id:string)=>{
    const response=await fetch(`${API}/coding/runs/${id}`,{headers:headers()});
    const body=await response.json();
    if(!response.ok)throw new Error(body.detail??"Unable to load run");
    setSelected(body.run);
  };
  const create=async(event:FormEvent)=>{
    event.preventDefault();setMessage("");
    const response=await fetch(`${API}/coding/runs`,{
      method:"POST",headers:headers(),body:JSON.stringify({
        repository,base_ref:"main",request,idempotency_key:crypto.randomUUID(),
        source_egress_consent:consent,
      }),
    });
    const body=await response.json();
    if(!response.ok){setMessage(body.detail??"Unable to create coding run");return;}
    setRequest("");setConsent(false);setSelected(body.run);await load();
  };
  const decide=async(decision:"approved"|"rejected")=>{
    if(!selected?.approval_action_hash)return;
    const response=await fetch(`${API}/coding/runs/${selected.id}/decision`,{
      method:"POST",headers:headers(),body:JSON.stringify({
        decision,action_hash:selected.approval_action_hash,note:"Admin portal decision",
      }),
    });
    const body=await response.json();
    if(!response.ok){setMessage(body.detail??"Decision failed");return;}
    setSelected(body.run);await load();
  };
  const cancel=async()=>{
    if(!selected)return;
    const response=await fetch(`${API}/coding/runs/${selected.id}/cancel`,{
      method:"POST",headers:headers(),body:JSON.stringify({reason:"Cancelled from coding portal"}),
    });
    const body=await response.json();
    if(!response.ok){setMessage(body.detail??"Cancellation failed");return;}
    setSelected(body.run);await load();
  };

  return <main className="mx-auto min-h-screen max-w-7xl space-y-6 p-6">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div><h1 className="text-2xl font-semibold">Durable coding agent</h1><p className="text-sm text-zinc-500">Groq plans typed Rust-broker calls; approval, keyless validation, draft PR, and trusted CI remain separate evidence gates.</p></div>
      <nav className="flex gap-3 text-sm"><Link className="underline" href="/">Agent</Link><Link className="underline" href="/admin/improvements">Improvement review</Link></nav>
    </header>
    {message&&<p className="rounded border border-amber-300 bg-amber-50 p-3 text-amber-900">{message}</p>}
    <form onSubmit={create} className="grid gap-3 rounded-xl border p-4">
      <label className="grid gap-1 text-sm">Repository<input className="rounded border bg-transparent p-2" value={repository} onChange={event=>setRepository(event.target.value)} /></label>
      <label className="grid gap-1 text-sm">Requested repository outcome<textarea className="min-h-28 rounded border bg-transparent p-2" value={request} onChange={event=>setRequest(event.target.value)} /></label>
      <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={consent} onChange={event=>setConsent(event.target.checked)} /><span>I approve sending the request and bounded non-secret source excerpts to the configured Groq coding model. No credentials or shell authority are sent.</span></label>
      <button disabled={!request.trim()||!consent} className="w-fit rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-40">Create durable coding run</button>
    </form>
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
      <section className="space-y-2"><h2 className="font-semibold">Runs</h2>{runs.map(run=><button key={run.id} onClick={()=>void open(run.id).catch(error=>setMessage(String(error)))} className="block w-full rounded border p-3 text-left"><span className="font-mono text-xs">{run.id.slice(0,8)}</span> · {run.status.replaceAll("_"," ")}<br/><span className="text-sm">{run.request_excerpt}</span><br/><span className="text-xs text-zinc-500">{new Date(run.created_at).toLocaleString()} · {run.input_tokens+run.output_tokens} tokens</span></button>)}</section>
      <section className="rounded-xl border p-4">{!selected?<p>Select a run to inspect its durable evidence.</p>:<div className="space-y-3">
        <div><span className="font-mono text-xs">{selected.id}</span><h2 className="text-xl font-semibold">{selected.status.replaceAll("_"," ")}</h2><p>{selected.current_phase.replaceAll("_"," ")} · {selected.repository}@{selected.base_ref}</p></div>
        <p className="text-sm">Model: {selected.planner_model} · Tokens: {selected.input_tokens+selected.output_tokens} · Attempts: {selected.attempt_count}</p>
        {selected.error_message&&<p className="rounded bg-red-50 p-3 text-sm text-red-800">{selected.error_category}: {selected.error_message}</p>}
        {selected.approval_preview&&<div className="rounded border border-amber-300 p-3"><h3 className="font-semibold">Exact frozen-plan approval required</h3><p className="text-xs">Hash: {selected.approval_action_hash}<br/>Expires: {selected.approval_expires_at?new Date(selected.approval_expires_at).toLocaleString():"—"}</p><ul className="mt-2 list-inside list-disc text-sm">{(selected.approval_preview.changes??[]).map((change,index)=><li key={`${change.path}-${index}`}>{change.path}</li>)}</ul><div className="mt-3 flex gap-2"><button onClick={()=>void decide("approved")} className="rounded bg-blue-600 px-3 py-2 text-white">Approve exact plan</button><button onClick={()=>void decide("rejected")} className="rounded border border-red-500 px-3 py-2 text-red-600">Reject</button></div></div>}
        {selected.pull_request_url&&<a className="block text-blue-700 underline" href={selected.pull_request_url} target="_blank" rel="noreferrer">Open draft PR</a>}
        {selected.ci_check_url&&<a className="block text-blue-700 underline" href={selected.ci_check_url} target="_blank" rel="noreferrer">Open trusted CI evidence</a>}
        {(selected.steps??[]).length>0&&<div><h3 className="font-semibold">Steps</h3>{selected.steps?.map(step=><p key={step.id} className="text-sm">{step.sequence_no}. {step.phase} — {step.status} {step.duration_ms!=null?`(${step.duration_ms} ms)`:""}</p>)}</div>}
        {!terminal.has(selected.status)&&<button onClick={()=>void cancel()} className="rounded border px-3 py-2">Cancel run</button>}
      </div>}</section>
    </div>
  </main>;
}
