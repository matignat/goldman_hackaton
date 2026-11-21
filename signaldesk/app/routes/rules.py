"""SignalDesk rules."""

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import (
    can_view_board,
    current_user,
    db_session,
    is_maintainer,
    is_manager,
    is_member,
    require,
)
from ..models import (
    DeletedRule,
    Proposal,
    Rule,
    Runbook,
    Ticket,
    User,
    current_rules,
    now,
)
from ..services.conditions import canonical, matches, render, validate
from ..services.config_reload import sign_manifest, validate_manifest
from ..services.delivery import queue_ticket
from ..services.records import activity, audit
from ..services.runbooks import formats
from .presentation import row

router = APIRouter()


@router.get("/api/rules")
def rules(team: int, u: User = Depends(current_user), db: Session = Depends(db_session)):
    require(can_view_board(db, u, team))
    return [
        row(
            r,
            [
                "id",
                "team_id",
                "name",
                "description",
                "priority",
                "enabled",
                "condition",
                "presentation",
                "routing",
                "dedup_template",
                "group_template",
                "throttle_seconds",
                "runbook_id",
                "version",
            ],
        )
        for r in current_rules(db).filter_by(team_id=team).order_by(Rule.priority, Rule.id)
    ]


def rule_data(data):
    condition = data.get("condition") or {}
    validate(condition)
    presentation = data.get("presentation") or {}
    if not isinstance(presentation, dict):
        raise HTTPException(422, "presentation must be an object")
    routing = data.get("routing") or {}
    return {
        "name": str(data.get("name", "Untitled rule"))[:120],
        "description": str(data.get("description", "")),
        "priority": int(data.get("priority", 100)),
        "enabled": bool(data.get("enabled", True)),
        "condition": condition,
        "presentation": presentation,
        "routing": routing,
        "dedup_template": str(data.get("dedup_template", ""))[:450],
        "group_template": str(data.get("group_template", ""))[:450],
        "throttle_seconds": max(0, int(data.get("throttle_seconds", 0))),
        "runbook_id": data.get("runbook_id"),
    }


@router.post("/api/rules/test")
def test_rule(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    team = int(data.get("team_id", 0))
    require(can_view_board(db, u, team))
    rule = rule_data(data.get("rule") or {})
    payload = data.get("event")
    if payload is None:
        raise HTTPException(422, "Sample event required")
    norm = canonical(payload, {}, now())
    hit = matches(rule["condition"], norm["flat"])
    return {
        "matched": hit,
        "severity": norm["severity"],
        "fields": norm["flat"],
        "title": render(rule["presentation"].get("title", ""), norm["flat"]),
        "body": render(rule["presentation"].get("body", ""), norm["flat"]),
    }


@router.post("/api/rules/sign-config")
def sign_rule_config(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    # A normal manager session is required; an A or D session cannot authorize
    # file reloads. The HMAC covers the entire rule payload and publication mode.
    team_id = int(data.get("team_id", 0))
    require(is_manager(db, u, team_id))
    manifest = {
        "team_id": team_id,
        "manager_id": u.id,
        "rules": data.get("rules", []),
        "disable_missing": bool(data.get("disable_missing", False)),
        "issued_at": datetime.now(UTC).isoformat(),
    }
    candidate = sign_manifest(manifest)
    try:
        validate_manifest(db, candidate)
    except (ValueError, PermissionError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    audit(
        db, "rule_manifest_authorized", team_id, actor=u, detail={"rules": len(manifest["rules"])}
    )
    db.commit()
    return candidate


@router.post("/api/rules")
def create_rule(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    tid = int(data.get("team_id", 0))
    require(is_manager(db, u, tid), "Team manager required to publish a rule")
    try:
        payload = rule_data(data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    r = Rule(team_id=tid, **payload)
    db.add(r)
    db.flush()
    audit(db, "rule_published", tid, actor=u, detail={"rule_id": r.id})
    db.commit()
    return {"id": r.id, "version": r.version}


@router.put("/api/rules/{rule_id}")
def edit_rule(
    rule_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    r = db.get(Rule, rule_id, with_for_update=True)
    if not r or db.get(DeletedRule, rule_id) is not None:
        raise HTTPException(404)
    require(is_manager(db, u, r.team_id))
    try:
        payload = rule_data(data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    for k, v in payload.items():
        setattr(r, k, v)
    r.version += 1
    audit(db, "rule_updated", r.team_id, actor=u, detail={"rule_id": r.id, "version": r.version})
    db.commit()
    return {"ok": True}


@router.delete("/api/rules/{rule_id}")
def delete_rule(
    rule_id: int,
    permanent: bool = False,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    r = db.get(Rule, rule_id, with_for_update=True)
    if not r or db.get(DeletedRule, rule_id) is not None:
        raise HTTPException(404)
    require(is_manager(db, u, r.team_id))
    r.enabled = False
    if permanent:
        db.add(DeletedRule(rule_id=r.id))
        audit(db, "rule_deleted", r.team_id, actor=u, detail={"rule_id": r.id, "name": r.name})
        db.commit()
        return {"ok": True}
    audit(db, "rule_disabled", r.team_id, actor=u, detail={"rule_id": r.id})
    db.commit()
    return {"ok": True, "note": "Soft-disabled for history safety"}


@router.get("/api/runbooks")
def runbooks(team: int, u: User = Depends(current_user), db: Session = Depends(db_session)):
    require(can_view_board(db, u, team))
    return [
        row(r, ["id", "team_id", "name", "source_format", "source_text", "steps", "version"])
        for r in db.query(Runbook).filter_by(team_id=team)
    ]


@router.post("/api/runbooks")
def create_runbook(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    tid = int(data.get("team_id", 0))
    require(is_manager(db, u, tid))
    raw = str(data.get("source_text", ""))
    fmt = str(data.get("source_format", "markdown"))
    if not raw:
        raise HTTPException(422, "Runbook content required")
    steps = formats.parse(fmt, raw)
    r = Runbook(
        team_id=tid,
        name=str(data.get("name", "Runbook"))[:120],
        source_format=fmt,
        source_text=raw,
        steps=steps,
    )
    db.add(r)
    db.flush()
    audit(db, "runbook_published", tid, actor=u, detail={"runbook_id": r.id})
    db.commit()
    return row(r, ["id", "steps", "version"])


@router.put("/api/runbooks/{rb_id}")
def edit_runbook(
    rb_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    rb = db.get(Runbook, rb_id)
    if not rb:
        raise HTTPException(404)
    require(is_manager(db, u, rb.team_id))
    rb.name = str(data.get("name", rb.name))
    rb.source_format = str(data.get("source_format", rb.source_format))
    rb.source_text = str(data.get("source_text", rb.source_text))
    rb.steps = formats.parse(rb.source_format, rb.source_text)
    rb.version += 1
    audit(db, "runbook_updated", rb.team_id, actor=u, detail={"runbook_id": rb.id})
    db.commit()
    return {"id": rb.id, "steps": rb.steps}


@router.get("/api/proposals")
def proposals(
    team: int | None = None, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    q = db.query(Proposal)
    if team:
        q = q.filter_by(team_id=team)
    items = q.order_by(Proposal.created_at.desc()).limit(150).all()
    return [
        row(
            p,
            [
                "id",
                "team_id",
                "requester_id",
                "target_type",
                "target_id",
                "description",
                "patch",
                "status",
                "comment",
                "revised_from",
                "ticket_id",
                "created_at",
            ],
        )
        for p in items
        if p.requester_id == u.id
        or is_manager(db, u, p.team_id)
        or is_maintainer(db, u, p.team_id)
    ]


@router.post("/api/proposals")
def create_proposal(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    tid = int(data.get("team_id", 0))
    require(is_member(db, u, tid) or is_manager(db, u, tid))
    target = str(data.get("target_type", "rule"))
    if target not in ("rule", "runbook", "ticket"):
        raise HTTPException(422, "Unknown proposal target")
    target_id = data.get("target_id")
    existing = (
        db.query(Proposal)
        .filter_by(
            team_id=tid, requester_id=u.id, target_type=target, target_id=target_id, status="open"
        )
        .first()
    )
    if existing:
        raise HTTPException(409, "You already have a pending request for this target")
    desc = str(data.get("description", "")).strip()
    if not desc:
        raise HTTPException(422, "Please explain the proposed change")
    previous = (
        db.query(Proposal)
        .filter_by(
            team_id=tid,
            requester_id=u.id,
            target_type=target,
            target_id=target_id,
            status="rejected",
        )
        .order_by(Proposal.id.desc())
        .first()
    )
    p = Proposal(
        team_id=tid,
        requester_id=u.id,
        target_type=target,
        target_id=target_id,
        description=desc,
        patch=data.get("patch") or {},
        revised_from=previous.id if previous else None,
    )
    db.add(p)
    db.flush()
    t = Ticket(
        team_id=tid,
        type="change_request",
        state="new",
        title="CHANGE · " + target,
        body=desc,
        severity="info",
        members=[u.id],
        occurrences=0,
        created_at=now(),
        updated_at=now(),
    )
    db.add(t)
    db.flush()
    p.ticket_id = t.id
    audit(db, "change_requested", tid, t.id, u, {"proposal_id": p.id})
    activity(db, tid, t.id, "change", "Change requested: " + desc[:90])
    queue_ticket(db, t, "approval_needed")
    db.commit()
    return {"id": p.id, "ticket_id": t.id}


@router.post("/api/proposals/{proposal_id}/decision")
def decide_proposal(
    proposal_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    p = db.get(Proposal, proposal_id)
    if not p:
        raise HTTPException(404)
    require(is_manager(db, u, p.team_id), "Only team manager can decide proposals")
    if p.status != "open":
        raise HTTPException(409, "Change request already reviewed")
    decision = data.get("decision")
    if decision not in ("accepted", "rejected", "implemented"):
        raise HTTPException(403, "Invalid decision")
    if decision == "rejected" and not str(data.get("comment", "")).strip():
        raise HTTPException(422, "Rejection requires reason")
    p.status = decision
    p.comment = str(data.get("comment", ""))
    t = db.get(Ticket, p.ticket_id)
    if t:
        t.state = "archived"
        t.archived_at = now()
        t.approved_by = u.id
        t.updated_at = now()
    audit(
        db,
        "change_" + decision,
        p.team_id,
        p.ticket_id,
        u,
        {"proposal_id": p.id, "comment": p.comment},
    )
    activity(
        db, p.team_id, p.ticket_id, "change", f"Change request {decision}: {p.description[:80]}"
    )
    db.commit()
    return {"id": p.id, "status": p.status}
