import os
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .database import Session as DBSession
from .models import BoardAccess, Membership, Team, Ticket, User

SIGNER = URLSafeTimedSerializer(
    os.getenv("SECRET_KEY", "dev-secret-for-local-demo-only"), salt="signaldesk-demo-session"
)


def session_user(request: Request) -> int:
    token = request.cookies.get("signaldesk_session")
    if not token:
        raise HTTPException(401, "Select a demo persona to sign in")
    try:
        return int(SIGNER.loads(token, max_age=60 * 60 * 24))
    except (BadSignature, SignatureExpired, ValueError):
        raise HTTPException(401, "Session expired")


def db_session() -> Iterator[Session]:
    """Close the request session even when a handler raises."""
    with DBSession() as db:
        yield db


def current_user(uid: int = Depends(session_user), db: Session = Depends(db_session)) -> User:
    user = db.get(User, uid)
    if not user:
        raise HTTPException(401, "Unknown session user")
    return user


def require_role(user: User, role: str) -> None:
    if user.role != role:
        raise HTTPException(403, f"Requires {role} role")


def require_debug(user: User) -> None:
    if not user.debug_enabled or os.getenv("DEMO_MODE", "1") != "1":
        raise HTTPException(403, "Demo/Debug capability required")


def is_manager(db: Session, user: User, team_id: int) -> bool:
    team = db.get(Team, team_id)
    return bool(team and team.leader_id == user.id and user.role == "M")


def is_member(db: Session, user: User, team_id: int) -> bool:
    return db.get(Membership, (user.id, team_id)) is not None


def is_maintainer(db: Session, user: User, team_id: int) -> bool:
    m = db.get(Membership, (user.id, team_id))
    return bool(m and m.maintainer)


def is_guest(db: Session, user: User, team_id: int) -> bool:
    return bool(
        db.query(BoardAccess)
        .filter_by(team_id=team_id, user_id=user.id, status="approved")
        .first()
    )


def can_view_board(db: Session, user: User, team_id: int) -> bool:
    return (
        is_member(db, user, team_id)
        or is_manager(db, user, team_id)
        or is_guest(db, user, team_id)
    )


def can_view_ticket(db: Session, user: User, ticket: Ticket) -> bool:
    """Allow board viewers to read active tickets and participants to read their history."""
    if can_view_board(db, user, ticket.team_id):
        if ticket.state != "archived" or is_manager(db, user, ticket.team_id):
            return True
    return user.id in (ticket.members or []) or user.id in (ticket.done_by, ticket.approved_by)


def can_work(db: Session, user: User, ticket: Ticket) -> bool:
    return (
        is_manager(db, user, ticket.team_id)
        or is_maintainer(db, user, ticket.team_id)
        or user.id in (ticket.members or [])
    )


def can_approve_final(db: Session, user: User, ticket: Ticket) -> bool:
    return is_manager(db, user, ticket.team_id) or is_maintainer(db, user, ticket.team_id)


def can_approve_resolution(db: Session, user: User, ticket: Ticket) -> bool:
    if ticket.done_by == user.id:
        return ticket.type in ("task", "incident") and is_manager(db, user, ticket.team_id)
    return can_approve_final(db, user, ticket)


def require(condition, detail="Permission denied"):
    if not condition:
        raise HTTPException(403, detail)
