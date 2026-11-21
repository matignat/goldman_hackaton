"""SignalDesk organization."""

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import (
    can_view_board,
    current_user,
    db_session,
    is_manager,
    require,
    require_role,
)
from ..models import (
    BoardAccess,
    Delivery,
    Event,
    Membership,
    Source,
    Team,
    Ticket,
    User,
)
from ..services.records import audit
from .presentation import row, team_dict

router = APIRouter()


@router.get("/api/organization")
def organization(u: User = Depends(current_user), db: Session = Depends(db_session)):
    boss = db.get(User, u.manager_id) if u.manager_id else None

    def direct_reports(manager_id):
        members = (
            db.query(Membership.user_id)
            .join(Team, Team.id == Membership.team_id)
            .filter(Team.leader_id == manager_id)
        )
        return db.query(User).filter(User.id != manager_id, User.id.in_(members))

    reports = direct_reports(u.id).order_by(User.name).all()

    def summary(x):
        return {
            "id": x.id,
            "name": x.name,
            "role": x.role,
            "email": x.email,
            "team_ids": [m.team_id for m in db.query(Membership).filter_by(user_id=x.id)],
            "direct_reports": direct_reports(x.id).count(),
        }

    managed = (
        db.query(Team).filter_by(leader_id=u.id).order_by(Team.name).all() if u.role == "M" else []
    )
    employees = (
        db.query(User.id, User.name)
        .filter_by(role="E", debug_enabled=False)
        .order_by(User.name)
        .all()
        if managed
        else []
    )
    return {
        "managed_teams": [team_dict(db, team) for team in managed],
        "employees": [{"id": emp.id, "name": emp.name} for emp in employees],
        "manager": summary(boss) if boss else None,
        "reports": [summary(e) for e in reports],
    }


@router.post("/api/teams/{team_id}/members")
def member_manage(
    team_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    team = db.get(Team, team_id, with_for_update=True)
    if not team:
        raise HTTPException(404, "Team not found")
    require(u.role == "A" or is_manager(db, u, team_id))
    user_id = int(data.get("user_id", 0))
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404)
    action = data.get("action", "add")
    if action == "remove" and target.id == team.leader_id:
        raise HTTPException(409, "The team leader must remain on the team")
    m = db.get(Membership, (user_id, team_id))
    if action == "add" and not m:
        db.add(Membership(user_id=user_id, team_id=team_id, maintainer=False))
    elif action == "remove" and m:
        db.delete(m)
    elif action in ("grant_maintainer", "revoke_maintainer") and m:
        m.maintainer = action == "grant_maintainer"
    else:
        raise HTTPException(409, "Invalid membership change")
    audit(db, "membership_" + action, team_id, actor=u, detail={"employee": user_id})
    db.commit()
    return {"ok": True}


@router.get("/api/access")
def access_requests(u: User = Depends(current_user), db: Session = Depends(db_session)):
    reqs = db.query(BoardAccess).order_by(BoardAccess.id.desc()).all()
    return [
        row(a, ["id", "team_id", "user_id", "reason", "status"])
        for a in reqs
        if a.user_id == u.id or is_manager(db, u, a.team_id)
    ]


@router.post("/api/access")
def access_request(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    tid = int(data.get("team_id", 0))
    require(db.get(Team, tid) is not None)
    require(not can_view_board(db, u, tid), "Already have board access")
    current = db.query(BoardAccess).filter_by(user_id=u.id, team_id=tid).first()
    if current and current.status == "pending":
        raise HTTPException(409, "Already pending")
    if current:
        current.status = "pending"
        current.reason = str(data.get("reason", ""))
    else:
        db.add(BoardAccess(user_id=u.id, team_id=tid, reason=str(data.get("reason", ""))))
    audit(db, "board_access_requested", tid, actor=u)
    db.commit()
    return {"ok": True}


@router.post("/api/access/{access_id}/decision")
def access_decision(
    access_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    a = db.get(BoardAccess, access_id)
    if not a:
        raise HTTPException(404)
    require(is_manager(db, u, a.team_id))
    a.status = "approved" if data.get("approved") else "denied"
    audit(db, "board_access_" + a.status, a.team_id, actor=u, detail={"requester": a.user_id})
    db.commit()
    return {"ok": True}


@router.get("/api/admin")
def admin(u: User = Depends(current_user), db: Session = Depends(db_session)):
    require_role(u, "A")

    def source_info(s):
        record = row(
            s, ["id", "name", "slug", "type", "routing_chain", "enabled", "configuration"]
        )
        record["configuration"] = {
            k: (
                "[redacted]"
                if any(secret in k.lower() for secret in ("password", "secret", "token", "key"))
                else v
            )
            for k, v in (record["configuration"] or {}).items()
        }
        return record

    return {
        "sources": [source_info(s) for s in db.query(Source)],
        "teams": [team_dict(db, t) for t in db.query(Team)],
        "staff": [
            row(person, ["id", "name", "role"])
            for person in db.query(User)
            .filter(User.role.in_(["E", "M"]), User.debug_enabled.is_(False))
            .order_by(User.name)
        ],
        "metrics": {
            "events": db.query(Event).count(),
            "matched": db.query(Event).filter_by(state="matched").count(),
            "unmatched": db.query(Event).filter_by(state="unmatched").count(),
            "pending": db.query(Event).filter_by(state="pending").count(),
            "rejected": db.query(Event).filter_by(state="rejected").count(),
            "tickets": db.query(Ticket).count(),
            "deliveries_retrying": db.query(Delivery).filter_by(status="retrying").count(),
        },
    }


class ManagerAssignment(BaseModel):
    user_id: int
    previous_leader_id: int


@router.put("/api/admin/teams/{team_id}/manager")
def assign_team_manager(
    team_id: int,
    data: ManagerAssignment,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    require_role(u, "A")
    team = db.get(Team, team_id, with_for_update=True)
    if not team:
        raise HTTPException(404, "Team not found")
    employee = db.get(User, data.user_id, with_for_update=True)
    if not employee:
        raise HTTPException(404, "Employee not found")
    if employee.role not in ("E", "M") or employee.debug_enabled:
        raise HTTPException(422, "Choose an employee or team manager")
    if team.leader_id == employee.id:
        return {"ok": True}
    if team.leader_id != data.previous_leader_id:
        raise HTTPException(409, "The team manager changed. Review the current assignment.")
    if not db.get(Membership, (employee.id, team.id)):
        db.add(Membership(user_id=employee.id, team_id=team.id, maintainer=False))
    previous = team.leader_id
    employee.role = "M"
    team.leader_id = employee.id
    audit(
        db,
        "team_manager_assigned",
        team.id,
        actor=u,
        detail={"employee": employee.id, "previous_manager": previous},
    )
    db.commit()
    return {"ok": True}


class NewEmployee(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    phone: str = Field(default="", max_length=50)


@router.post("/api/admin/users", status_code=201)
def create_employee(
    data: NewEmployee, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    require_role(u, "A")
    employee = User(name=data.name, email=data.email, phone=data.phone, role="E")
    db.add(employee)
    try:
        db.flush()
        audit(db, "employee_created", actor=u, detail={"employee": employee.id})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An employee with this name already exists")
    return row(employee, ["id", "name", "email", "phone", "role"])


@router.post("/api/admin/sources")
def admin_source(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    require_role(u, "A")
    chain = data.get("routing_chain") or [t.id for t in db.query(Team).order_by(Team.position)]
    if not isinstance(chain, list) or any(db.get(Team, int(t)) is None for t in chain):
        raise HTTPException(422, "Unknown team in routing chain")
    stype = data.get("type", "webhook")
    if stype not in ("webhook", "broker", "log"):
        raise HTTPException(422, "Unknown source adapter")
    s = Source(
        name=str(data["name"]),
        slug=str(data["slug"]),
        type=stype,
        routing_chain=chain,
        configuration=data.get("configuration") or {},
    )
    db.add(s)
    db.flush()
    audit(db, "source_created", actor=u, detail={"source_id": s.id})
    db.commit()
    return {"id": s.id}


@router.put("/api/admin/sources/{source_id}")
def admin_source_update(
    source_id: int,
    data: dict = Body(...),
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    require_role(u, "A")
    s = db.get(Source, source_id)
    if not s:
        raise HTTPException(404)
    for key in ("routing_chain", "configuration", "enabled", "name"):
        if key in data:
            setattr(s, key, data[key])
    audit(db, "source_updated", actor=u, detail={"source_id": s.id})
    db.commit()
    return {"ok": True}
