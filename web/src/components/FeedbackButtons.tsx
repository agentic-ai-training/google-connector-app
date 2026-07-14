"use client";
import {useState} from "react";import {sendFeedback} from "@/hooks/useChat";
export default function FeedbackButtons({sessionId}:{sessionId:string}){const [sent,setSent]=useState(false);if(sent)return <small>Thanks for the feedback.</small>;return <span className="flex gap-2"><button aria-label="thumbs up" onClick={()=>sendFeedback(sessionId,1).then(()=>setSent(true))}>👍</button><button aria-label="thumbs down" onClick={()=>sendFeedback(sessionId,-1).then(()=>setSent(true))}>👎</button></span>}
