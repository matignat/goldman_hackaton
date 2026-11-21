import pytest

from app.database import Session
from app.models import Audit, Delivery, Event, Membership, Rule, Source, Team, Ticket, User


@pytest.mark.parametrize("persona", [1, 2, 6])
@pytest.mark.parametrize("target", ["teams/1", "users/2"])
def test_only_admin_can_delete(login, persona, target):
    assert login(persona).delete("/api/admin/" + target).status_code == 403


def test_team_deletion_cleans_owned_work_and_preserves_other_teams(login):
    client = login(5)
    with Session.begin() as db:
        source = db.query(Source).filter_by(slug="demo").one()
        source.routing_chain = [1]
        tickets = [ticket.id for ticket in db.query(Ticket).filter_by(team_id=1)]
        assert tickets
        staff_count = db.query(User).count()
    assert client.delete("/api/admin/teams/1").status_code == 204
    assert client.delete("/api/admin/teams/1").status_code == 404
    with Session() as db:
        assert db.get(Team, 1) is None and db.get(Team, 2)
        assert db.query(User).count() == staff_count
        assert not db.query(Ticket).filter_by(team_id=1).count()
        assert not db.query(Event).filter(Event.ticket_id.in_(tickets)).count()
        assert not db.query(Delivery).filter(Delivery.ticket_id.in_(tickets)).count()
        assert not db.query(Rule).filter_by(team_id=1).count()
        assert not db.query(Membership).filter_by(team_id=1).count()
        assert not db.query(Source).filter_by(slug="demo").one().enabled
        assert all(1 not in source.routing_chain for source in db.query(Source))
        assert db.query(Audit).filter_by(action="team_deleted").count() == 1


def test_employee_erasure_and_session_invalidation(login):
    client = login(5)
    assert client.delete("/api/admin/users/1").status_code == 409
    assert client.delete("/api/admin/users/5").status_code == 403
    data = {"name": "Temporary employee", "email": "employee@example.test"}
    uid = client.post("/api/admin/users", json=data).json()["id"]
    login(uid)
    old_cookie = client.cookies.get("signaldesk_session")
    login(5)
    assert client.post("/api/teams/1/members", json={"action": "add", "user_id": uid}).is_success
    assert client.delete(f"/api/admin/users/{uid}").status_code == 204
    # Old session cookies on SQLite must never regain access, even after recreation.
    assert client.post("/api/admin/users", json=data).json()["id"] != uid
    assert (
        client.get("/api/me", headers={"Cookie": f"signaldesk_session={old_cookie}"}).status_code
        == 401
    )
    with Session() as db:
        assert db.get(User, uid) is None
        assert not db.query(Membership).filter_by(user_id=uid).count()


def test_delete_employee_with_active_ticket_and_deliveries(login):
    client = login(5)
    with Session() as db:
        ticket = next(t for t in db.query(Ticket) if 3 in t.members)
        ticket_id = ticket.id
        assert db.query(Delivery).filter_by(user_id=3).count()
    assert client.delete("/api/admin/users/3").status_code == 204
    with Session() as db:
        assert 3 not in db.get(Ticket, ticket_id).members
        assert not db.query(Delivery).filter_by(user_id=3).count()
        assert not db.query(Membership).filter_by(user_id=3).count()
