import {NextResponse} from "next/server";

export const dynamic="force-dynamic";

export async function GET(){
  return NextResponse.json({
    status:"ok",
    deployment_version:process.env.DEPLOYMENT_VERSION??"local",
    executor_role:process.env.CANDIDATE_FRONTEND_ROLE??"control",
  },{headers:{"Cache-Control":"no-store"}});
}
