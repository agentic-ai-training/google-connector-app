"use client";
import {useState} from "react";
export type Message={role:"user"|"assistant";content:string};
const API=process.env.NEXT_PUBLIC_API_URL??"http://localhost:8000";
async function auth(){
  const cached=localStorage.getItem("agent_token"); if(cached)return cached;
  const response=await fetch(`${API}/auth/token`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:"web-user@local"})});
  const token=(await response.json()).access_token; localStorage.setItem("agent_token",token); return token;
}
export function useChat(sessionId:string){
  const [messages,setMessages]=useState<Message[]>([]); const [streaming,setStreaming]=useState(false); const [error,setError]=useState("");
  const sendMessage=async(content:string)=>{setError("");setStreaming(true);setMessages(m=>[...m,{role:"user",content},{role:"assistant",content:""}]);
    try{const token=await auth(); const response=await fetch(`${API}/chat`,{method:"POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`},body:JSON.stringify({message:content,session_id:sessionId})});
      if(!response.ok||!response.body)throw new Error(`Request failed (${response.status})`);
      const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";
      while(true){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const events=buffer.split("\n\n");buffer=events.pop()??"";
        for(const event of events){if(!event.startsWith("data: "))continue;const data=JSON.parse(event.slice(6));if(!data.done)setMessages(m=>m.map((x,i)=>i===m.length-1?{...x,content:x.content+data.token}:x));}}
    }catch(e){setError(e instanceof Error?e.message:"Unknown error");}finally{setStreaming(false);}};
  return{messages,sendMessage,streaming,error};
}
export async function sendFeedback(sessionId:string,rating:number){const token=await auth();await fetch(`${API}/feedback`,{method:"POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${token}`},body:JSON.stringify({session_id:sessionId,rating})});}
