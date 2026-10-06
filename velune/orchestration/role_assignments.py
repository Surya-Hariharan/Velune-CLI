"""Council role assignment data model — persisted as JSON under ~/.velune/."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velune.models.specializations import CouncilRole

logger = logging.getLogger("velune.orchestration.role_assignments")

# The roles a council run actually consumes: exactly the runtime ``CouncilRole`` values.
# Everything assignable through /roles must be in here, because anything else is
# silently ignored by the orchestrator.
EFFECTIVE_ROLES: tuple[str, ...] = tuple(role.value for role in CouncilRole)

# Names that look assignable but that no council seat uses. Rejected with guidance
# when assigned; reported (not deleted) if they are already in the saved file.
INERT_ROLE_NOTES: dict[str, str] = {
    "architect": "no council seat uses this role yet",
    "security": (
        "no council seat is bound to it (the Security Critic runs on the reviewer model, "
        "so assign the reviewer role instead)"
    ),
    "embedding": (
        "embedding models are not a council role; they are configured with the memory "
        "settings (see `velune doctor`)"
    ),
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    "planner": "Decomposes tasks, retrieves context, plans execution steps",
    "coder": "Writes code, generates patches, implements solutions",
    "reviewer": "Audits code quality, catches regressions, checks correctness",
    "challenger": "Argues against proposals, finds edge cases, stress-tests logic",
    "synthesizer": "Combines agent outputs into a final coherent response",
}


def default_assignments_path() -> Path:
    """Where /roles persists assignments (resolved at call time, so HOME overrides apply)."""
    return Path.home() / ".velune" / "council_roles.json"


@dataclass
class RoleAssignment:
    role: str
    model_id: str
    provider_id: str


@dataclass
class CouncilRoleMap:
    assignments: dict[str, RoleAssignment] = field(default_factory=dict)
    # Entries in the file for roles that have no effect (or that are not roles at
    # all): reported, written back unchanged, never applied.
    ignored: dict[str, Any] = field(default_factory=dict)

    def assign(self, role: str, model_id: str, provider_id: str) -> None:
        if role in INERT_ROLE_NOTES:
            raise ValueError(
                f"Role {role!r} has no effect: {INERT_ROLE_NOTES[role]}. "
                f"Valid roles: {', '.join(EFFECTIVE_ROLES)}"
            )
        if role not in EFFECTIVE_ROLES:
            raise ValueError(f"Unknown role: {role!r}. Valid roles: {', '.join(EFFECTIVE_ROLES)}")
        self.assignments[role] = RoleAssignment(role, model_id, provider_id)

    def get(self, role: str) -> RoleAssignment | None:
        return self.assignments.get(role)

    def clear_role(self, role: str) -> None:
        self.assignments.pop(role, None)
        self.ignored.pop(role, None)

    def clear_all(self) -> None:
        self.assignments.clear()
        self.ignored.clear()

    def ignored_notes(self) -> dict[str, str]:
        """Why each ignored saved entry has no effect."""
        return {role: INERT_ROLE_NOTES.get(role, "not a council role") for role in self.ignored}

    def to_dict(self) -> dict:
        data: dict[str, Any] = dict(self.ignored)
        data.update(
            {
                role: {"model_id": a.model_id, "provider_id": a.provider_id}
                for role, a in self.assignments.items()
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: dict) -> CouncilRoleMap:
        """Load every valid entry; one bad entry never discards the rest."""
        role_map = cls()
        for role, info in data.items():
            valid = (
                role in EFFECTIVE_ROLES
                and isinstance(info, dict)
                and isinstance(info.get("model_id"), str)
                and isinstance(info.get("provider_id"), str)
            )
            if valid:
                role_map.assign(role, info["model_id"], info["provider_id"])
            else:
                role_map.ignored[role] = info
        return role_map

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), indent=2)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(str(tmp), str(path))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

    @classmethod
    def load(cls, path: Path) -> CouncilRoleMap:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read council role assignments from %s: %s", path, exc)
            return cls()
        if not isinstance(data, dict):
            logger.warning("Ignoring %s: expected a JSON object of role assignments", path)
            return cls()
        return cls.from_dict(data)


@dataclass
class RoleOverrideReport:
    """What applying a role map to the mapper did."""

    applied: list[str] = field(default_factory=list)
    ignored: dict[str, str] = field(default_factory=dict)


def apply_role_map(mapper: Any, role_map: CouncilRoleMap) -> RoleOverrideReport:
    """Make *mapper* route the roles in *role_map* to their assigned models.

    Replaces any earlier overrides. Only roles a council run consumes are applied;
    saved entries without effect are reported, never silently dropped.
    """
    mapper.overrides.clear()
    providers = getattr(mapper, "override_providers", None)
    if providers is not None:
        providers.clear()
    report = RoleOverrideReport(ignored=role_map.ignored_notes())
    for role_str, assignment in role_map.assignments.items():
        try:
            role = CouncilRole(role_str)
        except ValueError:
            report.ignored[role_str] = "not a council role"
            continue
        mapper.overrides[role] = assignment.model_id
        if providers is not None and assignment.provider_id not in ("", "unknown"):
            providers[role] = assignment.provider_id
        report.applied.append(role_str)
    return report


def apply_persisted_role_overrides(mapper: Any, path: Path | None = None) -> RoleOverrideReport:
    """Load the saved assignments and apply them (used by every entry point, not just the REPL)."""
    return apply_role_map(mapper, CouncilRoleMap.load(path or default_assignments_path()))
