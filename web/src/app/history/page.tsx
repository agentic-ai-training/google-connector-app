"use client";

import {FormEvent,useEffect,useState} from "react";
import Link from "next/link";
import {API,currentUser,getToken} from "@/hooks/useChat";

type HistoryRun={
  id:string;session_id:string;user_id:string;request:string;status:string;
  current_phase:string;technical_completion:number;functional_completion:number;
  user_visible_completion:number;side_effect_integrity:number;risk_level:string;
  error_category?:string;models_used:string[];services:string[];input_tokens:number;
  output_tokens:number;deployment_version?:string;queued_at:string;completed_at?:string;
};

const STATUSES=["","queued","awaiting_clarification","awaiting_approval","running","completed","partial","failed","cancelled"];
const SERVICES=["","gmail","calendar","meet","drive","docs","sheets","tasks","chat","contacts"];

export default function History(){
  const [runs,setRuns]=useState<HistoryRun[]>([]);
  const [admin,setAdmin]=useState(false);
  const [error,setError]=useState("");
  const [filters,setFilters]=useState({
    user_id:"",session_id:"",status:"",service:"",model:"",failure:"",
    deployment_version:"",started_after:"",started_before:"",
  });
  const load=async()=>{
    setError("");
    const query=new URLSearchParams();
    Object.entries(filters).forEach(([key,value])=>{if(value)query.set(key,value);});
    const endpoint=admin?"/admin/runs":"/runs";
    const response=await fetch(`${API}${endpoint}?${query}`,{
      headers:{Authorization:`Bearer ${getToken()??""}`},
    });
    const data=await response.json();
    if(!response.ok)throw new Error(data.detail??"Unable to load run history");
    setRuns(data.runs);
  };
  useEffect(()=>{
    let active=true;
    currentUser().then(user=>{if(active)setAdmin(Boolean(user.admin));})
      .catch(()=>{if(active)setError("Sign in before viewing history");});
    return()=>{active=false;};
  },[]);
  useEffect(()=>{queueMicrotask(()=>void load().catch(value=>setError(value instanceof Error?value.message:"Unable to load")));
    // Initial/admin-role loading is deliberate; filters apply only on submit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[admin]);
  const submit=(event:FormEvent)=>{
    event.preventDefault();
    void load().catch(value=>setError(value instanceof Error?value.message:"Unable to load"));
  };
  const update=(name:string,value:string)=>setFilters(current=>({...current,[name]:value}));
  return <main className="mx-auto min-h-screen max-w-7xl bg-zinc-50 p-6 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50">
    <header className="mb-6 flex items-center justify-between"><div><h1 className="text-2xl font-semibold">Durable run history</h1><p className="text-sm text-zinc-500">High-cardinality workflow facts come from PostgreSQL, not Prometheus labels.</p></div><Link href="/" className="rounded-lg border px-4 py-2">Back to agent</Link></header>
    <form onSubmit={submit} className="mb-6 grid gap-3 rounded-xl border bg-white p-4 dark:bg-zinc-900 sm:grid-cols-3 lg:grid-cols-5">
      {admin&&<input aria-label="User email" placeholder="User email" className="rounded border bg-transparent p-2" value={filters.user_id} onChange={event=>update("user_id",event.target.value)}/>}
      <input aria-label="Session ID" placeholder="Session ID" className="rounded border bg-transparent p-2" value={filters.session_id} onChange={event=>update("session_id",event.target.value)}/>
      <select aria-label="Status" className="rounded border bg-transparent p-2" value={filters.status} onChange={event=>update("status",event.target.value)}>{STATUSES.map(value=><option key={value} value={value}>{value||"All statuses"}</option>)}</select>
      <select aria-label="Service" className="rounded border bg-transparent p-2" value={filters.service} onChange={event=>update("service",event.target.value)}>{SERVICES.map(value=><option key={value} value={value}>{value||"All services"}</option>)}</select>
      <input aria-label="Model" placeholder="Model" className="rounded border bg-transparent p-2" value={filters.model} onChange={event=>update("model",event.target.value)}/>
      <input aria-label="Failure category" placeholder="Failure category" className="rounded border bg-transparent p-2" value={filters.failure} onChange={event=>update("failure",event.target.value)}/>
      <input aria-label="Deployment version" placeholder="Deployment version" className="rounded border bg-transparent p-2" value={filters.deployment_version} onChange={event=>update("deployment_version",event.target.value)}/>
      <input aria-label="From time" type="datetime-local" className="rounded border bg-transparent p-2" value={filters.started_after} onChange={event=>update("started_after",event.target.value)}/>
      <input aria-label="To time" type="datetime-local" className="rounded border bg-transparent p-2" value={filters.started_before} onChange={event=>update("started_before",event.target.value)}/>
      <button className="rounded bg-blue-600 px-4 py-2 text-white">Apply filters</button>
    </form>
    {error&&<p className="mb-4 rounded bg-red-50 p-3 text-red-700">{error}</p>}
    <div className="overflow-x-auto rounded-xl border bg-white dark:bg-zinc-900"><table className="w-full min-w-[1100px] text-left text-sm"><thead className="border-b"><tr><th className="p-3">Run / session</th>{admin&&<th className="p-3">User</th>}<th className="p-3">Task</th><th className="p-3">Status</th><th className="p-3">Progress</th><th className="p-3">Services / models</th><th className="p-3">Failure</th><th className="p-3">Time / version</th></tr></thead><tbody>{runs.map(run=><tr key={run.id} className="border-b align-top"><td className="p-3 font-mono text-xs">{run.id.slice(0,8)}<br/>{run.session_id.slice(0,16)}</td>{admin&&<td className="p-3">{run.user_id}</td>}<td className="max-w-sm p-3">{run.request}</td><td className="p-3">{run.status.replaceAll("_"," ")}<br/><span className="text-xs text-zinc-500">{run.current_phase}</span></td><td className="p-3 text-xs">T {Math.round(run.technical_completion)} · F {Math.round(run.functional_completion)} · U {Math.round(run.user_visible_completion)} · S {Math.round(run.side_effect_integrity)}</td><td className="p-3 text-xs">{run.services.join(", ")||"—"}<br/>{run.models_used.join(", ")||"—"}<br/>{run.input_tokens+run.output_tokens} tokens</td><td className="p-3">{run.error_category||"—"}</td><td className="p-3 text-xs">{new Date(run.queued_at).toLocaleString()}<br/>{run.deployment_version||"—"}</td></tr>)}{runs.length===0&&<tr><td className="p-6 text-center text-zinc-500" colSpan={admin?8:7}>No runs match these filters.</td></tr>}</tbody></table></div>
  </main>;
}
