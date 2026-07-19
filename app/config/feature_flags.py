import hashlib
import json


def cohort_selected(user_id: str, config: dict) -> bool:
    allowed = {str(value).lower() for value in config.get("allowed_users", [])}
    denied = {str(value).lower() for value in config.get("denied_users", [])}
    identity = user_id.lower()
    if identity in denied:
        return False
    if identity in allowed:
        return True
    percentage = max(0, min(int(config.get("percentage", 0)), 100))
    bucket = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) % 100
    return bucket < percentage


async def get_feature_flag(pool, name: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM feature_flags WHERE name=$1", name)
    if not row:
        return None
    value = dict(row)
    if isinstance(value.get("config"), str):
        value["config"] = json.loads(value["config"])
    return value


async def feature_enabled(pool, name: str, user_id: str | None = None) -> bool:
    flag = await get_feature_flag(pool, name)
    if not flag or not flag["enabled"]:
        return False
    config = flag.get("config") or {}
    if user_id and any(key in config for key in ("allowed_users", "denied_users", "percentage")):
        return cohort_selected(user_id, config)
    return True
