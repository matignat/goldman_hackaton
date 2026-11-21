"""Permanently remove platform records inside the caller's transaction."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Activity,
    Audit,
    BoardAccess,
    DeletedRule,
    Delivery,
    Event,
    Membership,
    Policy,
    Proposal,
    Rule,
    Runbook,
    Source,
    Team,
    Ticket,
    User,
)


def erase_team(db: Session, team: Team) -> None:
    """Delete team-owned work, retaining employees and source event history."""
    team_id = team.id
    tickets = select(Ticket.id).where(Ticket.team_id == team_id)
    rules = select(Rule.id).where(Rule.team_id == team_id)
    runbooks = select(Runbook.id).where(Runbook.team_id == team_id)
    for source in db.query(Source).with_for_update():
        chain = source.routing_chain or []
        remaining = [entry for entry in chain if str(entry) != str(team_id)]
        if remaining != chain:
            source.routing_chain = remaining
            if not remaining:
                # Empty chains otherwise default to every team.
                source.enabled = False

    db.query(Event).filter(Event.ticket_id.in_(tickets)).update(
        {Event.ticket_id: None}, synchronize_session=False
    )
    db.query(Ticket).filter(Ticket.recurrence_of.in_(tickets)).update(
        {Ticket.recurrence_of: None}, synchronize_session=False
    )
    db.query(Ticket).filter(Ticket.rule_id.in_(rules)).update(
        {Ticket.rule_id: None}, synchronize_session=False
    )
    db.query(Rule).filter(Rule.runbook_id.in_(runbooks)).update(
        {Rule.runbook_id: None}, synchronize_session=False
    )
    db.query(Delivery).filter(Delivery.ticket_id.in_(tickets)).delete(synchronize_session=False)
    db.query(DeletedRule).filter(DeletedRule.rule_id.in_(rules)).delete(synchronize_session=False)
    for model in (
        Proposal,
        Activity,
        Audit,
        BoardAccess,
        Membership,
        Policy,
        Ticket,
        Rule,
        Runbook,
    ):
        db.query(model).filter_by(team_id=team_id).delete(synchronize_session=False)
    db.delete(team)


def erase_employee(db: Session, employee: User) -> None:
    """Remove access and operational assignments while preserving other users' tickets."""
    uid = employee.id
    for ticket in db.query(Ticket).with_for_update():
        if uid in (ticket.members or []):
            ticket.members = [member for member in ticket.members if member != uid]
    for rule in db.query(Rule).with_for_update():
        recipients = (rule.routing or {}).get("recipients")
        if isinstance(recipients, list) and any(str(entry) == str(uid) for entry in recipients):
            rule.routing = {
                **rule.routing,
                "recipients": [entry for entry in recipients if str(entry) != str(uid)],
            }
    db.query(Ticket).filter_by(done_by=uid).update({Ticket.done_by: None})
    db.query(Ticket).filter_by(approved_by=uid).update({Ticket.approved_by: None})
    db.query(User).filter_by(manager_id=uid).update({User.manager_id: None})
    db.query(Audit).filter_by(actor_id=uid).update(
        {Audit.actor_id: None, Audit.actor: "Deleted employee"}
    )
    db.query(Policy).filter_by(target=f"user:{uid}").delete()
    db.query(Proposal).filter_by(requester_id=uid).delete()
    for model in (Delivery, Membership, BoardAccess):
        db.query(model).filter_by(user_id=uid).delete(synchronize_session=False)
    db.delete(employee)
