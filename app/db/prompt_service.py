import random
import json
from uuid import UUID


async def _experiment_arm(conn, experiment) -> str:
    if experiment["selection_policy"] != "thompson":
        return (
            "variant" if random.random() < float(experiment["traffic_split"])
            else "control"
        )
    rows = await conn.fetch(
        """SELECT pa.arm,count(pm.id) AS total,
                  count(pm.id) FILTER(WHERE pm.task_completed AND NOT pm.error_occurred
                                      AND coalesce(pm.user_rating,1)>0) AS successes
           FROM prompt_assignments pa LEFT JOIN prompt_metrics pm
             ON pm.assignment_id=pa.id
           WHERE pa.experiment_id=$1 GROUP BY pa.arm""",
        experiment["id"],
    )
    observations = {row["arm"]: row for row in rows}
    samples = {}
    for arm in ("control", "variant"):
        total = int(observations.get(arm, {}).get("total", 0))
        successes = int(observations.get(arm, {}).get("successes", 0))
        samples[arm] = random.betavariate(1 + successes, 1 + total - successes)
    return max(samples, key=samples.get)


async def get_prompt(
    name, session_id, model_target="groq/llama-3.3-70b", pool=None,
    risk_level="low",
):
    from app.db.connection import get_pool

    pool = pool or await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        experiment = await conn.fetchrow(
            """SELECT * FROM prompt_experiments
               WHERE prompt_name=$1 AND status='running' AND validated AND $2='low'
               ORDER BY started_at DESC LIMIT 1""",
            name, risk_level,
        )
        if experiment:
            assigned = await conn.fetchrow(
                """SELECT p.*, pa.id AS assignment_id
                   FROM prompt_assignments pa
                   JOIN prompts p ON p.id=pa.prompt_id
                   WHERE pa.session_id=$1 AND pa.experiment_id=$2""",
                session_id,
                experiment["id"],
            )
            if assigned:
                prompt = dict(assigned)
                assignment_id = prompt.pop("assignment_id")
                return prompt, assignment_id
            arm = await _experiment_arm(conn, experiment)
            prompt_id = experiment[f"{arm}_id"]
            assignment_id = await conn.fetchval(
                """INSERT INTO prompt_assignments
                   (session_id,experiment_id,prompt_id,arm)
                   VALUES($1,$2,$3,$4) RETURNING id""",
                session_id,
                experiment["id"],
                prompt_id,
                arm,
            )
            prompt = await conn.fetchrow("SELECT * FROM prompts WHERE id=$1", prompt_id)
            return dict(prompt), assignment_id
        row = await conn.fetchrow(
            """SELECT * FROM prompts
               WHERE name=$1 AND model_target=$2 AND is_active
               ORDER BY version DESC LIMIT 1""",
            name,
            model_target,
        )
        return (dict(row) if row else None), None


async def record_metric(pool=None, **values):
    from app.db.connection import get_pool

    pool = pool or await get_pool()
    allowed = {
        "assignment_id", "prompt_id", "session_id", "llm_latency_ms",
        "total_latency_ms", "input_tokens", "output_tokens", "faithfulness",
        "answer_relevancy", "context_recall", "user_rating", "task_completed",
        "error_occurred", "error_type",
    }
    columns = [key for key in values if key in allowed]
    if not columns:
        raise ValueError("At least one prompt metric value is required")
    async with pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO prompt_metrics({','.join(columns)}) "  # nosec B608
            f"VALUES({','.join(f'${i + 1}' for i in range(len(columns)))})",
            *(values[key] for key in columns),
        )


async def create_experiment(name, prompt_name, control_id, variant_id,
                            traffic_split=.5, notes=None, selection_policy="ab",
                            pool=None):
    from app.db.connection import get_pool

    if not 0 <= traffic_split <= 1:
        raise ValueError("traffic_split must be between 0 and 1")
    if selection_policy not in {"ab", "thompson"}:
        raise ValueError("selection_policy must be ab or thompson")
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        prompts = await conn.fetch(
            "SELECT id,name FROM prompts WHERE id=ANY($1::uuid[])",
            [UUID(str(control_id)), UUID(str(variant_id))],
        )
        if len(prompts) != 2 or any(row["name"] != prompt_name for row in prompts):
            raise ValueError("Both prompts must exist and match prompt_name")
        row = await conn.fetchrow(
            """INSERT INTO prompt_experiments
               (name,prompt_name,control_id,variant_id,traffic_split,notes,
                selection_policy,status)
               VALUES($1,$2,$3,$4,$5,$6,$7,'draft') RETURNING *""",
            name,
            prompt_name,
            UUID(str(control_id)),
            UUID(str(variant_id)),
            traffic_split,
            notes,
            selection_policy,
        )
        return dict(row)


async def activate_experiment(name, actor, evidence, pool=None):
    """Human-gated activation; runtime assignment remains low-risk only."""
    from app.db.connection import get_pool

    if not evidence or not evidence.get("suite_version"):
        raise ValueError("versioned offline evaluation evidence is required")
    pool = pool or await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE prompt_experiments SET status='running',validated=TRUE,
                 validated_by=$2,validated_at=now(),validation_evidence=$3::jsonb
               WHERE name=$1 AND status='draft' RETURNING *""",
            name, actor, json.dumps(evidence),
        )
    if not row:
        raise ValueError("draft experiment not found")
    return dict(row)


async def conclude_experiment(name, winner, pool=None):
    from app.db.connection import get_pool

    pool = pool or await get_pool()
    if winner not in {"control", "variant"}:
        raise ValueError("winner must be control or variant")
    async with pool.acquire() as conn, conn.transaction():
        experiment = await conn.fetchrow(
            """UPDATE prompt_experiments
               SET status='concluded',winner=$2,ended_at=now()
               WHERE name=$1 AND status='running' RETURNING *""",
            name,
            winner,
        )
        if not experiment:
            raise ValueError("running experiment not found")
        winning_id = experiment[f"{winner}_id"]
        prompt = await conn.fetchrow("SELECT * FROM prompts WHERE id=$1", winning_id)
        await conn.execute(
            "UPDATE prompts SET is_active=FALSE WHERE name=$1 AND model_target=$2",
            prompt["name"],
            prompt["model_target"],
        )
        await conn.execute("UPDATE prompts SET is_active=TRUE WHERE id=$1", winning_id)
        return dict(experiment)
