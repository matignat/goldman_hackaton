"""SignalDesk boards."""

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import can_view_board, current_user, db_session, is_manager, require
from ..database import Session as DBSession
from ..models import Activity, Delivery, Event, Proposal, Source, Team, Ticket, User
from ..services.delivery import TICKET_CATEGORIES, queue_ticket
from ..services.records import activity, audit
from .presentation import row, ticket_dict

router = APIRouter()


@router.get("/api/activity")
def recent(
    team: int | None = None,
    limit: int = 40,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    ids = [t.id for t in db.query(Team) if can_view_board(db, u, t.id)]
    if team:
        require(team in ids)
        ids = [team]
    if not ids:
        return []
    q = db.query(Activity).filter(Activity.team_id.in_(ids))
    return [
        row(a, ["id", "team_id", "ticket_id", "kind", "label", "count", "updated_at"])
        for a in q.order_by(Activity.updated_at.desc(), Activity.id.desc())
        .limit(min(200, max(1, limit)))
        .all()
    ]


@router.get("/api/stream")
async def stream(team: int | None = None, u: User = Depends(current_user)):
    uid = u.id

    async def feed():
        last = None
        yield "retry: 1500\n\n"
        while True:
            with DBSession() as db:
                user = db.get(User, uid)
                if user is None:
                    return
                ids = [t.id for t in db.query(Team) if can_view_board(db, user, t.id)]
                if team is not None:
                    ids = [team] if team in ids else []
                if ids:
                    a = (
                        db.query(func.max(Activity.updated_at), func.max(Activity.id))
                        .filter(Activity.team_id.in_(ids))
                        .first()
                    )
                    t = (
                        db.query(func.max(Ticket.updated_at))
                        .filter(Ticket.team_id.in_(ids))
                        .scalar()
                    )
                    d = (
                        db.query(func.max(Delivery.sent_at))
                        .join(Ticket, Delivery.ticket_id == Ticket.id)
                        .filter(Ticket.team_id.in_(ids))
                        .scalar()
                    )
                    signature = str((a, t, d))
                else:
                    signature = "none"
            if signature != last:
                yield "event: invalidate\ndata: " + json.dumps({"changed": True}) + "\n\n"
                last = signature
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        feed(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/archive")
def archive(
    team: int | None = None,
    include_errors: bool = False,
    search: str = "",
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    managed = [t.id for t in db.query(Team) if is_manager(db, u, t.id)]
    require(bool(managed), "Major Incident Archive is manager-only")
    if team:
        require(team in managed)
        managed = [team]
    items = (
        db.query(Ticket)
        .filter(
            Ticket.team_id.in_(managed),
            Ticket.state == "archived",
            Ticket.type == "incident",
            Ticket.severity.in_(["critical", "error"] if include_errors else ["critical"]),
        )
        .order_by(Ticket.archived_at.desc())
        .limit(300)
    )
    return [
        ticket_dict(t)
        for t in items
        if not search or search.lower() in (t.title + " " + str(t.id)).lower()
    ]


@router.get("/api/unmatched")
def unmatched(
    team: int | None = None, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    if u.role != "A" and u.role != "M" and not u.debug_enabled:
        raise HTTPException(403)
    items = db.query(Event).filter_by(state="unmatched").order_by(Event.id.desc()).limit(100).all()

    def visible(ev):
        if u.role == "A":
            return True
        attempted = [r.get("team_id") for r in (ev.route_trace or [])]
        return (
            any(is_manager(db, u, t) for t in attempted)
            if u.role == "M"
            else ev.source_id == db.query(Source).filter_by(slug="demo").first().id
        )

    return [
        row(e, ["id", "source_id", "raw", "received_at", "error", "route_trace", "state"])
        for e in items
        if visible(e) and (not team or any(t.get("team_id") == team for t in e.route_trace or []))
    ]


@router.post("/api/unmatched/{event_id}/claim")
def claim_unmatched(
    event_id: int,
    team_id: int = Body(...),
    category: str = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    require(is_manager(db, u, team_id), "Only the team manager can claim events")
    if category not in TICKET_CATEGORIES:
        raise HTTPException(422, "Choose a valid ticket type")
    ticket_type = category if category in ("task", "change_request") else "incident"
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    require(
        any(entry.get("team_id") == team_id for entry in event.route_trace or []),
        "Choose a team in the routing trace",
    )
    claimed = (
        db.query(Event)
        .filter_by(id=event_id, state="unmatched", ticket_id=None)
        .update({Event.state: "matched"}, synchronize_session=False)
    )
    if not claimed:
        raise HTTPException(409, "This event is no longer unmatched")
    normalized = event.normalized or {}
    fields = normalized.get("flat", {})
    try:
        payload = json.loads(event.raw)
    except ValueError:
        payload = event.raw
    snapshot = payload if isinstance(payload, dict) else {"line": payload}
    occurred = fields.get("canonical.occurredAt")
    timestamp = datetime.fromisoformat(occurred) if occurred else event.received_at
    title = f"{normalized.get('schema') or 'Unmatched event'} · Event #{event.id}"
    ticket = Ticket(
        team_id=team_id,
        type=ticket_type,
        state="new",
        members=[u.id],
        source_id=event.source_id,
        event_id=event.id,
        title=title[:250],
        severity=category if ticket_type == "incident" else "info",
        schema=normalized.get("schema") or "",
        environment=fields.get("canonical.environment", ""),
        occurred_at=timestamp,
        last_occurrence_at=timestamp,
        first_payload=snapshot,
        latest_payload=snapshot,
        samples=[{"event_id": event.id, "at": timestamp.isoformat()}],
    )
    db.add(ticket)
    db.flush()
    if ticket_type == "change_request":
        proposal = Proposal(
            team_id=team_id,
            requester_id=u.id,
            target_type="ticket",
            target_id=ticket.id,
            ticket_id=ticket.id,
            description=title,
        )
        db.add(proposal)
        db.flush()
        audit(db, "change_requested", team_id, ticket.id, u, {"proposal_id": proposal.id})
    event.ticket_id = ticket.id
    audit(
        db,
        "unmatched_event_claimed",
        team_id,
        ticket.id,
        u,
        {"event_id": event.id, "category": category},
    )
    activity(db, team_id, ticket.id, ticket_type, "Claimed unmatched event: " + ticket.title)
    queue_ticket(db, ticket)
    db.commit()
    return ticket_dict(ticket)
