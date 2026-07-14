from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.connection import get_pool
from app.db.prompt_service import create_experiment, conclude_experiment
router=APIRouter(prefix="/admin")
class ExperimentIn(BaseModel):
    name:str; prompt_name:str; control_id:str; variant_id:str; traffic_split:float=.5; notes:str|None=None
class ConcludeIn(BaseModel): winner:str
class PromptIn(BaseModel):
    name:str; content:str; model_target:str="groq/llama-3.3-70b"; temperature:float=.3; max_tokens:int=1000; notes:str|None=None
@router.get("/experiments/{name}/summary")
async def summary(name:str):
    pool=await get_pool()
    async with pool.acquire() as conn: rows=await conn.fetch("SELECT * FROM experiment_summary WHERE experiment_name=$1",name)
    return {"summary":[dict(r) for r in rows]}
@router.post("/experiments")
async def create(body:ExperimentIn): return await create_experiment(**body.model_dump())
@router.post("/experiments/{name}/conclude")
async def conclude(name:str,body:ConcludeIn): return await conclude_experiment(name,body.winner)
@router.get("/prompts")
async def prompts():
    pool=await get_pool()
    async with pool.acquire() as conn: rows=await conn.fetch("SELECT * FROM prompts ORDER BY name,version")
    return {"prompts":[dict(r) for r in rows]}
@router.post("/prompts")
async def add_prompt(body:PromptIn):
    pool=await get_pool()
    async with pool.acquire() as conn:
        row=await conn.fetchrow("INSERT INTO prompts(name,version,content,model_target,temperature,max_tokens,notes) SELECT $1,coalesce(max(version),0)+1,$2,$3,$4,$5,$6 FROM prompts WHERE name=$1 RETURNING *",body.name,body.content,body.model_target,body.temperature,body.max_tokens,body.notes)
    return dict(row)
