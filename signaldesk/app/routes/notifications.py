"""SignalDesk notifications."""

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import can_view_board, current_user, db_session, is_manager, require
from ..models import Delivery, Policy, Team, Ticket, TicketDeliveryPreferences, User
from ..services.delivery import TICKET_CATEGORIES, channels
from ..services.records import audit
from .presentation import row

router = APIRouter()


@router.get("/api/delivery")
def delivery_logs(
    team: int | None = None,
    page: int | None = None,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    visible = Delivery.user_id == u.id
    if u.role == "M":
        managed = (
            db.query(Ticket.id)
            .join(Team, Team.id == Ticket.team_id)
            .filter(Team.leader_id == u.id)
        )
        if team is not None:
            managed = managed.filter(Team.id == team)
        visible = visible | Delivery.ticket_id.in_(managed)
    query = db.query(Delivery).filter(visible).order_by(Delivery.id.desc())
    fields = [
        "id",
        "ticket_id",
        "user_id",
        "channel",
        "kind",
        "status",
        "content",
        "attempts",
        "available_at",
        "sent_at",
        "last_error",
        "fallback_for",
    ]
    if page is None:
        return [row(job, fields) for job in query.limit(120)]
    if page < 1:
        raise HTTPException(422, "Page must be positive")
    page_size = 25
    total = query.order_by(None).count()
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    jobs = query.offset((page - 1) * page_size).limit(page_size)
    return {
        "items": [row(job, fields) for job in jobs],
        "page": page,
        "pages": pages,
        "total": total,
    }


@router.put("/api/preferences")
def preferences(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    if any(
        key in data
        for key in ("primary_channel", "backup_channel", "preferred_channels", "severity_channels")
    ):
        raise HTTPException(
            422, "Use ticket_channels to select delivery channels for each ticket category"
        )
    if "ticket_channels" in data:
        mapping = data["ticket_channels"]
        if not isinstance(mapping, dict) or set(mapping) != set(TICKET_CATEGORIES):
            raise HTTPException(422, "Choose channels for every ticket category")
        available = channels.available(u)
        normalized = {}
        for category, choices in mapping.items():
            if (
                not isinstance(choices, list)
                or not choices
                or any(not isinstance(c, str) or c not in available for c in choices)
            ):
                raise HTTPException(
                    422, f"Select at least one available channel for {TICKET_CATEGORIES[category]}"
                )
            normalized[category] = sorted(set(choices))
        if u.delivery_preferences is None:
            u.delivery_preferences = TicketDeliveryPreferences()
        u.delivery_preferences.channels = normalized
    if "subscriptions" in data:
        u.subscriptions = data["subscriptions"]
    if "timezone" in data:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(str(data["timezone"]))
        except Exception:
            raise HTTPException(422, "Invalid timezone")
        u.timezone = str(data["timezone"])
    if "preset" in data:
        if data["preset"] not in ("engineer", "oncall", "nonengineer"):
            raise HTTPException(422, "Unknown preset")
        u.preset = data["preset"]
    audit(db, "preferences_updated", actor=u)
    db.commit()
    return {"ok": True}


@router.get("/api/policies")
def policies(team: int, u: User = Depends(current_user), db: Session = Depends(db_session)):
    require(can_view_board(db, u, team))
    return [
        row(p, ["id", "team_id", "severity", "channel", "kind", "target", "ignore_schedule"])
        for p in db.query(Policy).filter_by(team_id=team)
    ]


@router.post("/api/policies")
def create_policy(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    tid = int(data.get("team_id", 0))
    require(is_manager(db, u, tid))
    channel = data.get("channel")
    if not isinstance(channel, str) or channel not in channels.senders:
        raise HTTPException(422, "Invalid channel")
    category = data.get("severity", "critical")
    if category not in (*TICKET_CATEGORIES, "all"):
        raise HTTPException(422, "Invalid ticket type")
    p = Policy(
        team_id=tid,
        severity=category,
        channel=channel,
        kind=str(data.get("kind", "new_ticket")),
        target=str(data.get("target", "all")),
        ignore_schedule=bool(data.get("ignore_schedule", False)),
    )
    db.add(p)
    audit(db, "policy_created", tid, actor=u)
    db.commit()
    return {"ok": True}


@router.delete("/api/policies/{policy_id}")
def delete_policy(
    policy_id: int, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    policy = db.get(Policy, policy_id)
    if not policy:
        raise HTTPException(404, "Policy not found")
    tid = policy.team_id
    require(is_manager(db, u, tid))
    db.get(Team, tid, with_for_update=True)
    other = db.query(Policy.id).filter(Policy.team_id == tid, Policy.id != policy_id).exists()
    removed = (
        db.query(Policy).filter(Policy.id == policy_id, other).delete(synchronize_session=False)
    )
    if not removed:
        raise HTTPException(409, "Keep at least one mandatory team policy")
    audit(
        db,
        "policy_deleted",
        tid,
        actor=u,
        detail={"policy_id": policy_id, "channel": policy.channel},
    )
    db.commit()
    return {"ok": True}
