"""Print a non-spending scenario from the last complete operator billing report."""
from datetime import datetime, timezone
import json

from app.coding.credit_planning import CreditPlanningInput, project_credit_scenario


def main():
    value = CreditPlanningInput(
        project_id="project-ee0c1944-144c-4364-ad4",
        evidence_kind="operator_report_unknown_capture_time",
        reported_remaining_paise=2_161_067,
        # A deliberately early planning assumption, NOT a capture timestamp.
        assumed_charge_anchor=datetime(2026, 10, 5, tzinfo=timezone.utc),
        hosting_stop_at=datetime(2026, 10, 19, 17, 24, 32, tzinfo=timezone.utc),
        # INR 500/day exceeds the earlier INR 13,850.56/30-day quote (~462/day).
        daily_allowance_paise=50_000,
        uncertainty_reserve_paise=50_000,
        source_reference="docs/DELHI_BILLING_RECONCILIATION_2026_10_07.md",
    )
    print(json.dumps(project_credit_scenario(value, now=datetime.now(timezone.utc)),
                     sort_keys=True))


if __name__ == "__main__":
    main()
