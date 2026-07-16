"use client";

import {useState} from "react";

export type Message={role:"user"|"assistant";content:string};
export type CurrentUser={email:string;google_connected:boolean;missing_scopes?:string[]};
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
  const sendMessage=async(content:string)=>{
    setError("");setStreaming(true);
    setMessages(m=>[...m,{role:"user",content},{role:"assistant",content:""}]);
    try{
      const token=getToken();
      if(!token)throw new Error("Sign in with Google first");
      const response=await fetch(`${API}/chat`,{method:"POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`},body:JSON.stringify({message:content,session_id:sessionId})});
      if(!response.ok||!response.body){
        const detail=await response.json().catch(()=>({detail:`Request failed (${response.status})`}));
        throw new Error(detail.detail??`Request failed (${response.status})`);
      }
      const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";
      while(true){
        const {done,value}=await reader.read();if(done)break;
        buffer+=decoder.decode(value,{stream:true});const events=buffer.split("\n\n");buffer=events.pop()??"";
        for(const event of events){
          if(!event.startsWith("data: "))continue;
          const data=JSON.parse(event.slice(6));
          if(!data.done)setMessages(m=>m.map((x,i)=>i===m.length-1?{...x,content:x.content+data.token}:x));
        }
      }
    }catch(e){setError(e instanceof Error?e.message:"Unknown error");}
    finally{setStreaming(false);}
  };
  return{messages,sendMessage,streaming,error};
}

export async function sendFeedback(sessionId:string,rating:number){
  const token=getToken();
  if(!token)throw new Error("Sign in with Google first");
  await fetch(`${API}/feedback`,{method:"POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`},body:JSON.stringify({session_id:sessionId,rating})});
}
