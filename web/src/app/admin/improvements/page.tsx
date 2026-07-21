"use client";

import {useCallback,useEffect,useState} from "react";
import Link from "next/link";
import {API,beginGoogleLogin,getToken} from "@/hooks/useChat";

type DeploymentEvidence={verified?:boolean;frontend_url?:string;deployment_url?:string};
type Proposal={proposal_key:string;title:string;proposal_type:string;sanitized_summary:string;status:string;severity:string;risk_level:string;affected_sessions:number;content_hash:string;exact_diff?:string;canary_status?:string;canary_metrics?:Record<string,unknown>;candidate_kind:"diagnosis"|"code"|"okf"|"config"|"prompt";candidate_state:"diagnosis_only"|"implementation_draft"|"validated_implementation"|"deployed_canary";candidate_version?:string;candidate_manifest?:Record<string,unknown>;validation_report?:Record<string,unknown>;deployment_evidence?:DeploymentEvidence};
type FeatureFlag={name:string;enabled:boolean;config:Record<string,unknown>;updated_by:string;updated_at:string};
type Notification={id:string;proposal_key:string;channel:string;event_type:string;status:string;external_reference?:string;error_message?:string;created_at:string};
type ImprovementOption={id:"A"|"B";title:string;explanation:string;change_scope:string[];acceptance_tests:string[];tradeoff:string;automation_eligible:boolean};
type FailureIncident={id:string;cluster_key:string;cluster_occurrences:number;request_excerpt:string;intent_kind:string;stage:string;category:string;component:string;title:string;summary:string;root_cause:string;contributing_factors:string[];breaking_point?:string;improvement_options:ImprovementOption[];recommended_option:"A"|"B";recommendation_reason:string;risk_level:string;created_at:string};
type FailureNotification={id:string;incident_id:string;cluster_key:string;title:string;channel:string;event_type:string;status:string;error_message?:string;created_at:string};
type CandidateBuild={id:string;proposal_key:string;title:string;mode:"single"|"multi_role";status:string;model_name:string;tokens_used:number;token_budget:number;error_message?:string;created_at:string};
type FailureTheme={id:string;theme_key:string;title:string;systemic_cause:string;strategy_options:{id:"A"|"B";title:string;explanation:string}[];recommended_option:"A"|"B";occurrence_count:number;status:string;clusters:{cluster_key:string;title:string;occurrence_count:number;category:string}[]};

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
  const [incidents,setIncidents]=useState<FailureIncident[]>([]);
  const [failureNotifications,setFailureNotifications]=useState<FailureNotification[]>([]);
  const [candidateBuilds,setCandidateBuilds]=useState<CandidateBuild[]>([]);
  const [themes,setThemes]=useState<FailureTheme[]>([]);
  const [proposalView,setProposalView]=useState<"active"|"history">("active");
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
    const [proposalResponse,notificationResponse,incidentResponse,buildResponse,themeResponse]=await Promise.all([
      fetch(`${API}/admin/improvements?view=${proposalView}`,{headers:headers()}),
      fetch(`${API}/admin/improvement-notifications`,{headers:headers()}),
      fetch(`${API}/admin/failure-incidents`,{headers:headers()}),
      fetch(`${API}/admin/candidate-builds`,{headers:headers()}),
      fetch(`${API}/admin/failure-themes`,{headers:headers()}),
    ]);
    await requireAuthorized(proposalResponse,"Unable to load proposals");
    const proposalData=await proposalResponse.json();
    setProposals(proposalData.proposals);
    await requireAuthorized(notificationResponse,"Unable to load the notification ledger");
    setNotifications((await notificationResponse.json()).notifications);
    await requireAuthorized(incidentResponse,"Unable to load the failure review inbox");
    const incidentData=await incidentResponse.json();
    setIncidents(incidentData.incidents);setFailureNotifications(incidentData.notifications);
    await requireAuthorized(buildResponse,"Unable to load candidate builds");
    setCandidateBuilds((await buildResponse.json()).builds);
    await requireAuthorized(themeResponse,"Unable to load architectural themes");
    setThemes((await themeResponse.json()).themes);
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
  },[loadFlags,proposalView]);
  const decide=async(proposal:Proposal,decision:"approved"|"rejected"|"changes_requested",stage:"canary-decision"|"activate-canary"|"promotion-decision"|"okf-publication-decision"="canary-decision")=>{
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
      body:JSON.stringify({enabled,config,confirmation:name==="failure_improvement_automation"&&enabled?"ENABLE FAILURE AUTO DRAFT":undefined}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Rollout update failed");}
    await loadFlags();
  };
  const decideIncident=async(incident:FailureIncident,decision:"choose_A"|"choose_B"|"acknowledged"|"ignored")=>{
    const response=await fetch(`${API}/admin/failure-incidents/${incident.id}/decision`,{
      method:"POST",headers:{...headers(),"Content-Type":"application/json"},
      body:JSON.stringify({decision}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Failure decision failed");}
    setMessage(decision.startsWith("choose_")?"Strategy selected. A Groq-only candidate build was queued. It cannot execute its code; trusted CI and human canary approval remain required.":"Failure review recorded.");
    await load();
  };
  const decideTheme=async(theme:FailureTheme,decision:"choose_A"|"choose_B")=>{
    const response=await fetch(`${API}/admin/failure-themes/${theme.id}/decision`,{
      method:"POST",headers:{...headers(),"Content-Type":"application/json"},
      body:JSON.stringify({decision}),
    });
    if(!response.ok){const data=await response.json();throw new Error(data.detail??"Theme decision failed");}
    setMessage("Architectural strategy selected. A sanitized cross-cluster candidate was dispatched; trusted CI and human rollout gates remain required.");
    await load();
  };
  const safely=(action:()=>Promise<void>)=>void action().catch(value=>setError(value instanceof Error?value.message:"Action failed"));
  const pilot=flags.find(item=>item.name==="pilot_cohorts");
  const newRag=flags.find(item=>item.name==="new_rag");
  const failureAutomation=flags.find(item=>item.name==="failure_improvement_automation");

  return <main className="mx-auto min-h-screen max-w-5xl bg-zinc-50 p-6 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50">
    <header className="mb-6 flex items-center justify-between"><div><h1 className="text-2xl font-semibold">Improvement review</h1><p className="text-zinc-500">Human approval is required before canary, promotion, and every external publication.</p></div><Link href="/" className="rounded-lg border px-4 py-2">Back to agent</Link></header>
    {error&&<p className="mb-3 rounded-xl bg-red-50 p-4 text-red-700">{error}</p>}
    {authRequired&&<button onClick={beginGoogleLogin} className="mb-4 rounded-lg bg-blue-600 px-4 py-2 text-white">Sign in with Google</button>}
    {message&&<p className="mb-3 rounded-xl bg-emerald-50 p-4 text-emerald-800">{message}</p>}
    {authorized&&<><section className="mb-6 rounded-xl border bg-white p-5 dark:bg-zinc-900"><h2 className="text-lg font-semibold">Pilot rollout controls</h2><p className="mt-1 text-sm text-zinc-500">Changes apply to new runs and are audited with your identity. Allowed users enter the pilot; denied users never do.</p><label className="mt-4 block text-sm">Pilot percentage<input type="number" min={0} max={100} value={pilotPercentage} onChange={event=>setPilotPercentage(Number(event.target.value))} className="mt-1 block w-32 rounded border bg-transparent p-2"/></label><label className="mt-3 block text-sm">Allowed pilot emails, one per line<textarea value={pilotUsers} onChange={event=>setPilotUsers(event.target.value)} className="mt-1 block min-h-24 w-full rounded border bg-transparent p-2"/></label><div className="mt-4 flex flex-wrap gap-2"><button onClick={()=>safely(()=>updateFlag("pilot_cohorts",true,{...(pilot?.config??{}),percentage:pilotPercentage,allowed_users:pilotUsers.split("\n").map(value=>value.trim()).filter(Boolean)}))} className="rounded-lg bg-blue-600 px-4 py-2 text-white">Enable/update pilot</button><button onClick={()=>safely(()=>updateFlag("pilot_cohorts",false,pilot?.config??{}))} className="rounded-lg border px-4 py-2">Disable pilot</button><button onClick={()=>safely(()=>updateFlag("new_rag",!newRag?.enabled,newRag?.config??{}))} className="rounded-lg border px-4 py-2">{newRag?.enabled?"Disable":"Enable"} new RAG</button><button onClick={()=>safely(()=>updateFlag("failure_improvement_automation",!failureAutomation?.enabled,{mode:failureAutomation?.enabled?"manual":"auto_draft",human_approval_required:true}))} className="rounded-lg border px-4 py-2">{failureAutomation?.enabled?"Disable":"Enable"} automatic diagnosis drafts</button></div><p className="mt-3 text-xs text-zinc-500">Pilot: {pilot?.enabled?"enabled":"disabled"} · New RAG: {newRag?.enabled?"enabled":"disabled"} · Failure drafts: {failureAutomation?.enabled?"automatic":"manual"} · Live RL: locked off. Draft automation never approves, canaries, promotes, or publishes.</p></section>
    <section className="mb-8 space-y-4"><div><h2 className="text-xl font-semibold">Failure review inbox</h2><p className="text-sm text-zinc-500">Every captured failure receives two bounded strategies. Select one for an implementation proposal, or acknowledge/ignore it. Human candidate, canary, promotion, and publication gates remain mandatory.</p></div>{incidents.length===0&&<p className="rounded-xl border bg-white p-5 dark:bg-zinc-900">No failures are awaiting review.</p>}{incidents.map(incident=><article key={incident.id} className="rounded-xl border bg-white p-5 dark:bg-zinc-900"><div className="flex flex-wrap gap-2 text-xs uppercase"><span className="rounded bg-red-100 px-2 py-1 text-red-800">{incident.risk_level}</span><span>{incident.stage} · {incident.category}</span><span>{incident.cluster_occurrences} in cluster</span></div><h3 className="mt-3 text-lg font-semibold">{incident.title}</h3><p className="mt-2">{incident.summary}</p><p className="mt-2 text-sm text-zinc-500">Intent: {incident.intent_kind.replaceAll("_"," ")} · Component: {incident.component} · Breaking point: {incident.breaking_point??"not recorded"}</p><p className="mt-2 rounded bg-zinc-100 p-3 text-sm dark:bg-zinc-800">Sanitized request: {incident.request_excerpt}</p><p className="mt-3 text-sm"><strong>Root cause:</strong> {incident.root_cause}</p><div className="mt-4 grid gap-3 md:grid-cols-2">{incident.improvement_options.map(option=><div key={option.id} className={`rounded-xl border p-4 ${incident.recommended_option===option.id?"border-blue-500":""}`}><h4 className="font-semibold">Option {option.id}: {option.title}</h4>{incident.recommended_option===option.id&&<p className="text-xs text-blue-600">Recommended — {incident.recommendation_reason}</p>}<p className="mt-2 text-sm">{option.explanation}</p><p className="mt-2 text-xs text-zinc-500">Tradeoff: {option.tradeoff}</p><button onClick={()=>safely(()=>decideIncident(incident,`choose_${option.id}` as "choose_A"|"choose_B"))} className="mt-3 rounded-lg bg-blue-600 px-3 py-2 text-sm text-white">Choose option {option.id}</button></div>)}</div><div className="mt-3 flex gap-2"><button onClick={()=>safely(()=>decideIncident(incident,"acknowledged"))} className="rounded border px-3 py-2 text-sm">Acknowledge</button><button onClick={()=>safely(()=>decideIncident(incident,"ignored"))} className="rounded border px-3 py-2 text-sm">Ignore</button></div></article>)}</section>
    <section className="mb-8 space-y-4"><div><h2 className="text-xl font-semibold">Architectural themes</h2><p className="text-sm text-zinc-500">Themes combine multiple exact failure clusters. A broad label alone cannot create a systemic change.</p></div>{themes.length===0&&<p className="rounded-xl border bg-white p-5 dark:bg-zinc-900">No cross-cluster theme has enough evidence yet.</p>}{themes.map(theme=><article key={theme.id} className="rounded-xl border bg-white p-5 dark:bg-zinc-900"><div className="text-xs uppercase">{theme.status} · {theme.occurrence_count} occurrences · {theme.clusters.length} clusters</div><h3 className="mt-2 font-semibold">{theme.title}</h3><p className="mt-2 text-sm">{theme.systemic_cause}</p><div className="mt-3 grid gap-2 md:grid-cols-2">{theme.strategy_options.map(option=><div key={option.id} className={`rounded border p-3 ${theme.recommended_option===option.id?"border-blue-500":""}`}><strong>Option {option.id}: {option.title}</strong><p className="mt-1 text-sm text-zinc-500">{option.explanation}</p><button onClick={()=>safely(()=>decideTheme(theme,`choose_${option.id}` as "choose_A"|"choose_B"))} className="mt-3 rounded-lg bg-blue-600 px-3 py-2 text-sm text-white">Choose option {option.id}</button></div>)}</div></article>)}</section>
    <section className="mb-8 rounded-xl border bg-white p-5 dark:bg-zinc-900"><h2 className="text-xl font-semibold">Candidate builder</h2><p className="text-sm text-zinc-500">Groq generates frozen files with bounded repository tools. Generated code is never run while the Groq key is present; trusted no-secret CI validates it.</p><div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">Proposal</th><th className="p-2">Mode/model</th><th className="p-2">Status</th><th className="p-2">Tokens</th><th className="p-2">Error</th></tr></thead><tbody>{candidateBuilds.map(build=><tr key={build.id} className="border-t"><td className="p-2">{build.proposal_key}</td><td className="p-2">{build.mode} · {build.model_name}</td><td className="p-2">{build.status.replaceAll("_"," ")}</td><td className="p-2">{build.tokens_used}/{build.token_budget}</td><td className="p-2">{build.error_message??"—"}</td></tr>)}</tbody></table></div></section>
    <div className="mb-4 flex gap-2"><button onClick={()=>setProposalView("active")} className={`rounded border px-3 py-2 ${proposalView==="active"?"bg-blue-600 text-white":""}`}>Active proposals</button><button onClick={()=>setProposalView("history")} className={`rounded border px-3 py-2 ${proposalView==="history"?"bg-blue-600 text-white":""}`}>History</button></div>
    {proposals.some(proposal=>proposal.status==="awaiting_review"&&proposal.candidate_manifest?.okf_bundle_hash&&proposal.candidate_manifest?.okf_approval_status!=="approved")&&<section className="mb-4 rounded-xl border border-violet-300 bg-violet-50 p-5 text-violet-950"><h2 className="font-semibold">Trusted OKF approval</h2><p className="mt-1 text-sm">Knowledge overlays are approved separately from their related code. Approval only makes the frozen bundle eligible for canary; it does not publish or activate it.</p><div className="mt-3 space-y-2">{proposals.filter(proposal=>proposal.status==="awaiting_review"&&proposal.candidate_manifest?.okf_bundle_hash&&proposal.candidate_manifest?.okf_approval_status!=="approved").map(proposal=><div key={`okf-${proposal.proposal_key}`} className="flex items-center justify-between rounded border bg-white p-3"><span>{proposal.title}</span><button disabled={proposal.candidate_state!=="validated_implementation"} onClick={()=>safely(()=>decide(proposal,"approved","okf-publication-decision"))} className="rounded bg-violet-700 px-3 py-2 text-sm text-white disabled:opacity-40">Approve frozen OKF overlay</button></div>)}</div></section>}
    <section className="space-y-4">{proposals.length===0&&!error&&<p className="rounded-xl border bg-white p-5 dark:bg-zinc-900">No proposals in this view.</p>}{proposals.map(proposal=>{const ready=proposal.candidate_state==="validated_implementation";const draft=proposal.candidate_state==="implementation_draft";const deployed=proposal.deployment_evidence?.verified===true;return <article key={proposal.proposal_key} className="rounded-xl border bg-white p-5 shadow-sm dark:bg-zinc-900"><div className="flex flex-wrap items-center gap-2 text-xs uppercase"><span className="rounded bg-zinc-200 px-2 py-1 dark:bg-zinc-700">{proposal.proposal_type}</span><span className="rounded bg-amber-100 px-2 py-1 text-amber-900">{proposal.severity}</span><span>{proposal.status.replaceAll("_"," ")}</span><span className={ready?"rounded bg-emerald-100 px-2 py-1 text-emerald-900":"rounded bg-amber-100 px-2 py-1 text-amber-900"}>{proposal.candidate_state.replaceAll("_"," ")}</span>{proposal.canary_status&&<span>canary: {proposal.canary_status}</span>}</div><h2 className="mt-3 text-lg font-semibold">{proposal.title}</h2><p className="mt-2 text-zinc-600 dark:text-zinc-300">{proposal.sanitized_summary}</p><p className="mt-2 text-sm">Affected sessions: {proposal.affected_sessions} · Risk: {proposal.risk_level}</p>{proposal.candidate_state==="diagnosis_only"&&<p className="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">Diagnosis recorded. Select an option to queue a concrete Groq-only candidate build.</p>}{draft&&<p className="mt-3 rounded-lg bg-blue-50 p-3 text-sm text-blue-900">Implementation draft generated. Publishing its draft PR requires your confirmation; then trusted CI validates the exact files and hashes.</p>}{ready&&<p className="mt-3 text-sm">Candidate: <code>{proposal.candidate_version}</code> · Trusted CI: passed · Deployment: {deployed?"verified":"not yet verified"}</p>}{proposal.deployment_evidence?.frontend_url&&<p className="mt-2 text-sm"><a className="text-blue-700 underline" href={proposal.deployment_evidence.frontend_url} target="_blank" rel="noreferrer">Open immutable candidate frontend preview</a></p>}{proposal.status==="production_pending"&&<p className="mt-3 rounded-lg bg-blue-50 p-3 text-sm text-blue-900">The approved PR is merged. Publication remains pending until both Railway API and worker run the exact merge commit, pass health checks, and submit trusted deployment evidence.</p>}{proposal.exact_diff&&<pre className="mt-3 max-h-64 overflow-auto rounded bg-zinc-950 p-3 text-xs text-zinc-100">{proposal.exact_diff}</pre>}{proposal.status==="awaiting_review"&&<div className="mt-4 flex flex-wrap gap-2">{draft&&<button onClick={()=>safely(()=>publish(proposal,"github"))} className="rounded-lg bg-zinc-950 px-4 py-2 text-white dark:bg-white dark:text-zinc-950">Publish frozen draft PR</button>}<button disabled={!ready} title={!ready?"Trusted CI must validate the frozen implementation first":undefined} onClick={()=>safely(()=>decide(proposal,"approved"))} className="rounded-lg bg-blue-600 px-4 py-2 text-white disabled:cursor-not-allowed disabled:opacity-40">Approve and deploy isolated canary</button><button onClick={()=>safely(()=>decide(proposal,"changes_requested"))} className="rounded-lg border px-4 py-2">Request changes</button><button onClick={()=>safely(()=>decide(proposal,"rejected"))} className="rounded-lg border border-red-500 px-4 py-2 text-red-600">Reject</button><button onClick={()=>safely(()=>publish(proposal,"email"))} className="rounded-lg border px-4 py-2">Send sanitized review email</button></div>}{proposal.status==="approved_for_canary"&&<div className="mt-4"><p className="mb-2 text-sm text-zinc-500">The deployment workflow was dispatched. Activation stays blocked until its signed evidence and smoke checks arrive.</p><button disabled={!deployed} onClick={()=>safely(()=>decide(proposal,"approved","activate-canary"))} className="rounded-lg bg-blue-600 px-4 py-2 text-white disabled:cursor-not-allowed disabled:opacity-40">Activate version-pinned traffic</button></div>}{proposal.status==="awaiting_promotion"&&<div className="mt-4 flex flex-wrap gap-2"><button onClick={()=>safely(()=>decide(proposal,"approved","promotion-decision"))} className="rounded-lg bg-emerald-600 px-4 py-2 text-white">Promote, merge, and deploy production</button><button onClick={()=>safely(()=>decide(proposal,"changes_requested","promotion-decision"))} className="rounded-lg border px-4 py-2">Request changes</button><button onClick={()=>safely(()=>decide(proposal,"rejected","promotion-decision"))} className="rounded-lg border border-red-500 px-4 py-2 text-red-600">Reject</button></div>}</article>})}</section>
    <section className="mt-8 rounded-xl border bg-white p-5 dark:bg-zinc-900"><h2 className="text-lg font-semibold">Failure notification ledger</h2><p className="text-sm text-zinc-500">Every incident is immediately visible to Admin and Grafana. Email/GitHub require separate configuration and explicit approval.</p><div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">Failure</th><th className="p-2">Channel</th><th className="p-2">Event</th><th className="p-2">Status</th><th className="p-2">Reference/error</th></tr></thead><tbody>{failureNotifications.slice(0,100).map(item=><tr key={item.id} className="border-t"><td className="p-2">{item.title}</td><td className="p-2">{item.channel}</td><td className="p-2">{item.event_type}</td><td className="p-2">{item.status}</td><td className="p-2">{item.error_message||"—"}</td></tr>)}</tbody></table></div></section>
    <section className="mt-8 rounded-xl border bg-white p-5 dark:bg-zinc-900"><h2 className="text-lg font-semibold">Proposal notification ledger</h2><p className="text-sm text-zinc-500">Admin/Grafana are internal. Email/GitHub remain skipped until explicitly confirmed and configured.</p><div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr><th className="p-2">Proposal</th><th className="p-2">Channel</th><th className="p-2">Event</th><th className="p-2">Status</th><th className="p-2">Reference/error</th></tr></thead><tbody>{notifications.slice(0,100).map(item=><tr key={item.id} className="border-t"><td className="p-2">{item.proposal_key}</td><td className="p-2">{item.channel}</td><td className="p-2">{item.event_type}</td><td className="p-2">{item.status}</td><td className="p-2">{item.external_reference?<a className="underline" href={item.external_reference} target="_blank" rel="noreferrer">Open</a>:item.error_message||"—"}</td></tr>)}</tbody></table></div></section></>}
  </main>;
}
