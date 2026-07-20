"use client";

import {useCallback,useEffect,useState} from "react";
import Link from "next/link";
import {API,beginGoogleLogin,getToken} from "@/hooks/useChat";

type Proposal={proposal_key:string;title:string;proposal_type:string;sanitized_summary:string;status:string;severity:string;risk_level:string;affected_sessions:number;content_hash:string;exact_diff?:string;canary_status?:string;canary_metrics?:Record<string,unknown>;candidate_kind:"diagnosis"|"code"|"okf"|"config"|"prompt";candidate_state:"diagnosis_only"|"implementation_draft"|"validated_implementation"|"deployed_canary";candidate_version?:string;validation_report?:Record<string,unknown>;deployment_evidence?:Record<string,unknown>};
type FeatureFlag={name:string;enabled:boolean;config:Record<string,unknown>;updated_by:string;updated_at:string};
type Notification={id:string;proposal_key:string;channel:string;event_type:string;status:string;external_reference?:string;error_message?:string;created_at:string};

async function requireAuthorized(response:Response,fallback:string){
  if(response.status===401){
    localStorage.removeItem("agent_token");
    throw new Error("Your session is missing or expired. Sign in again with the administrator Google account.");
  }
  if(response.status===403)throw new Error("Administrator access is required. Sign in with an email listed in ADMIN_EMAILS.");
  if(!response.ok){
    const data=await response.json().catch(()=>({detail:fallback}));
    throw new Error(data.detail??fallback);
  }
  return response;
}

export default function Improvements(){
  const [proposals,setProposals]=useState<Proposal[]>([]);
  const [notifications,setNotifications]=useState<Notification[]>([]);
  const [error,setError]=useState("");
  const [message,setMessage]=useState("");
  const [flags,setFlags]=useState<FeatureFlag[]>([]);
  const [pilotPercentage,setPilotPercentage]=useState(10);
  const [pilotUsers,setPilotUsers]=useState("");
  const [authorized,setAuthorized]=useState(false);
  const [authRequired,setAuthRequired]=useState(false);
  const headers=()=>({Authorization:`Bearer ${getToken()??""}`});
  const load=async()=>{
    if(!getToken())throw new Error("Sign in with the administrator Google account to open the improvement portal.");
    const [proposalResponse,notificationResponse]=await Promise.all([
      fetch(`${API}/admin/improvements`,{headers:headers()}),
      fetch(`${API}/admin/improvement-notifications`,{headers:headers()}),
    ]);
    await requireAuthorized(proposalResponse,"Unable to load proposals");
    const proposalData=await proposalResponse.json();
    setProposals(proposalData.proposals);
    await requireAuthorized(notificationResponse,"Unable to load the notification ledger");
    setNotifications((await notificationResponse.json()).notifications);
    setAuthorized(true);setAuthRequired(false);setError("");
  };
  const loadFlags=useCallback(async()=>{
    if(!getToken())throw new Error("Sign in with the administrator Google account to open the improvement portal.");
    const response=await fetch(`${API}/admin/feature-flags`,{headers:{Authorization:`Bearer ${getToken()??""}`}});
    await requireAuthorized(response,"Unable to load rollout controls");
    const data=await response.json();
    setFlags(data.feature_flags);
    const pilot=(data.feature_flags as FeatureFlag[]).find(item=>item.name==="pilot_cohorts");
    if(pilot){
      setPilotPercentage(Number(pilot.config.percentage??10));
      setPilotUsers(((pilot.config.allowed_users as string[]|undefined)??[]).join("\n"));
    }
  },[]);
  useEffect(()=>{
    let active=true;
    const failed=(value:unknown)=>{
      if(!active)return;
      const text=value instanceof Error?value.message:"Unable to load";
      setError(text);setAuthorized(false);
      if(text.includes("Sign in")||text.includes("session")||text.includes("Administrator"))setAuthRequired(true);
    };
    const refresh=()=>{
      void load().catch(failed);
      void loadFlags().catch(failed);
    };
    queueMicrotask(refresh);
    const timer=window.setInterval(refresh,30000);
    return()=>{active=false;window.clearInterval(timer);};
    // Refresh functions deliberately share this page's authenticated state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[loadFlags]);
  const decide=async(proposal:Proposal,decision:"approved"|"rejected"|"changes_requested",stage:"canary-decision"|"activate-canary"|"promotion-decision"="canary-decision")=>{
    setError("");setMessage("");
    const note=decision==="changes_requested"?window.prompt("Describe the concrete changes required before this can be reviewed again:"):undefined;
    if(decision==="changes_requested"&&!note?.trim())throw new Error("A change-request note is required.");
    const response=await fetch(`${API}/admin/improvements/${proposal.proposal_key}/${stage}`,{
      method:"POST",headers:{...headers(),"Content-Type":"application/json"},
      body:JSON.stringify({decision,proposal_hash:proposal.content_hash,note}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Decision failed");}
    setMessage("Decision recorded. The candidate hash is frozen for the next stage.");
    await load();
  };
  const publish=async(proposal:Proposal,channel:"email"|"github")=>{
    setError("");setMessage("");
    const isGithub=channel==="github";
    const confirmation=isGithub?"PUBLISH SANITIZED DRAFT PR":"SEND SANITIZED REVIEW EMAIL";
    if(!window.confirm(`${confirmation}. Continue?`))return;
    const endpoint=isGithub?"publish-draft-pr":"notify-email";
    const response=await fetch(`${API}/admin/improvements/${proposal.proposal_key}/${endpoint}`,{
      method:"POST",headers:{...headers(),"Content-Type":"application/json"},
      body:JSON.stringify({proposal_hash:proposal.content_hash,confirmation}),
    });
    const data=await response.json();
    if(!response.ok)throw new Error(data.detail??"Publication failed");
    setMessage(isGithub?`Draft PR created: ${data.url}`:"Sanitized review email sent.");
    await load();
  };
  const updateFlag=async(name:string,enabled:boolean,config:Record<string,unknown>)=>{
    const response=await fetch(`${API}/admin/feature-flags/${name}`,{
      method:"PUT",headers:{...headers(),"Content-Type":"application/json"},
      body:JSON.stringify({enabled,config}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Rollout update failed");}
    await loadFlags();
  };
  const safely=(action:()=>Promise<void>)=>void action().catch(value=>setError(value instanceof Error?value.message:"Action failed"));
  const pilot=flags.find(item=>item.name==="pilot_cohorts");
  const newRag=flags.find(item=>item.name==="new_rag");

  return <main className="mx-auto min-h-screen max-w-5xl bg-zinc-50 p-6 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50">
    <header className="mb-6 flex items-center justify-between"><div><h1 className="text-2xl font-semibold">Improvement review</h1><p className="text-zinc-500">Human approval is required before canary, promotion, and every external publication.</p></div><Link href="/" className="rounded-lg border px-4 py-2">Back to agent</Link></header>
    {error&&<p className="mb-3 rounded-xl bg-red-50 p-4 text-red-700">{error}</p>}
    {authRequired&&<button onClick={beginGoogleLogin} className="mb-4 rounded-lg bg-blue-600 px-4 py-2 text-white">Sign in with Google</button>}
    {message&&<p className="mb-3 rounded-xl bg-emerald-50 p-4 text-emerald-800">{message}</p>}
    {authorized&&<><section className="mb-6 rounded-xl border bg-white p-5 dark:bg-zinc-900"><h2 className="text-lg font-semibold">Pilot rollout controls</h2><p className="mt-1 text-sm text-zinc-500">Changes apply to new runs and are audited with your identity. Allowed users enter the pilot; denied users never do.</p><label className="mt-4 block text-sm">Pilot percentage<input type="number" min={0} max={100} value={pilotPercentage} onChange={event=>setPilotPercentage(Number(event.target.value))} className="mt-1 block w-32 rounded border bg-transparent p-2"/></label><label className="mt-3 block text-sm">Allowed pilot emails, one per line<textarea value={pilotUsers} onChange={event=>setPilotUsers(event.target.value)} className="mt-1 block min-h-24 w-full rounded border bg-transparent p-2"/></label><div className="mt-4 flex flex-wrap gap-2"><button onClick={()=>safely(()=>updateFlag("pilot_cohorts",true,{...(pilot?.config??{}),percentage:pilotPercentage,allowed_users:pilotUsers.split("\n").map(value=>value.trim()).filter(Boolean)}))} className="rounded-lg bg-blue-600 px-4 py-2 text-white">Enable/update pilot</button><button onClick={()=>safely(()=>updateFlag("pilot_cohorts",false,pilot?.config??{}))} className="rounded-lg border px-4 py-2">Disable pilot</button><button onClick={()=>safely(()=>updateFlag("new_rag",!newRag?.enabled,newRag?.config??{}))} className="rounded-lg border px-4 py-2">{newRag?.enabled?"Disable":"Enable"} new RAG</button></div><p className="mt-3 text-xs text-zinc-500">Pilot: {pilot?.enabled?"enabled":"disabled"} · New RAG: {newRag?.enabled?"enabled":"disabled"} · Live RL: locked off</p></section>
    <section className="space-y-4">{proposals.length===0&&!error&&<p className="rounded-xl border bg-white p-5 dark:bg-zinc-900">No improvement proposals are awaiting review.</p>}{proposals.map(proposal=>{const ready=proposal.candidate_state==="validated_implementation";const deployed=proposal.deployment_evidence?.verified===true;return <article key={proposal.proposal_key} className="rounded-xl border bg-white p-5 shadow-sm dark:bg-zinc-900"><div className="flex flex-wrap items-center gap-2 text-xs uppercase"><span className="rounded bg-zinc-200 px-2 py-1 dark:bg-zinc-700">{proposal.proposal_type}</span><span className="rounded bg-amber-100 px-2 py-1 text-amber-900">{proposal.severity}</span><span>{proposal.status.replaceAll("_"," ")}</span><span className={ready?"rounded bg-emerald-100 px-2 py-1 text-emerald-900":"rounded bg-red-100 px-2 py-1 text-red-800"}>{proposal.candidate_state.replaceAll("_"," ")}</span>{proposal.canary_status&&<span>canary: {proposal.canary_status}</span>}</div><h2 className="mt-3 text-lg font-semibold">{proposal.title}</h2><p className="mt-2 text-zinc-600 dark:text-zinc-300">{proposal.sanitized_summary}</p><p className="mt-2 text-sm">Affected sessions: {proposal.affected_sessions} · Risk: {proposal.risk_level}</p>{!ready&&<p className="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">This is a diagnosis, not an implemented upgrade. Canary approval is blocked until concrete changed files, hashes, passing validation commands, a rollback plan, and a candidate version are attached.</p>}{ready&&<p className="mt-3 text-sm">Candidate: <code>{proposal.candidate_version}</code> · Validation: passed · Deployment: {deployed?"verified":"not yet verified"}</p>}{proposal.exact_diff&&<pre className="mt-3 max-h-64 overflow-auto rounded bg-zinc-950 p-3 text-xs text-zinc-100">{proposal.exact_diff}</pre>}{proposal.status==="awaiting_review"&&<div className="mt-4 flex flex-wrap gap-2"><button disabled={!ready} title={!ready?"Attach and validate an implementation candidate first":undefined} onClick={()=>safely(()=>decide(proposal,"approved"))} className="rounded-lg bg-blue-600 px-4 py-2 text-white disabled:cursor-not-allowed disabled:opacity-40">Approve for canary</button><button onClick={()=>safely(()=>decide(proposal,"changes_requested"))} className="rounded-lg border px-4 py-2">Request changes</button><button onClick={()=>safely(()=>decide(proposal,"rejected"))} className="rounded-lg border border-red-500 px-4 py-2 text-red-600">Reject</button><button onClick={()=>safely(()=>publish(proposal,"email"))} className="rounded-lg border px-4 py-2">Send sanitized review email</button></div>}{proposal.status==="approved_for_canary"&&<div className="mt-4"><p className="mb-2 text-sm text-zinc-500">Activation is blocked until the frozen candidate deployment and its smoke tests are verified.</p><button disabled={!deployed} onClick={()=>safely(()=>decide(proposal,"approved","activate-canary"))} className="rounded-lg bg-blue-600 px-4 py-2 text-white disabled:cursor-not-allowed disabled:opacity-40">Activate deployed canary</button></div>}{proposal.status==="awaiting_promotion"&&<div className="mt-4 flex flex-wrap gap-2"><button onClick={()=>safely(()=>decide(proposal,"approved","promotion-decision"))} className="rounded-lg bg-emerald-600 px-4 py-2 text-white">Approve publication</button><button onClick={()=>safely(()=>decide(proposal,"changes_requested","promotion-decision"))} className="rounded-lg border px-4 py-2">Request changes</button><button onClick={()=>safely(()=>decide(proposal,"rejected","promotion-decision"))} className="rounded-lg border border-red-500 px-4 py-2 text-red-600">Reject</button></div>}{proposal.status==="approved_for_publication"&&<button onClick={()=>safely(()=>publish(proposal,"github"))} className="mt-4 rounded-lg bg-zinc-950 px-4 py-2 text-white dark:bg-white dark:text-zinc-950">Publish implementation draft PR</button>}</article>})}</section>
    <section className="mt-8 rounded-xl border bg-white p-5 dark:bg-zinc-900"><h2 className="text-lg font-semibold">Notification ledger</h2><p className="text-sm text-zinc-500">Admin/Grafana are internal. Email/GitHub remain skipped until explicitly confirmed and configured.</p><div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">Proposal</th><th className="p-2">Channel</th><th className="p-2">Event</th><th className="p-2">Status</th><th className="p-2">Reference/error</th></tr></thead><tbody>{notifications.slice(0,100).map(item=><tr key={item.id} className="border-t"><td className="p-2">{item.proposal_key}</td><td className="p-2">{item.channel}</td><td className="p-2">{item.event_type}</td><td className="p-2">{item.status}</td><td className="p-2">{item.external_reference?<a className="underline" href={item.external_reference} target="_blank" rel="noreferrer">Open</a>:item.error_message||"—"}</td></tr>)}</tbody></table></div></section></>}
  </main>;
}
