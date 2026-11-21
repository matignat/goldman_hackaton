"""SignalDesk presentation."""

import json
from datetime import datetime

from fastapi import HTTPException

from ..models import (
    Membership,
    User,
)


def serialized(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (dict, list, tuple)):
        return json.loads(json.dumps(obj, default=str))
    return obj


def row(obj, keys):
    return {k: serialized(getattr(obj, k)) for k in keys}


def team_dict(db, t):
    leader = db.get(User, t.leader_id)
    return {
        "id": t.id,
        "name": t.name,
        "slug": t.slug,
        "leader_id": t.leader_id,
        "leader_name": leader.name if leader else "",
        "members": [
            {"id": m.user_id, "name": db.get(User, m.user_id).name, "maintainer": m.maintainer}
            for m in db.query(Membership).filter_by(team_id=t.id).all()
        ],
    }


def first_approval_gate(steps):
    for index, step in enumerate(steps or []):
        if step.get("approval") == "required" and (
            not step.get("approved_by") or step.get("status") != "done"
        ):
            return index
    return None


def require_step_unlocked(steps, index):
    gate = first_approval_gate(steps)
    if gate is not None and index > gate:
        raise HTTPException(409, f"Step {gate + 1} needs manager approval and completion first")


def ticket_dict(t, detail=False):
    keys = [
        "id",
        "team_id",
        "rule_id",
        "source_id",
        "event_id",
        "type",
        "state",
        "incident_status",
        "title",
        "body",
        "severity",
        "environment",
        "schema",
        "occurred_at",
        "last_occurrence_at",
        "created_at",
        "updated_at",
        "resolved_at",
        "archived_at",
        "occurrences",
        "flags",
        "members",
        "runbook_steps",
        "done_by",
        "approved_by",
        "recurrence_of",
    ]
    x = row(t, keys)
    x["display_id"] = f"TKT-{t.id:05d}"
    if detail:
        x["approval_gate_index"] = first_approval_gate(t.runbook_steps)
        x.update(row(t, ["first_payload", "latest_payload", "samples", "fingerprint"]))
    return x
