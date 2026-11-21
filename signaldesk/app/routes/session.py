"""SignalDesk session."""

import os

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Response,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import SIGNER, can_view_board, current_user, db_session, require
from ..models import BoardAccess, Membership, Team, User
from ..services.delivery import TICKET_CATEGORIES, channels, ticket_channels
from ..services.records import audit
from .presentation import row, team_dict
from .profile_data import ProfileData

router = APIRouter()


@router.get("/api/personas")
def personas(db: Session = Depends(db_session)):
    require(os.getenv("DEMO_MODE", "1") == "1", "Demo persona selector disabled")
    return [
        {"id": u.id, "name": u.name, "role": u.role, "debug": u.debug_enabled}
        for u in db.query(User).order_by(User.id)
    ]


@router.post("/api/session")
def signin(data: dict = Body(...), db: Session = Depends(db_session)):
    require(os.getenv("DEMO_MODE", "1") == "1", "Demo sign-in disabled")
    u = db.get(User, data.get("user_id"))
    if not u:
        raise HTTPException(404, "Persona not found")
    response = Response(status_code=204)
    response.set_cookie(
        "signaldesk_session",
        SIGNER.dumps(u.id),
        httponly=True,
        samesite="strict",
        secure=os.getenv("HTTPS_ONLY", "0") == "1",
        max_age=86400,
    )
    return response


@router.post("/api/logout")
def logout():
    resp = Response(status_code=204)
    resp.delete_cookie("signaldesk_session")
    return resp


@router.get("/api/me")
def me(u: User = Depends(current_user), db: Session = Depends(db_session)):
    myteams = [
        team_dict(db, db.get(Team, m.team_id))
        for m in db.query(Membership).filter_by(user_id=u.id)
    ]
    managed = [
        team_dict(db, t)
        for t in db.query(Team).filter_by(leader_id=u.id)
        if t.id not in {x["id"] for x in myteams}
    ]
    guest = [
        team_dict(db, db.get(Team, g.team_id))
        for g in db.query(BoardAccess).filter_by(user_id=u.id, status="approved")
        if g.team_id not in {x["id"] for x in myteams + managed}
    ]
    return {
        **row(
            u,
            [
                "id",
                "name",
                "role",
                "email",
                "phone",
                "manager_id",
                "subscriptions",
                "preset",
                "timezone",
                "debug_enabled",
            ],
        ),
        "ticket_channels": ticket_channels(u),
        "ticket_categories": TICKET_CATEGORIES,
        "available_channels": channels.available(u),
        "policy_channels": channels.labels,
        "teams": myteams + managed,
        "shared_teams": guest,
        "is_demo": os.getenv("DEMO_MODE", "1") == "1",
    }


@router.put("/api/me/profile")
def update_profile(
    data: ProfileData, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    """Update the authenticated user's own name and contact details."""
    u.name, u.email, u.phone = data.name, data.email, data.phone
    try:
        db.flush()
        audit(db, "profile_updated", actor=u)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An employee with this name already exists")
    return {"ok": True}


@router.get("/api/teams")
def teams(u: User = Depends(current_user), db: Session = Depends(db_session)):
    return [
        team_dict(db, t)
        for t in db.query(Team).order_by(Team.position, Team.id)
        if can_view_board(db, u, t.id)
    ]
