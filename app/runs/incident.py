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
    uncertain_writes = 0
    for step in steps:
        semantic = (step.get("input_data") or {}).get(
            "semantic_authorization", {}
        )
        if (
            step.get("status") == "completed"
            and not step.get("read_only", True)
            and semantic.get("authorized") is False
        ):
            # Provider success cannot make an unintended external mutation safe.
            uncertain_writes += 1
            continue
        if step.get("status") != "failed" or step.get("read_only", True):
            continue
        if step.get("error_category") == "worker_reconciliation":
            # A lost worker lease has no reliable response boundary. Preserve
            # the existing fail-closed signal until explicit reconciliation.
            uncertain_writes += 4
            continue
        executions = (step.get("output_data") or {}).get("tool_executions") or []
        if executions:
            uncertain_writes += 1
    return {
        "technical_completion": technical,
        "functional_completion": round(functional_weight / total * 100, 2),
        "user_visible_completion": round(visible_weight / total * 100, 2),
        "side_effect_integrity": max(0.0, 100.0 - uncertain_writes * 25.0),
    }


def build_incident(steps: list[dict], error_category: str, error_message: str) -> dict:
    completed_steps = [step for step in steps if step.get("status") == "completed"]
    completed = [step["title"] for step in completed_steps]
    failed = next((step for step in steps if step.get("status") == "failed"), None)
    pending = [step["title"] for step in steps if step.get("status") == "pending"]
    unintended_side_effects = [
        {
            "step_id": str(step.get("id") or ""),
            "title": str(step.get("title") or ""),
            "service": str(step.get("service") or ""),
            "operation": str(step.get("operation") or ""),
            "verified": True,
        }
        for step in completed_steps
        if (
            not step.get("read_only", True)
            and (step.get("input_data") or {})
            .get("semantic_authorization", {})
            .get("authorized") is False
        )
    ]
    failed_write_may_exist = bool(
        failed
        and not failed.get("read_only", True)
        and (
            (failed.get("output_data") or {}).get("tool_executions")
            or failed.get("error_category") == "worker_reconciliation"
        )
    )
    return {
        "completed": completed,
        "last_success": completed_steps[-1]["title"] if completed_steps else None,
        "breaking_point": failed.get("title") if failed else "Run execution",
        "first_incomplete": failed.get("title") if failed else (pending[0] if pending else None),
        "primary_cause": error_category,
        "contributing_factors": [factor for factor in (
            "Dependent steps were not executed" if pending else None,
            "A high-risk write may require artifact review"
            if failed_write_may_exist else None,
            "A verified external write did not match the user's requested delivery channel"
            if unintended_side_effects else None,
        ) if factor],
        "error": error_message,
        "recoverable": error_category in {"rate_limit", "network", "worker"},
        "evidence": [str(failed.get("id"))] if failed else [],
        "pending": pending,
        "unintended_side_effects": unintended_side_effects,
    }
