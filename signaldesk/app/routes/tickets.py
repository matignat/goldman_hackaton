"""SignalDesk tickets."""

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import (
    can_approve_final,
    can_approve_resolution,
    can_view_board,
    can_view_ticket,
    can_work,
    current_user,
    db_session,
    is_guest,
    is_manager,
    is_member,
    require,
)
from ..models import (
    Activity,
    Audit,
    Delivery,
    Event,
    Rule,
    Runbook,
    Team,
    Ticket,
    User,
    now,
)
from ..services.delivery import queue_ticket
from ..services.records import activity, audit
from ..services.runbooks import actions
from .presentation import require_step_unlocked, row, ticket_dict

router = APIRouter()


@router.get("/api/board")
def board(
    team: int | None = None,
    severity: str = "",
    search: str = "",
    my_tickets: bool = False,
    unassigned: bool = False,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    allowed = [t.id for t in db.query(Team) if can_view_board(db, u, t.id)]
    if team is not None:
        require(team in allowed)
        allowed = [team]
    if not allowed:
        return {
            "tickets": [],
            "counters": {
                "total": 0,
                "new": 0,
                "acknowledged": 0,
                "in_progress": 0,
                "done": 0,
                "critical": 0,
                "attention": 0,
                "awaiting_approval": 0,
            },
            "total": 0,
        }
    all_tickets = (
        db.query(Ticket)
        .filter(Ticket.team_id.in_(allowed), Ticket.state.notin_(["archived", "purged"]))
        .order_by(Ticket.updated_at.desc())
        .all()
    )
    visible = [
        t
        for t in all_tickets
        if (not severity or t.severity == severity)
        and (not search or search.lower() in (t.title + " " + t.body + " " + str(t.id)).lower())
        and (not my_tickets or u.id in (t.members or []))
        and (not unassigned or not t.members)
    ]

    def counter(ts):
        return {
            "total": len(ts),
            **{
                s: sum(t.state == s for t in ts)
                for s in ["new", "acknowledged", "in_progress", "done"]
            },
            "critical": sum(t.severity == "critical" and t.type == "incident" for t in ts),
            "attention": sum("attention" in (t.flags or []) for t in ts),
            "awaiting_approval": sum(t.state == "done" for t in ts),
        }

    return {
        "tickets": [ticket_dict(t) for t in visible],
        "counters": counter(visible),
        "total": len(all_tickets),
        "all_counters": counter(all_tickets),
    }


@router.get("/api/tickets")
def tickets(
    handled: bool = False,
    team: int | None = None,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    q = db.query(Ticket)
    if team:
        q = q.filter_by(team_id=team)
    q = q.filter(
        Ticket.state == "archived" if handled else Ticket.state.notin_(["archived", "purged"])
    )
    items = q.order_by(Ticket.updated_at.desc()).limit(250).all()
    if handled:
        items = [
            t for t in items if u.id in (t.members or []) or u.id in (t.done_by, t.approved_by)
        ]
    else:
        items = [t for t in items if can_view_ticket(db, u, t)]
    return [ticket_dict(t) for t in items]


@router.get("/api/tickets/{ticket_id}")
def ticket_detail(
    ticket_id: int, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    if t.state == "purged":
        raise HTTPException(410, "Ticket has been purged; audit tombstone remains")
    require(can_view_ticket(db, u, t))
    timeline = db.query(Audit).filter_by(ticket_id=ticket_id).order_by(Audit.id.asc()).all()
    deliveries = (
        db.query(Delivery)
        .filter_by(ticket_id=ticket_id)
        .order_by(Delivery.id.desc())
        .limit(40)
        .all()
    )
    rule = db.get(Rule, t.rule_id) if t.rule_id else None
    runbook = db.get(Runbook, rule.runbook_id) if rule and rule.runbook_id else None
    first_event = db.get(Event, t.event_id) if t.event_id else None
    latest_event = (
        db.query(Event)
        .filter_by(ticket_id=ticket_id, state="matched")
        .order_by(Event.id.desc())
        .first()
        if t.type == "incident"
        else None
    )
    return {
        **ticket_dict(t, True),
        "runbook_source": runbook.source_text if runbook else None,
        "runbook_format": runbook.source_format if runbook else None,
        "first_normalized": first_event.normalized if first_event else None,
        "latest_normalized": latest_event.normalized if latest_event else None,
        "timeline": [row(a, ["id", "actor", "action", "detail", "created_at"]) for a in timeline],
        "deliveries": [
            row(d, ["id", "channel", "user_id", "kind", "status", "attempts", "last_error"])
            for d in deliveries
        ],
        "permissions": {
            "can_work": can_work(db, u, t)
            and t.state != "archived"
            and not is_guest(db, u, t.team_id),
            "can_approve": can_approve_resolution(db, u, t),
            "can_manage": is_manager(db, u, t.team_id),
            "can_join": is_member(db, u, t.team_id) and u.id not in (t.members or []),
        },
        "members": [
            {"id": uid, "name": member.name if (member := db.get(User, uid)) else "Unknown"}
            for uid in (t.members or [])
        ],
    }


@router.post("/api/tickets")
def create_task(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    team_id = int(data.get("team_id", 0))
    require(is_member(db, u, team_id) or is_manager(db, u, team_id))
    title = str(data.get("title", "")).strip()
    if not title:
        raise HTTPException(422, "Title required")
    t = Ticket(
        team_id=team_id,
        type="task",
        title=title[:250],
        body=str(data.get("body", "")),
        severity="info",
        state="new",
        occurrences=0,
        members=[u.id],
        first_payload={},
        latest_payload={},
        samples=[],
        runbook_steps=[],
        flags=[],
        created_at=now(),
        updated_at=now(),
    )
    db.add(t)
    db.flush()
    audit(db, "task_created", team_id, t.id, u)
    activity(db, team_id, t.id, "task", "Task created: " + t.title)
    queue_ticket(db, t, "new_ticket")
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/join")
def join(ticket_id: int, u: User = Depends(current_user), db: Session = Depends(db_session)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    require(is_member(db, u, t.team_id) and t.state != "archived")
    t.members = list(set((t.members or []) + [u.id]))
    t.updated_at = now()
    audit(db, "ticket_joined", t.team_id, t.id, u)
    activity(db, t.team_id, t.id, "ticket", "Joined ticket: " + t.title)
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/members")
def add_member(
    ticket_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    require(can_work(db, u, t) and t.state != "archived" and not is_guest(db, u, t.team_id))
    candidate = db.get(User, data.get("user_id"))
    if not candidate:
        raise HTTPException(404, "Employee not found")
    if candidate.id not in (t.members or []):
        t.members = [*(t.members or []), candidate.id]
        queue_ticket(db, t, "assigned", extra_users=[candidate.id])
        audit(db, "ticket_member_added", t.team_id, t.id, u, {"member_id": candidate.id})
    t.updated_at = now()
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/move")
def move(
    ticket_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    require(
        t.type in ("incident", "task")
        and t.state != "archived"
        and can_work(db, u, t)
        and not is_guest(db, u, t.team_id)
    )
    dest = data.get("state")
    if dest not in ("acknowledged", "in_progress", "done"):
        raise HTTPException(422, "Invalid target state")
    if t.state == "done" and dest == "done":
        return ticket_dict(t)
    if dest in ("acknowledged", "in_progress"):
        require(u.id in (t.members or []), "Join the ticket before starting its workflow")
        previous = "new" if dest == "acknowledged" else "acknowledged"
        if t.state != previous:
            raise HTTPException(409, "Acknowledge the ticket before starting work")
    if dest == "done" and any(step.get("status") != "done" for step in t.runbook_steps or []):
        raise HTTPException(409, "Complete every runbook step before marking the ticket Done")
    if dest == "done" and not is_manager(db, u, t.team_id):
        require(u.id in (t.members or []), "Join the ticket before marking it Done")
        if t.state != "in_progress":
            raise HTTPException(
                409, "Start work and complete every runbook step before marking the ticket Done"
            )
    if t.state == "done" and dest != "done":
        raise HTTPException(409, "Only rejection or relapse reopens Done")
    old = t.state
    t.state = dest
    t.updated_at = now()
    if u.id not in (t.members or []):
        t.members = [*(t.members or []), u.id]
    if dest == "done":
        t.done_by = u.id
        queue_ticket(db, t, "approval_needed")
    audit(
        db,
        "ticket_moved",
        t.team_id,
        t.id,
        u,
        {"from": old, "to": dest, "note": str(data.get("note", ""))[:400]},
    )
    activity(
        db,
        t.team_id,
        t.id,
        "ticket",
        f"{t.title}: {old.replace('_', ' ')} → {dest.replace('_', ' ')}",
    )
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/approve")
def approve(ticket_id: int, u: User = Depends(current_user), db: Session = Depends(db_session)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    require(
        t.type in ("task", "incident") and t.state == "done" and can_approve_final(db, u, t),
        "Manager/maintainer approval required",
    )
    require(
        can_approve_resolution(db, u, t),
        "Separation of duties: the person marking Done cannot approve",
    )
    t.state = "archived"
    t.active_key = None
    t.archived_at = now()
    t.updated_at = now()
    t.approved_by = u.id
    audit(db, "ticket_approved", t.team_id, t.id, u)
    activity(db, t.team_id, t.id, "approval", "Approved and archived: " + t.title)
    queue_ticket(db, t, "resolve_approved")
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/reject")
def reject(
    ticket_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    require(t.type in ("incident", "task") and t.state == "done" and can_approve_final(db, u, t))
    comment = str(data.get("comment", "")).strip()
    if not comment:
        raise HTTPException(422, "Reason is required")
    t.state = "in_progress"
    t.done_by = None
    t.updated_at = now()
    audit(db, "ticket_rejected", t.team_id, t.id, u, {"comment": comment})
    activity(db, t.team_id, t.id, "approval", "Returned to In progress: " + t.title)
    queue_ticket(db, t, "resolve_rejected")
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/comment")
def comment(
    ticket_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    require(can_view_ticket(db, u, t) and t.state != "archived" and not is_guest(db, u, t.team_id))
    text = str(data.get("comment", "")).strip()
    if not text:
        raise HTTPException(422, "Comment is required")
    audit(db, "comment", t.team_id, t.id, u, {"text": text[:4000]})
    activity(db, t.team_id, t.id, "comment", u.name + " commented on " + t.title)
    t.updated_at = now()
    db.commit()
    return {"ok": True}


@router.post("/api/tickets/{ticket_id}/attention")
def acknowledge_attention(
    ticket_id: int, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    require(can_work(db, u, t) and t.state != "archived")
    t.flags = [v for v in (t.flags or []) if v != "attention"]
    audit(db, "attention_acknowledged", t.team_id, t.id, u)
    db.commit()
    return {"ok": True}


@router.post("/api/tickets/{ticket_id}/steps/{step_idx}/run")
def run_step(
    ticket_id: int,
    step_idx: int,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    t = db.get(Ticket, ticket_id, with_for_update=True)
    if not t:
        raise HTTPException(404)
    require(can_work(db, u, t) and t.state != "archived" and not is_guest(db, u, t.team_id))
    require(u.id in (t.members or []), "Join the ticket before running a step")
    if t.state != "in_progress":
        raise HTTPException(409, "Acknowledge the ticket and start work before running a step")
    steps = list(t.runbook_steps or [])
    if not 0 <= step_idx < len(steps):
        raise HTTPException(404, "Step not found")
    require_step_unlocked(steps, step_idx)
    step = {**steps[step_idx]}
    if step.get("status") == "done":
        raise HTTPException(409, "Step already complete")
    if step.get("status") == "waiting_approval":
        raise HTTPException(409, "Step already awaiting review")
    if step.get("approval") == "required" and not step.get("approved_by"):
        step["status"] = "waiting_approval"
        steps[step_idx] = step
        t.runbook_steps = steps
        t.flags = list(set((t.flags or []) + ["action_approval"]))
        audit(db, "action_approval_requested", t.team_id, t.id, u, {"step": step_idx})
        queue_ticket(db, t, "action_approval_needed")
        db.commit()
        return ticket_dict(t)
    return complete_step(db, t, u, steps, step_idx, step)


def complete_step(db, t, u, steps, step_idx, step):
    try:
        outcome = (
            actions.execute(step["action"], step.get("params", {}), t, db)
            if step.get("type") == "action"
            else {"status": "completed_manual"}
        )
        step.update(status="done", done_by=u.id, outcome=outcome)
    except Exception as exc:
        step.update(status="failed", outcome={"error": str(exc)[:100]})
    steps[step_idx] = step
    t.runbook_steps = steps
    t.updated_at = now()
    audit(db, "runbook_step", t.team_id, t.id, u, {"step": step_idx, "result": step["status"]})
    activity(db, t.team_id, t.id, "runbook", "Runbook step: " + t.title)
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/steps/{step_idx}/approve")
def approve_step(
    ticket_id: int,
    step_idx: int,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    return review_step(db, u, ticket_id, step_idx, approve=True)


@router.post("/api/tickets/{ticket_id}/steps/{step_idx}/reject")
def reject_step(
    ticket_id: int,
    step_idx: int,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    return review_step(db, u, ticket_id, step_idx, approve=False)


def review_step(db, u, ticket_id, step_idx, approve):
    t = db.get(Ticket, ticket_id, with_for_update=True)
    if not t:
        raise HTTPException(404)
    require(is_manager(db, u, t.team_id), "Only the team manager can review this step")
    if t.state != "in_progress":
        raise HTTPException(409, "Ticket must be in progress")
    steps = list(t.runbook_steps or [])
    if not 0 <= step_idx < len(steps):
        raise HTTPException(404)
    require_step_unlocked(steps, step_idx)
    step = {**steps[step_idx]}
    if step.get("status") != "waiting_approval":
        raise HTTPException(409, "No pending step approval")
    step.update(approved_by=u.id if approve else None, status="pending" if approve else "returned")
    steps[step_idx] = step
    if not any(s.get("status") == "waiting_approval" for s in steps):
        t.flags = [f for f in (t.flags or []) if f != "action_approval"]
    if approve:
        audit(db, "runbook_action_approved", t.team_id, t.id, u, {"step": step_idx})
        return complete_step(db, t, u, steps, step_idx, step)
    t.runbook_steps = steps
    t.updated_at = now()
    audit(db, "runbook_action_returned", t.team_id, t.id, u, {"step": step_idx})
    activity(db, t.team_id, t.id, "runbook", "Runbook step sent back: " + step.get("text", ""))
    db.commit()
    return ticket_dict(t)


@router.post("/api/tickets/{ticket_id}/purge")
def purge_ticket(
    ticket_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    require(is_manager(db, u, t.team_id), "Only the designated team manager can purge history")
    if t.state != "archived":
        raise HTTPException(409, "Only approved archived tickets can be purged")
    if str(data.get("confirmation", "")) != f"PURGE TKT-{t.id:05d}":
        raise HTTPException(422, f"Type PURGE TKT-{t.id:05d} to confirm")
    # This is an explicit destructive operation: remove raw linked event data,
    # delivery payloads and prior comment bodies, retaining the audited tombstone.
    for ev in db.query(Event).filter_by(ticket_id=t.id):
        ev.raw = "[explicitly purged]"
        ev.normalized = {}
        ev.route_trace = []
        ev.state = "purged"
    for job in db.query(Delivery).filter_by(ticket_id=t.id):
        job.content = {"redacted": True}
    for previous in db.query(Audit).filter_by(ticket_id=t.id):
        previous.detail = {}
    for item in db.query(Activity).filter_by(ticket_id=t.id):
        item.label = f"Purged ticket #{t.id}"
    t.state = "purged"
    t.type = "purged"
    t.title = "[purged]"
    t.body = ""
    t.first_payload = {}
    t.latest_payload = {}
    t.samples = []
    t.members = []
    t.runbook_steps = []
    t.flags = ["purged"]
    t.fingerprint = ""
    t.active_key = None
    audit(db, "ticket_purged_tombstone", t.team_id, t.id, u, {"ticket_id": t.id, "explicit": True})
    db.commit()
    return {"ok": True, "id": ticket_id, "tombstone": "ticket_purged_tombstone"}
