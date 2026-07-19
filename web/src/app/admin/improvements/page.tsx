"use client";

import {useEffect,useState} from "react";
import Link from "next/link";
import {API,getToken} from "@/hooks/useChat";

type Proposal={proposal_key:string;title:string;proposal_type:string;sanitized_summary:string;status:string;severity:string;risk_level:string;affected_sessions:number;content_hash:string;exact_diff?:string;canary_status?:string;canary_metrics?:Record<string,unknown>};

export default function Improvements(){
  const [proposals,setProposals]=useState<Proposal[]>([]);
  const [error,setError]=useState("");
  const headers=()=>({Authorization:`Bearer ${getToken()??""}`});
  const load=async()=>{
    const response=await fetch(`${API}/admin/improvements`,{headers:headers()});
    if(!response.ok)throw new Error(response.status===403?"Administrator access is required":"Unable to load proposals");
    const data=await response.json();setProposals(data.proposals);
  };
  useEffect(()=>{
    let active=true;
    const refresh=()=>{
      void fetch(`${API}/admin/improvements`,{headers:{Authorization:`Bearer ${getToken()??""}`}})
        .then(response=>{if(!response.ok)throw new Error(response.status===403?"Administrator access is required":"Unable to load proposals");return response.json();})
        .then(data=>{if(active)setProposals(data.proposals);})
        .catch(value=>{if(active)setError(value instanceof Error?value.message:"Unable to load");});
    };
    queueMicrotask(refresh);
    const timer=window.setInterval(refresh,30000);
    return()=>{active=false;window.clearInterval(timer)};
  },[]);
  const decide=async(proposal:Proposal,decision:"approved"|"rejected"|"changes_requested",stage:"canary-decision"|"activate-canary"|"promotion-decision"="canary-decision")=>{
    const response=await fetch(`${API}/admin/improvements/${proposal.proposal_key}/${stage}`,{method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({decision,proposal_hash:proposal.content_hash})});
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Decision failed");}
    await load();
  };
  return <main className="mx-auto min-h-screen max-w-5xl bg-zinc-50 p-6 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50"><header className="mb-6 flex items-center justify-between"><div><h1 className="text-2xl font-semibold">Improvement review</h1><p className="text-zinc-500">Human approval is required before canary and again before publication.</p></div><Link href="/" className="rounded-lg border px-4 py-2">Back to agent</Link></header>{error&&<p className="rounded-xl bg-red-50 p-4 text-red-700">{error}</p>}<section className="space-y-4">{proposals.length===0&&!error&&<p className="rounded-xl border bg-white p-5 dark:bg-zinc-900">No improvement proposals are awaiting review.</p>}{proposals.map(proposal=><article key={proposal.proposal_key} className="rounded-xl border bg-white p-5 shadow-sm dark:bg-zinc-900"><div className="flex flex-wrap items-center gap-2 text-xs uppercase"><span className="rounded bg-zinc-200 px-2 py-1 dark:bg-zinc-700">{proposal.proposal_type}</span><span className="rounded bg-amber-100 px-2 py-1 text-amber-900">{proposal.severity}</span><span>{proposal.status.replaceAll("_"," ")}</span>{proposal.canary_status&&<span>canary: {proposal.canary_status}</span>}</div><h2 className="mt-3 text-lg font-semibold">{proposal.title}</h2><p className="mt-2 text-zinc-600 dark:text-zinc-300">{proposal.sanitized_summary}</p><p className="mt-2 text-sm">Affected sessions: {proposal.affected_sessions} · Risk: {proposal.risk_level}</p>{proposal.exact_diff&&<pre className="mt-3 max-h-64 overflow-auto rounded bg-zinc-950 p-3 text-xs text-zinc-100">{proposal.exact_diff}</pre>}{proposal.status==="awaiting_review"&&<div className="mt-4 flex flex-wrap gap-2"><button onClick={()=>void decide(proposal,"approved")} className="rounded-lg bg-blue-600 px-4 py-2 text-white">Approve for canary</button><button onClick={()=>void decide(proposal,"changes_requested")} className="rounded-lg border px-4 py-2">Request changes</button><button onClick={()=>void decide(proposal,"rejected")} className="rounded-lg border border-red-500 px-4 py-2 text-red-600">Reject</button></div>}{proposal.status==="approved_for_canary"&&<div className="mt-4"><p className="mb-2 text-sm text-zinc-500">Activate only after the candidate version has been deployed to its selected cohort.</p><button onClick={()=>void decide(proposal,"approved","activate-canary")} className="rounded-lg bg-blue-600 px-4 py-2 text-white">Activate deployed canary</button></div>}{proposal.status==="awaiting_promotion"&&<div className="mt-4 flex flex-wrap gap-2"><button onClick={()=>void decide(proposal,"approved","promotion-decision")} className="rounded-lg bg-emerald-600 px-4 py-2 text-white">Approve publication</button><button onClick={()=>void decide(proposal,"changes_requested","promotion-decision")} className="rounded-lg border px-4 py-2">Request changes</button><button onClick={()=>void decide(proposal,"rejected","promotion-decision")} className="rounded-lg border border-red-500 px-4 py-2 text-red-600">Reject</button></div>}</article>)}</section></main>;
}
