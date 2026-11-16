"""Offline resource plan: no authority to prepare, connect, execute or clean up."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    database: str
    preparation_role: str
    test_role: str


def plan_resources(run_id: UUID, *, excluded_resources: frozenset[str]) -> ResourcePlan:
    """Build deterministic per-run names; caller must supply a verified inventory.

    No existence, uniqueness, ownership or privilege checks are possible offline.
    The returned names do not grant permission to use or create resources.
    """
    if (type(run_id) is not UUID or run_id.version != 4
            or type(excluded_resources) is not frozenset or not excluded_resources
            or any(type(name) is not str or not name for name in excluded_resources)):
        raise ValueError("TASK005_PLAN_REJECTED")
    suffix = run_id.hex
    plan = ResourcePlan("task005_" + suffix, "task005_prep_" + suffix, "task005_test_" + suffix)
    if {plan.database, plan.preparation_role, plan.test_role} & excluded_resources:
        raise ValueError("TASK005_PLAN_REJECTED")
    return plan


def require_real_integration_gate(*, opt_in=False, owner_approved=False,
                                  isolated_server=False, sql_approved=False,
                                  inventory_verified=False):
    """Validate operator attestations; they are not evidence of authorization."""
    if not all(value is True for value in (opt_in, owner_approved, isolated_server,
                                           sql_approved, inventory_verified)):
        raise RuntimeError("TASK005_INTEGRATION_NOT_AUTHORIZED")
