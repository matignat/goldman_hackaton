import copy
import json

import pytest

from app.database import Session
from app.models import Audit, Delivery, Event, Rule, Source, Ticket, User
from app.seed import PAYMENTS, WORKFLOW
from app.services.delivery import RETRY_SECONDS, process_deliveries
from app.services.ingestion import ingest
from app.services.routing import pump_events


def test_delivery_preferences_require_routes_for_every_type(login):
    client = login(2)
    profile = client.get("/api/me").json()
    mapping = profile["ticket_channels"]
    assert set(mapping) == set(profile["ticket_categories"])
    for invalid in ({}, {**mapping, "task": []}, {**mapping, "error": ["unknown"]}):
        assert client.put("/api/preferences", json={"ticket_channels": invalid}).status_code == 422
    mapping.update(task=["phone", "sms", "phone"], change_request=["email"])
    assert client.put("/api/preferences", json={"ticket_channels": mapping}).status_code == 200
    assert client.put("/api/preferences", json={"timezone": "UTC"}).status_code == 200
    assert client.get("/api/me").json()["ticket_channels"]["task"] == ["phone", "sms"]
    task = client.post("/api/tickets", json={"team_id": 1, "title": "Delivery check"}).json()
    with Session() as db:
        jobs = db.query(Delivery).filter_by(user_id=2, ticket_id=task["id"], kind="new_ticket")
        assert sorted(job.channel for job in jobs) == ["phone", "sms"]


def test_seed_matches_all_appendix_schemas_and_falls_through_teams():
    with Session() as db:
        events = db.query(Event).order_by(Event.id).all()
        assert len(events) == 3 and all(x.state == "matched" for x in events)
        assert [x.ticket_id for x in events] == [1, 2, 3]
        assert [len(x.route_trace) for x in events] == [2, 1, 3]
        assert events[0].route_trace[0]["team_id"] == 1
        assert events[0].route_trace[-1]["team_id"] == 2
        assert db.get(Ticket, 1).schema == "WORKFLOW_FAILED"
        assert db.get(Ticket, 2).severity == "critical"
        assert db.get(Ticket, 3).first_payload["payload"]["notional"] == 25000000


def test_flood_500_events_is_one_ticket_and_one_initial_notification():
    with Session() as db:
        s = db.query(Source).filter_by(slug="demo").one()
        old = db.get(Ticket, 2).occurrences
        data = json.dumps(PAYMENTS)
        for _ in range(500):
            ingest(db, s, data)
        db.commit()
        while pump_events(db, limit=500):
            db.commit()
        matching = db.query(Ticket).filter_by(title="High error rate · payments-api").all()
        assert len(matching) == 1
        assert matching[0].occurrences == old + 500
        assert len(matching[0].samples) <= 20
        assert db.query(Event).filter_by(state="matched", ticket_id=2).count() == 501
        # Delivery jobs are not created 500 times for this incident.
        assert db.query(Delivery).filter_by(ticket_id=2, kind="new_ticket").count() <= 4


def test_unmatched_event_survives_and_invalid_payload_is_rejected(login):
    client = login(6)
    r = client.post(
        "/api/debug/generate",
        json={"count": 1, "event": {"brandNew": {"kind": "NO_RULE", "nested": [1, {"x": 2}]}}},
    )
    assert r.status_code == 200
    with Session() as db:
        unmatched = db.query(Event).filter_by(state="unmatched").one()
        assert len(unmatched.route_trace) == 4
        assert unmatched.raw == '{"brandNew": {"kind": "NO_RULE", "nested": [1, {"x": 2}]}}'
        assert unmatched.ticket_id is None
    r = client.post("/api/ingest/webhook", content=b"{ invalid json")
    assert r.status_code == 200
    with Session() as db:
        assert db.query(Event).filter_by(state="rejected").count() == 1


def test_log_line_source_parser_and_pricing_grouping(login):
    client = login(1)
    a = "WARN pricing-engine-v2 instrument=EURUSD High cache latency"
    b = "WARN pricing-engine-v2 instrument=EURUSD Cache eviction retries"
    r1 = client.post("/api/ingest/logs", content=a.encode())
    r2 = client.post("/api/ingest/logs", content=b.encode())
    assert r1.status_code == 200 and r2.status_code == 200
    with Session() as db:
        x = db.get(Event, r1.json()["event_id"])
        y = db.get(Event, r2.json()["event_id"])
        assert x.state == "matched" and y.state == "matched"
        assert x.ticket_id == y.ticket_id
        t = db.get(Ticket, x.ticket_id)
        assert t.occurrences == 2 and t.team_id == 4


def test_recovery_to_done_then_relapse_before_approval(login):
    client = login(6)
    recovery = copy.deepcopy(PAYMENTS)
    recovery["status"] = "resolved"
    client.post("/api/debug/generate", json={"event": recovery})
    with Session() as db:
        ticket = db.get(Ticket, 2)
        assert ticket.state == "done" and ticket.incident_status == "resolved"
        assert "attention" in ticket.flags and ticket.done_by is None
    client.post("/api/debug/generate", json={"event": PAYMENTS})
    with Session() as db:
        t = db.get(Ticket, 2)
        assert t.state == "in_progress" and t.incident_status == "firing"
        assert "relapse" in t.flags and t.occurrences == 2


def test_manager_can_approve_own_incident(login):
    client = login(1)
    path = "/api/tickets/2"
    assert client.post(path + "/move", json={"state": "done"}).status_code == 409
    complete_runbook(login, 2, 3)
    client = login(1)
    assert client.post(path + "/move", json={"state": "done"}).status_code == 200
    ticket = client.get(path).json()
    assert ticket["type"] == "incident" and ticket["permissions"]["can_approve"]
    assert client.post(path + "/approve").status_code == 200
    with Session() as db:
        incident = db.get(Ticket, 2)
        assert (incident.state, incident.done_by, incident.approved_by) == ("archived", 1, 1)
        assert incident.active_key is None


def complete_runbook(login, ticket_id, worker):
    client = login(worker)
    path = f"/api/tickets/{ticket_id}"
    ticket = client.get(path).json()
    if ticket["state"] == "acknowledged":
        assert client.post(path + "/move", json={"state": "in_progress"}).status_code == 200
    assert client.post(path + "/move", json={"state": "done"}).status_code == 409
    for index, step in enumerate(ticket["runbook_steps"]):
        url = f"{path}/steps/{index}"
        assert client.post(url + "/run").status_code == 200
        if step.get("approval") == "required":
            assert login(1).post(url + "/approve").status_code == 200
            client = login(worker)
            assert client.get(path).json()["runbook_steps"][index]["status"] == "done"


def test_maintainer_needs_another_approver(login):
    complete_runbook(login, 2, 3)
    client = login(3)
    assert client.post("/api/tickets/2/move", json={"state": "done"}).status_code == 200
    response = client.post("/api/tickets/2/approve")
    assert response.status_code == 403 and "Separation of duties" in response.text
    client = login(1)  # Team manager approves the maintainer's resolution
    assert client.post("/api/tickets/2/approve").status_code == 200
    with Session() as db:
        t = db.get(Ticket, 2)
        assert t.state == "archived" and t.active_key is None and t.approved_by == 1
    client = login(2)
    assert client.get("/api/archive").status_code == 403
    assert client.get("/api/tickets/2").status_code == 403  # was not personally handling


def test_team_manager_can_complete_and_approve_team_task(login):
    client = login(2)
    task = client.post("/api/tickets", json={"team_id": 1, "title": "Review thresholds"})
    task.raise_for_status()
    path = "/api/tickets/" + str(task.json()["id"])
    client = login(1)
    assert client.post(path + "/move", json={"state": "done"}).status_code == 200
    assert client.get(path).json()["permissions"]["can_approve"]
    assert client.post(path + "/approve").status_code == 200
    with Session() as db:
        saved = db.get(Ticket, task.json()["id"])
        assert (saved.state, saved.done_by, saved.approved_by) == ("archived", 1, 1)


def test_risky_runbook_action_is_manager_only(login):
    manager = login(1)
    assert manager.post("/api/tickets/1/steps/0/run").status_code == 403
    client = login(2)
    assert client.post("/api/tickets/1/steps/2/run").status_code == 409
    assert client.post("/api/tickets/1/move", json={"state": "in_progress"}).status_code == 200
    assert client.post("/api/tickets/1/steps/2/run").status_code == 200
    with Session() as db:
        t = db.get(Ticket, 1)
        assert t.runbook_steps[2]["status"] == "waiting_approval"
    client = login(3)
    assert client.post("/api/tickets/1/steps/2/approve").status_code == 403
    assert client.post("/api/tickets/1/steps/2/reject").status_code == 403
    client = login(1)
    assert client.post("/api/tickets/1/steps/2/reject").status_code == 200
    assert client.post("/api/tickets/1/steps/2/approve").status_code == 409
    assert login(2).post("/api/tickets/1/steps/2/run").status_code == 200
    client = login(1)
    assert client.post("/api/tickets/1/steps/2/approve").status_code == 200
    assert client.post("/api/tickets/1/steps/2/approve").status_code == 409
    with Session() as db:
        assert db.get(Ticket, 1).runbook_steps[2]["status"] == "done"


def test_later_steps_wait_for_approved_completion(login):
    with Session() as db:
        ticket = db.get(Ticket, 1)
        ticket.runbook_steps = [
            dict(ticket.runbook_steps[0], approval="required"),
            *ticket.runbook_steps[1:],
        ]
        db.commit()
    path = "/api/tickets/1"
    first = path + "/steps/0"
    later = path + "/steps/1/run"
    worker = login(2)
    assert worker.post(path + "/move", json={"state": "in_progress"}).status_code == 200
    assert worker.get(path).json()["approval_gate_index"] == 0
    assert worker.post(later).status_code == 409
    assert worker.post(first + "/run").status_code == 200
    assert worker.post(path + "/steps/2/run").status_code == 409
    assert login(1).post(first + "/reject").status_code == 200
    worker = login(2)
    assert worker.post(later).status_code == 409
    assert worker.post(first + "/run").status_code == 200
    assert login(1).post(first + "/approve").status_code == 200
    worker = login(2)
    assert worker.get(path).json()["approval_gate_index"] == 2
    assert worker.post(later).status_code == 200


def test_manager_only_rule_runbook_and_proposals(login):
    client = login(3)  # BM
    assert (
        client.post(
            "/api/rules", json={"team_id": 1, "name": "No access", "condition": {}}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/runbooks", json={"team_id": 1, "name": "No access", "source_text": "Text"}
        ).status_code
        == 403
    )
    client = login(2)
    body = {
        "team_id": 1,
        "target_type": "rule",
        "target_id": None,
        "description": "Please route risk failures",
    }
    one = client.post("/api/proposals", json=body)
    assert one.status_code == 200
    assert client.post("/api/proposals", json=body).status_code == 409
    client = login(3)
    assert (
        client.post(
            f"/api/proposals/{one.json()['id']}/decision", json={"decision": "accepted"}
        ).status_code
        == 403
    )
    client = login(1)
    assert (
        client.post(
            f"/api/proposals/{one.json()['id']}/decision",
            json={"decision": "rejected", "comment": "Revise condition"},
        ).status_code
        == 200
    )
    client = login(2)
    two = client.post("/api/proposals", json={**body, "description": "Updated rule condition"})
    assert two.status_code == 200 and two.json()["id"] != one.json()["id"]
    with Session() as db:
        from app.models import Proposal

        assert db.get(Proposal, two.json()["id"]).revised_from == one.json()["id"]


def test_archive_critical_only_default_toggle_errors_and_handled_history(login):
    # This archive fixture has no runbook action that changes its severity.
    with Session() as db:
        db.get(Ticket, 1).runbook_steps = []
        db.commit()
    client = login(1)
    assert client.post("/api/tickets/1/move", json={"state": "done"}).status_code == 200
    client = login(1)
    assert client.post("/api/tickets/1/approve").status_code == 200
    default = client.get("/api/archive?team=2").json()
    expanded = client.get("/api/archive?team=2&include_errors=true").json()
    assert default == []
    assert len(expanded) == 1 and expanded[0]["id"] == 1
    client = login(2)
    assert client.get("/api/tickets/1").status_code == 200
    assert any(t["id"] == 1 for t in client.get("/api/tickets?handled=true").json())
    assert client.get("/api/archive?include_errors=true").status_code == 403


def test_admin_is_not_debug_and_debug_is_not_admin(login):
    client = login(5)
    assert client.get("/api/admin").status_code == 200
    assert client.get("/api/debug").status_code == 403
    assert client.post("/api/debug/generate", json={"count": 1}).status_code == 403
    client = login(6)
    assert client.get("/api/debug").status_code == 200
    assert client.get("/api/admin").status_code == 403
    assert (
        client.post(
            "/api/admin/sources", json={"name": "Injected", "slug": "bad", "type": "webhook"}
        ).status_code
        == 403
    )
    assert client.post("/api/rules", json={"team_id": 1, "name": "Not allowed"}).status_code == 403


def test_manager_must_keep_one_policy_per_team(login):
    manager = login(1)
    last = manager.get("/api/policies?team=2").json()[0]["id"]
    assert manager.delete(f"/api/policies/{last}").status_code == 409
    assert (
        manager.post(
            "/api/policies", json={"team_id": 2, "severity": "error", "channel": "phone"}
        ).status_code
        == 200
    )
    assert login(2).delete(f"/api/policies/{last}").status_code == 403
    manager = login(1)
    assert manager.delete(f"/api/policies/{last}").status_code == 200
    remaining = manager.get("/api/policies?team=2").json()
    assert [p["channel"] for p in remaining] == ["phone"]


def test_employee_severity_preferences_route_matching_tickets(login):
    from app.services.delivery import queue_ticket

    client = login(2)
    settings = client.get("/api/me").json()["ticket_channels"]
    settings.update(critical=["phone", "sms"], error=["sms"])
    assert client.put("/api/preferences", json={"ticket_channels": settings}).status_code == 200
    with Session() as db:
        for ticket_id, expected in [(2, ["phone", "sms"]), (1, ["sms", "ui"])]:
            ticket = db.get(Ticket, ticket_id)
            queue_ticket(db, ticket, "severity_test")
            jobs = db.query(Delivery).filter_by(
                user_id=2, ticket_id=ticket_id, kind="severity_test"
            )
            assert sorted(job.channel for job in jobs) == expected


def email_routes(client):
    return {category: ["email"] for category in client.get("/api/me").json()["ticket_categories"]}


def test_failed_channel_retries_without_unselected_fallback(login):
    client = login(2)
    assert (
        client.put("/api/preferences", json={"ticket_channels": email_routes(client)}).status_code
        == 200
    )
    client = login(6)
    assert (
        client.post("/api/debug/channel", json={"name": "email", "failing": True}).status_code
        == 200
    )
    payload = copy.deepcopy(WORKFLOW)
    payload["workflowId"] = "WF-NOTIFY-UNIQUE"
    r = client.post("/api/debug/generate", json={"event": payload})
    assert r.status_code == 200
    old = RETRY_SECONDS[:]
    RETRY_SECONDS[:] = [0, 0, 0, 0, 0]
    try:
        with Session() as db:
            t = db.query(Ticket).filter_by(title="Settlement failure · WF-NOTIFY-UNIQUE").one()
            primary = (
                db.query(Delivery).filter_by(ticket_id=t.id, user_id=2, channel="email").one()
            )
            from app.models import now

            for _ in range(8):
                primary.available_at = now()
                process_deliveries(db)
                db.commit()
            assert primary.status == "failed" and primary.attempts == 6
            assert db.query(Delivery).filter_by(fallback_for=primary.id).count() == 0
    finally:
        RETRY_SECONDS[:] = old


def test_guest_board_access_does_not_grant_modification(login):
    client = login(7)  # Rates only
    assert client.get("/api/board?team=1").status_code == 403
    r = client.post("/api/access", json={"team_id": 1, "reason": "Cross-team awareness"})
    assert r.status_code == 200
    with Session() as db:
        from app.models import BoardAccess

        request_id = db.query(BoardAccess).filter_by(user_id=7, team_id=1).one().id
    client = login(1)
    assert (
        client.post(f"/api/access/{request_id}/decision", json={"approved": True}).status_code
        == 200
    )
    client = login(7)
    assert client.get("/api/board?team=1").status_code == 200
    assert client.post("/api/tickets/2/join").status_code == 403
    assert (
        client.post("/api/tickets/2/comment", json={"comment": "guest write"}).status_code == 403
    )


def test_rule_editor_unknown_nested_schema_at_runtime(login):
    client = login(1)
    cond = {
        "all": [
            {"field": "envelope.signal.details.identifier", "op": "gte", "value": 40},
            {
                "any": [
                    {"field": "envelope.facility.component", "op": "eq", "value": "risk-engine"},
                    {"field": "envelope.kind", "op": "wildcard", "value": "CUSTOM_*"},
                ]
            },
        ]
    }
    data = {
        "team_id": 1,
        "name": "New schema rule",
        "priority": 1,
        "condition": cond,
        "presentation": {
            "title": "New: {{envelope.facility.component}}",
            "body": "{{envelope.signal.details.text}}",
        },
        "dedup_template": "{{envelope.facility.instance}}:{{envelope.kind}}",
    }
    assert client.post("/api/rules", json=data).status_code == 200
    payload = {
        "envelope": {
            "kind": "CUSTOM_UPSTREAM_FAILURE",
            "facility": {"component": "risk-engine", "instance": "risk-eu-4"},
            "signal": {"details": {"identifier": 42, "text": "New schema matched"}},
        }
    }
    check = client.post(
        "/api/rules/test", json={"team_id": 1, "rule": data, "event": payload}
    ).json()
    assert check["matched"] and check["title"] == "New: risk-engine"
    client = login(6)
    assert client.post("/api/debug/generate", json={"event": payload}).status_code == 200
    with Session() as db:
        ticket = db.query(Ticket).filter_by(title="New: risk-engine").one()
        assert ticket.team_id == 1 and ticket.first_payload == payload


def test_semantic_markup_and_ui_assets(client):
    html = client.get("/").text
    js = client.get("/assets/app.js").text
    css = client.get("/assets/styles.css").text
    for id in (
        "app-header",
        "app-nav",
        "app-main",
        "team-selector",
        "kanban-board",
        "counter-total",
        "column-new",
        "column-done",
        "recent-activity",
        "rules-list",
        "runbooks-list",
        "debug-event-generator",
        "delivery-log",
        "ticket-detail",
        "org-manager",
    ):
        assert f'id="{id}"' in html
    assert "EventSource(" in js and "data-action" in html
    assert "--gold:" in css and "--navy:" in css and "data-severity" in css


def test_task_policy_queues_external_channel_immediately(login):
    manager = login(1)
    assert (
        manager.post(
            "/api/policies", json={"team_id": 2, "severity": "task", "channel": "phone"}
        ).status_code
        == 200
    )
    worker = login(2)
    with Session() as db:
        user = db.get(User, 2)
        user.buffer_seconds = 3600
        user.schedule = {"start": "00:01", "end": "00:02", "timezone": "UTC"}
        db.commit()
    ids = [
        worker.post("/api/tickets", json={"team_id": 2, "title": str(i)}).json()["id"]
        for i in range(2)
    ]
    with Session() as db:
        jobs = (
            db.query(Delivery)
            .filter(
                Delivery.ticket_id.in_(ids), Delivery.user_id == 2, Delivery.channel == "phone"
            )
            .all()
        )
        assert sorted(job.ticket_id for job in jobs) == sorted(ids)
        assert all(job.status == "queued" and "digest" not in job.content for job in jobs)
        process_deliveries(db)
        assert all(job.status == "sent" for job in jobs)


def test_manager_signed_live_rule_config_is_atomic_and_auth_checked(login, tmp_path):
    client = login(5)
    assert (
        client.post("/api/rules/sign-config", json={"team_id": 1, "rules": []}).status_code == 403
    )
    client = login(1)
    manifest = client.post(
        "/api/rules/sign-config",
        json={
            "team_id": 1,
            "rules": [
                {
                    "name": "Approved file rule",
                    "condition": {"field": "new.custom.identifier", "op": "eq", "value": "X"},
                    "priority": 2,
                    "presentation": {
                        "title": "Approved {{new.custom.identifier}}",
                        "body": "from signed file",
                    },
                }
            ],
        },
    )
    assert manifest.status_code == 200, manifest.text
    from app.services.config_reload import (
        apply_signed_manifest,
        reload_approved_rule_files,
    )

    fake = {**manifest.json(), "rules": [{"name": "Sneaky", "condition": {}}]}
    with Session() as db:
        try:
            apply_signed_manifest(db, fake)
        except ValueError:
            db.rollback()
        else:
            assert False, "tampered file was accepted"
    config_path = tmp_path / "rules.json"
    config_path.write_text(json.dumps(manifest.json()))
    reload_approved_rule_files(Session, tmp_path)
    with Session() as db:
        rule = db.query(Rule).filter_by(name="Approved file rule").one()
        assert rule.team_id == 1 and rule.priority == 2
    # A manager can sign a revised file. Changes take effect on the NEXT event.
    client = login(1)
    revised = {**manifest.json()["rules"][0], "priority": 3}
    signature = client.post(
        "/api/rules/sign-config", json={"team_id": 1, "rules": [revised]}
    ).json()
    config_path.write_text(json.dumps(signature))
    reload_approved_rule_files(Session, tmp_path)
    with Session() as db:
        rule = db.query(Rule).filter_by(name="Approved file rule").one()
        assert rule.priority == 3 and rule.version == 2


def test_explicit_manager_only_history_purge_retains_tombstone(login):
    complete_runbook(login, 1, 2)
    client = login(2)
    assert client.post("/api/tickets/1/move", json={"state": "done"}).status_code == 200
    client = login(1)
    assert client.post("/api/tickets/1/approve").status_code == 200
    assert (
        client.post("/api/tickets/1/purge", json={"confirmation": "not confirmed"}).status_code
        == 422
    )
    client = login(3)
    assert (
        client.post("/api/tickets/1/purge", json={"confirmation": "PURGE TKT-00001"}).status_code
        == 403
    )
    client = login(1)
    assert (
        client.post("/api/tickets/1/purge", json={"confirmation": "PURGE TKT-00001"}).status_code
        == 200
    )
    assert client.get("/api/tickets/1").status_code == 410
    with Session() as db:
        assert db.get(Ticket, 1).state == "purged"
        assert all(e.raw == "[explicitly purged]" for e in db.query(Event).filter_by(ticket_id=1))
        assert (
            db.query(Audit).filter_by(ticket_id=1, action="ticket_purged_tombstone").count() == 1
        )


def test_delivery_log_pages_visible_rows_before_applying_limit(login):
    with Session() as db:
        for i in range(180):
            db.add(
                Delivery(
                    user_id=2 if i < 52 else 7,
                    channel="ui",
                    kind="page_test",
                    idempotency_key=f"page-test-{i}",
                )
            )
        db.commit()
        expected = [
            job.id for job in db.query(Delivery).filter_by(user_id=2).order_by(Delivery.id.desc())
        ]
    client = login(2)
    received = []
    for page in range(1, (len(expected) + 24) // 25 + 1):
        result = client.get("/api/delivery", params={"page": page}).json()
        assert result["total"] == len(expected)
        ids = [job["id"] for job in result["items"]]
        assert ids == expected[(page - 1) * 25 : page * 25]
        received.extend(ids)
    assert received == expected


def test_admin_onboards_employee_and_manager_controls_team_roster(login):
    data = {"name": "New Employee", "email": "new@example.org"}
    assert login(1).post("/api/admin/users", json=data).status_code == 403
    admin = login(5)
    response = admin.post("/api/admin/users", json=data)
    assert response.status_code == 201
    employee = response.json()
    assert employee["role"] == "E"
    assert admin.post("/api/admin/users", json=data).status_code == 409
    manager = login(1)
    assert employee["id"] in [
        e["id"] for e in manager.get("/api/organization").json()["employees"]
    ]
    payload = {"user_id": employee["id"], "action": "add"}
    assert login(4).post("/api/teams/1/members", json=payload).status_code == 403
    assert login(1).post("/api/teams/1/members", json=payload).status_code == 200

    def report_ids():
        return [person["id"] for person in manager.get("/api/organization").json()["reports"]]

    assert employee["id"] in report_ids()
    assert manager.post("/api/teams/2/members", json=payload).status_code == 200
    assert report_ids().count(employee["id"]) == 1
    assert 1 in [team["id"] for team in login(employee["id"]).get("/api/me").json()["teams"]]
    payload["action"] = "remove"
    assert login(1).post("/api/teams/1/members", json=payload).status_code == 200
    assert employee["id"] in report_ids()
    assert manager.post("/api/teams/2/members", json=payload).status_code == 200
    assert employee["id"] not in report_ids()
    assert login(employee["id"]).get("/api/me").json()["teams"] == []


def test_direct_report_remains_until_last_managed_team_membership_ends(login):
    manager = login(1)

    def reports():
        return [person["id"] for person in manager.get("/api/organization").json()["reports"]]

    def change(team, action):
        response = manager.post(
            f"/api/teams/{team}/members", json={"user_id": 2, "action": action}
        )
        assert response.status_code == 200

    assert reports().count(2) == 1
    change(1, "remove")
    assert 2 in reports()
    change(2, "remove")
    assert 2 not in reports()
    change(1, "add")
    assert 2 in reports()


def test_manager_deletes_rule_preserving_ticket_history(login):
    manager = login(1)
    with Session() as db:
        rule_id = db.get(Ticket, 2).rule_id
    url = f"/api/rules/{rule_id}"
    assert login(4).delete(url + "?permanent=true").status_code == 403
    manager = login(1)
    assert manager.delete(url).status_code == 200
    assert any(
        r["id"] == rule_id and not r["enabled"] for r in manager.get("/api/rules?team=1").json()
    )
    assert manager.delete(url + "?permanent=true").status_code == 200
    assert all(r["id"] != rule_id for r in manager.get("/api/rules?team=1").json())
    assert manager.put(url, json={"enabled": True}).status_code == 404
    assert manager.get("/api/tickets/2").json()["rule_id"] == rule_id


@pytest.mark.parametrize(
    "category", ["critical", "error", "warning", "info", "task", "change_request"]
)
def test_manager_claims_unmatched_event_once(login, category):
    payload = {"unknown": "manual triage"}
    assert login(6).post("/api/debug/generate", json={"event": payload}).status_code == 200
    event = login(1).get("/api/unmatched").json()[0]
    path = f"/api/unmatched/{event['id']}/claim"
    assert login(2).post(path, json={"team_id": 1, "category": category}).status_code == 403
    assert login(4).post(path, json={"team_id": 1, "category": category}).status_code == 403
    manager = login(1)
    assert manager.post(path, json={"team_id": 1}).status_code == 422
    assert manager.post(path, json={"team_id": 1, "category": "invalid"}).status_code == 422
    response = manager.post(path, json={"team_id": 1, "category": category})
    assert response.status_code == 200
    ticket = response.json()
    assert ticket["team_id"] == 1 and ticket["members"] == [1] and ticket["state"] == "new"
    assert manager.get(f"/api/tickets/{ticket['id']}").json()["first_payload"] == payload
    assert all(e["id"] != event["id"] for e in manager.get("/api/unmatched").json())
    assert manager.post(path, json={"team_id": 2, "category": category}).status_code == 409
    assert ticket["type"] == (category if category in ("task", "change_request") else "incident")
    assert ticket["severity"] == (category if ticket["type"] == "incident" else "info")
    assert ticket["id"] in [
        row["id"] for row in manager.get("/api/board?my_tickets=true").json()["tickets"]
    ]
    assert ticket["id"] in [
        row["id"] for row in login(2).get("/api/board?team=1").json()["tickets"]
    ]
    if category == "change_request":
        assert any(p["ticket_id"] == ticket["id"] for p in login(1).get("/api/proposals").json())
    with Session() as db:
        assert db.query(Ticket).filter_by(event_id=event["id"]).count() == 1
        assert db.get(Event, event["id"]).ticket_id == ticket["id"]


def test_admin_promotes_employee_and_assigns_team_members(login):
    admin = login(5)
    new = admin.post(
        "/api/admin/users", json={"name": "New lead", "email": "lead@example.org"}
    ).json()["id"]
    assert admin.post("/api/teams/3/members", json={"user_id": new}).status_code == 200
    path = "/api/admin/teams/1/manager"
    assignment = {"user_id": new, "previous_leader_id": 1}
    assert login(1).put(path, json=assignment).status_code == 403
    admin = login(5)
    assert admin.put(path, json=assignment).status_code == 200
    assert admin.put(path, json={"user_id": 2, "previous_leader_id": 1}).status_code == 409
    assert (
        admin.post("/api/teams/1/members", json={"action": "remove", "user_id": new}).status_code
        == 409
    )
    lead = login(new)
    assert lead.get("/api/me").json()["role"] == "M"
    assert lead.post("/api/teams/1/members", json={"user_id": 7}).status_code == 200
    assert lead.post("/api/teams/3/members", json={"user_id": 2}).status_code == 403
    assert (
        login(1).post("/api/teams/1/members", json={"action": "remove", "user_id": 7}).status_code
        == 403
    )
