import random

async def get_prompt(name, session_id, model_target="groq/llama-3.3-70b", pool=None):
    from app.db.connection import get_pool
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        experiment = await conn.fetchrow("SELECT * FROM prompt_experiments WHERE prompt_name=$1 AND status='running' ORDER BY started_at DESC LIMIT 1", name)
        if experiment:
            assignment = await conn.fetchrow("SELECT pa.*,p.* FROM prompt_assignments pa JOIN prompts p ON p.id=pa.prompt_id WHERE pa.session_id=$1 AND pa.experiment_id=$2", session_id, experiment["id"])
            if not assignment:
                arm = "variant" if random.random() < experiment["traffic_split"] else "control"
                prompt_id = experiment[f"{arm}_id"]
                assignment = await conn.fetchrow("INSERT INTO prompt_assignments(session_id,experiment_id,prompt_id,arm) VALUES($1,$2,$3,$4) RETURNING *", session_id, experiment["id"], prompt_id, arm)
                prompt = await conn.fetchrow("SELECT * FROM prompts WHERE id=$1", prompt_id)
                return dict(prompt), assignment["id"]
            return dict(assignment), assignment["id"]
        row = await conn.fetchrow("SELECT * FROM prompts WHERE name=$1 AND model_target=$2 AND is_active ORDER BY version DESC LIMIT 1", name, model_target)
        return (dict(row) if row else None), None

async def record_metric(pool=None, **values):
    from app.db.connection import get_pool
    pool = pool or await get_pool()
    columns = [k for k in values if k in {"assignment_id","prompt_id","session_id","llm_latency_ms","total_latency_ms","input_tokens","output_tokens","faithfulness","answer_relevancy","context_recall","user_rating","task_completed","error_occurred","error_type"}]
    async with pool.acquire() as conn:
        await conn.execute(f"INSERT INTO prompt_metrics({','.join(columns)}) VALUES({','.join(f'${i+1}' for i in range(len(columns)))})", *(values[k] for k in columns))

async def create_experiment(name, prompt_name, control_id, variant_id, traffic_split=.5, notes=None, pool=None):
    from app.db.connection import get_pool
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        return dict(await conn.fetchrow("INSERT INTO prompt_experiments(name,prompt_name,control_id,variant_id,traffic_split,notes) VALUES($1,$2,$3,$4,$5,$6) RETURNING *", name,prompt_name,control_id,variant_id,traffic_split,notes))

async def conclude_experiment(name, winner, pool=None):
    from app.db.connection import get_pool
    pool = pool or await get_pool()
    if winner not in {"control", "variant"}:
        raise ValueError("winner must be control or variant")
    async with pool.acquire() as conn, conn.transaction():
        exp = await conn.fetchrow("UPDATE prompt_experiments SET status='concluded',winner=$2,ended_at=now() WHERE name=$1 RETURNING *",name,winner)
        if not exp: raise ValueError("experiment not found")
        winning_id = exp[f"{winner}_id"]
        prompt = await conn.fetchrow("SELECT * FROM prompts WHERE id=$1",winning_id)
        await conn.execute("UPDATE prompts SET is_active=FALSE WHERE name=$1 AND model_target=$2",prompt["name"],prompt["model_target"])
        await conn.execute("UPDATE prompts SET is_active=TRUE WHERE id=$1",winning_id)
        return dict(exp)
