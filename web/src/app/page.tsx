"use client";

import {FormEvent,useEffect,useState} from "react";
import FeedbackButtons from "@/components/FeedbackButtons";
import {
  beginGoogleLogin,currentUser,disconnectGoogle,getToken,useChat,CurrentUser,
} from "@/hooks/useChat";

export default function Home(){
  const [session,setSession]=useState("");
  const [input,setInput]=useState("");
  const [user,setUser]=useState<CurrentUser|null>(null);
  const [authLoading,setAuthLoading]=useState(true);
  const [authError,setAuthError]=useState("");
  useEffect(()=>{
    let active=true;
    const fragment=new URLSearchParams(window.location.hash.slice(1));
    const returnedToken=fragment.get("access_token");
    const returnedError=fragment.get("oauth_error");
    if(returnedToken){localStorage.setItem("agent_token",returnedToken);history.replaceState(null,"",window.location.pathname);}
    if(returnedError){localStorage.removeItem("agent_token");queueMicrotask(()=>{if(active){setAuthError(returnedError);setAuthLoading(false)}});history.replaceState(null,"",window.location.pathname);}
    let id=localStorage.getItem("agent_session");
    if(!id){id=crypto.randomUUID();localStorage.setItem("agent_session",id);}
    queueMicrotask(()=>{if(active)setSession(id)});
    if(!getToken()){queueMicrotask(()=>{if(active)setAuthLoading(false)});return()=>{active=false};}
    currentUser().then(value=>{
      if(!value.google_connected){localStorage.removeItem("agent_token");throw new Error(value.missing_scopes?.length?"Reconnect Google once to approve the newly added Workspace permissions":"Connect your Google account to continue");}
      if(active)setUser(value);
    }).catch(error=>{
      localStorage.removeItem("agent_token");if(active)setAuthError(error instanceof Error?error.message:"Sign-in failed");
    }).finally(()=>{if(active)setAuthLoading(false)});
    return()=>{active=false};
  },[]);
  const {messages,sendMessage,streaming,error}=useChat(session);
  const submit=(e:FormEvent)=>{e.preventDefault();const value=input.trim();if(value&&!streaming&&user){setInput("");void sendMessage(value)}};
  const disconnect=async()=>{await disconnectGoogle();setUser(null);};
  if(authLoading)return <main className="grid h-screen place-items-center">Checking your session…</main>;
  if(!user)return <main className="grid h-screen place-items-center bg-zinc-50 p-6 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50"><section className="max-w-lg rounded-2xl border bg-white p-8 text-center shadow-sm dark:bg-zinc-900"><h1 className="text-2xl font-semibold">Google Workspace Agent</h1><p className="mt-3 text-zinc-600 dark:text-zinc-300">Sign in and grant access to use your own Gmail, Calendar, Drive, Docs, Sheets, Tasks, Chat, Contacts, and Google Meet.</p>{authError&&<p className="mt-3 text-red-500">{authError}</p>}<button onClick={beginGoogleLogin} className="mt-6 rounded-xl bg-blue-600 px-5 py-3 font-medium text-white">Sign in with Google</button></section></main>;
  return <main className="mx-auto flex h-screen max-w-4xl flex-col bg-zinc-50 text-zinc-950 dark:bg-zinc-950 dark:text-zinc-50"><header className="flex items-center justify-between border-b p-4"><span className="text-xl font-semibold">Google Workspace Agent</span><span className="flex items-center gap-3 text-sm"><span>{user.email}</span><button onClick={()=>void disconnect()} className="rounded-lg border px-3 py-2">Disconnect Google</button></span></header><section className="flex-1 space-y-4 overflow-y-auto p-4">{messages.length===0&&<p className="text-center text-zinc-500">Ask me to work with Gmail, Calendar, Drive, Docs, Sheets, Tasks, Chat, Contacts, or Google Meet.</p>}{messages.map((m,i)=><div key={i} className={`flex ${m.role==="user"?"justify-end":"justify-start"}`}><div className={`max-w-[80%] rounded-2xl p-3 ${m.role==="user"?"bg-blue-600 text-white":"bg-zinc-200 dark:bg-zinc-800"}`}><p className="whitespace-pre-wrap">{m.content||"…"}</p>{m.role==="assistant"&&!streaming&&<FeedbackButtons sessionId={session}/>}</div></div>)}{error&&<p className="text-red-500">{error}</p>}</section><form onSubmit={submit} className="flex gap-2 border-t p-4"><input aria-label="Message" className="flex-1 rounded-xl border bg-transparent p-3" value={input} onChange={e=>setInput(e.target.value)} placeholder="Type a message…"/><button className="rounded-xl bg-blue-600 px-5 text-white disabled:opacity-50" disabled={!session||streaming}>{streaming?"Working…":"Send"}</button></form></main>;
}
