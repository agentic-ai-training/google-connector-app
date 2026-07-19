def completion_from_steps(steps: list[dict]) -> dict:
    total = sum(float(step.get("weight") or 1) for step in steps) or 1
    completed = sum(
        float(step.get("weight") or 1)
        for step in steps if step.get("status") == "completed"
    )
    technical = round(completed / total * 100, 2)
    functional_weight = sum(
        float(step.get("weight") or 1) for step in steps
        if step.get("status") == "completed" and not step.get("error_message")
    )
    visible_weight = sum(
        float(step.get("weight") or 1) for step in steps
        if step.get("status") == "completed" and bool((step.get("output_data") or {}).get("output"))
    )
    failed_writes = sum(
        1 for step in steps
        if step.get("status") == "failed" and not step.get("read_only", True)
    )
    return {
        "technical_completion": technical,
        "functional_completion": round(functional_weight / total * 100, 2),
        "user_visible_completion": round(visible_weight / total * 100, 2),
        "side_effect_integrity": max(0.0, 100.0 - failed_writes * 25.0),
    }


def build_incident(steps: list[dict], error_category: str, error_message: str) -> dict:
    completed_steps = [step for step in steps if step.get("status") == "completed"]
    completed = [step["title"] for step in completed_steps]
    failed = next((step for step in steps if step.get("status") == "failed"), None)
    pending = [step["title"] for step in steps if step.get("status") == "pending"]
    return {
        "completed": completed,
        "last_success": completed_steps[-1]["title"] if completed_steps else None,
        "breaking_point": failed.get("title") if failed else "Run execution",
        "first_incomplete": failed.get("title") if failed else (pending[0] if pending else None),
        "primary_cause": error_category,
        "contributing_factors": [factor for factor in (
            "Dependent steps were not executed" if pending else None,
            "A high-risk write may require artifact review"
            if failed and not failed.get("read_only", True) else None,
        ) if factor],
        "error": error_message,
        "recoverable": error_category in {"rate_limit", "network", "worker"},
        "evidence": [str(failed.get("id"))] if failed else [],
        "pending": pending,
    }
