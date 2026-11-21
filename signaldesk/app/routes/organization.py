"""SignalDesk organization."""

import re
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, union_all
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
    Audit,
    BoardAccess,
    Delivery,
    Event,
    ExplicitManagerAssignment,
    Membership,
    Policy,
    Source,
    Team,
    Ticket,
    User,
)
from ..services.deletion import erase_employee, erase_team
from ..services.records import audit
from .presentation import row, team_dict
from .profile_data import ProfileData

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
        explicit = select(ExplicitManagerAssignment.user_id)
        return db.query(User).filter(
            User.id != manager_id,
            (User.id.in_(explicit) & (User.manager_id == manager_id))
            | (User.id.not_in(explicit) & User.id.in_(members)),
        )

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
            row(person, ["id", "name", "role", "manager_id"])
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


class NewTeam(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    leader_id: int


@router.post("/api/admin/teams", status_code=201)
def create_team(data: NewTeam, u: User = Depends(current_user), db: Session = Depends(db_session)):
    require_role(u, "A")
    leader = db.get(User, data.leader_id, with_for_update=True)
    if leader is None or leader.role not in ("E", "M") or leader.debug_enabled:
        raise HTTPException(422, "Select a staff member to lead the team")
    prefix = re.sub(r"[^a-z0-9]+", "-", data.name.lower()).strip("-")[:47] or "team"
    team = Team(name=data.name, slug=f"{prefix}-{uuid4().hex[:12]}", leader_id=leader.id)
    if db.get_bind().dialect.name == "sqlite":
        # Historical routing traces and signed manifests must not refer to a replacement team.
        ids = union_all(
            select(Team.id.label("tid")),
            select(Audit.detail["team"].as_integer()).where(Audit.action == "team_deleted"),
        ).subquery()
        team.id = select(func.coalesce(func.max(ids.c.tid), 0) + 1).scalar_subquery()
    try:
        db.add(team)
        db.flush()
        leader.role = "M"
        db.add(Membership(team_id=team.id, user_id=leader.id))
        db.add(Policy(team_id=team.id, severity="critical", channel="ui"))
        audit(db, "team_created", team.id, actor=u, detail={"leader": leader.id})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A team with this name already exists")
    return team_dict(db, team)


class DirectManagerAssignment(BaseModel):
    manager_id: int | None


@router.put("/api/admin/users/{user_id}/manager")
def assign_direct_manager(
    user_id: int,
    data: DirectManagerAssignment,
    u: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    require_role(u, "A")
    # Lock the manager hierarchy in a fixed order so simultaneous reassignments cannot form a cycle.
    staff = {
        person.id: person
        for person in db.query(User)
        .filter((User.role == "M") | (User.id == user_id))
        .order_by(User.id)
        .with_for_update()
    }
    person = staff.get(user_id)
    if person is None or person.role not in ("E", "M") or person.debug_enabled:
        raise HTTPException(404, "Staff member not found")
    if data.manager_id is not None:
        manager = staff.get(data.manager_id)
        if manager is None or manager.role != "M" or manager.debug_enabled:
            raise HTTPException(422, "Choose an existing manager")
        seen = {person.id}
        while manager is not None:
            if manager.id in seen:
                raise HTTPException(409, "This assignment would create a reporting cycle")
            seen.add(manager.id)
            manager = staff.get(manager.manager_id) if manager.manager_id is not None else None
    previous = person.manager_id
    person.manager_id = data.manager_id
    if db.get(ExplicitManagerAssignment, user_id) is None:
        db.add(ExplicitManagerAssignment(user_id=user_id))
    audit(
        db,
        "direct_manager_assigned",
        actor=u,
        detail={"employee": user_id, "previous_manager": previous, "manager": data.manager_id},
    )
    db.commit()
    return {"ok": True}


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


class NewEmployee(ProfileData):
    """Personal details for a new employee account."""


@router.post("/api/admin/users", status_code=201)
def create_employee(
    data: NewEmployee, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    require_role(u, "A")
    employee = User(name=data.name, email=data.email, phone=data.phone, role="E")
    if db.get_bind().dialect.name == "sqlite":
        # Existing demos use SQLite ROWIDs, which can reuse the highest deleted ID.
        # Allocate in the INSERT so old signed sessions cannot identify a new employee.
        ids = union_all(
            select(User.id.label("uid")),
            select(Audit.detail["employee"].as_integer()).where(
                Audit.action == "employee_deleted"
            ),
        ).subquery()
        employee.id = select(func.coalesce(func.max(ids.c.uid), 0) + 1).scalar_subquery()
    db.add(employee)
    try:
        db.flush()
        audit(db, "employee_created", actor=u, detail={"employee": employee.id})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An employee with this name already exists")
    return row(employee, ["id", "name", "email", "phone", "role"])


@router.delete("/api/admin/teams/{team_id}", status_code=204)
def delete_team(team_id: int, u: User = Depends(current_user), db: Session = Depends(db_session)):
    require_role(u, "A")
    team = db.get(Team, team_id, with_for_update=True)
    if team is None:
        raise HTTPException(404, "Team not found")
    erase_team(db, team)
    audit(db, "team_deleted", actor=u, detail={"team": team_id})
    db.commit()


@router.delete("/api/admin/users/{user_id}", status_code=204)
def delete_employee(
    user_id: int, u: User = Depends(current_user), db: Session = Depends(db_session)
):
    require_role(u, "A")
    employee = db.get(User, user_id, with_for_update=True)
    if employee is None:
        raise HTTPException(404, "Employee not found")
    require(employee.role in ("E", "M") and not employee.debug_enabled, "Choose a staff account")
    if db.query(Team.id).filter_by(leader_id=user_id).first():
        raise HTTPException(409, "Assign a new manager to each team this employee leads first.")
    erase_employee(db, employee)
    audit(db, "employee_deleted", actor=u, detail={"employee": user_id})
    db.commit()


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
