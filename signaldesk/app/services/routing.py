"""Deterministic sequential routing and idempotent incident deduplication."""

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import Event, Rule, Runbook, Source, Team, Ticket, now
from .conditions import canonical, matches, render
from .delivery import queue_ticket
from .records import activity, audit
from .runbooks import ticket_steps

ORDER = ["info", "warning", "error", "critical"]


def _key(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()[:48]


def _match_rule(
    db: Session,
    source: Source,
    chain: list[int],
    fields: dict,
    recovery: bool,
    rulebooks: dict[int, list[Rule]] | None,
) -> tuple[Rule | None, list[dict]]:
    """Correlate a recovery or select the first matching rule in team order."""
    trace: list[dict] = []
    # Keep recovery bound to the original incident even after live rule edits.
    if recovery:
        for previous in (
            db.query(Ticket)
            .filter_by(source_id=source.id, type="incident")
            .filter(Ticket.state != "archived")
            .all()
        ):
            old_rule = db.get(Rule, previous.rule_id)
            if not old_rule:
                continue
            old_identity = render(
                old_rule.dedup_template or "{{canonical.service}}:{{canonical.schema}}", fields
            )
            if previous.fingerprint == old_identity:
                trace.append(
                    {
                        "team_id": previous.team_id,
                        "reason": "recovery_correlated_to_original_ticket",
                        "ticket_id": previous.id,
                    }
                )
                return old_rule, trace
    for team_id in chain:
        rules = (
            rulebooks.get(team_id, [])
            if rulebooks is not None
            else db.query(Rule)
            .filter_by(team_id=team_id, enabled=True)
            .order_by(Rule.priority, Rule.id)
            .all()
        )
        tested = []
        for rule in rules:
            passed = matches(rule.condition or {}, fields)
            tested.append({"rule_id": rule.id, "match": passed, "version": rule.version})
            if passed:
                trace.append({"team_id": team_id, "rules": tested})
                return rule, trace
        trace.append({"team_id": team_id, "rules": tested})
    return None, trace


def process_event(db: Session, ev: Event, rulebooks: dict[int, list[Rule]] | None = None) -> Event:
    """Route one pending event and update its incident within the caller’s transaction."""
    if ev.state != "pending":
        return ev
    source = db.get(Source, ev.source_id)
    if source is None:
        raise ValueError(f"Missing source {ev.source_id} for event {ev.id}")
    try:
        payload = json.loads(ev.raw)
    except json.JSONDecodeError:
        if (source.configuration or {}).get("pattern"):
            payload = ev.raw
        else:
            ev.state = "rejected"
            ev.error = "Invalid JSON and no log extraction configured"
            ev.processed_at = now()
            return ev
    try:
        normalized = canonical(payload, source.configuration or {}, ev.received_at)
    except Exception as e:
        ev.state = "rejected"
        ev.error = "Normalization failed: " + str(e)[:160]
        ev.processed_at = now()
        return ev
    fields = normalized["flat"]
    ev.normalized = {
        "flat": fields,
        "severity": normalized["severity"],
        "schema": normalized["schema"],
    }
    chain = source.routing_chain or [t.id for t in db.query(Team).order_by(Team.position, Team.id)]
    recovery = normalized["status"] in ("resolved", "ok", "recovered", "cleared", "healthy")
    winner, trace = _match_rule(db, source, chain, fields, recovery, rulebooks)
    ev.route_trace = trace
    ev.processed_at = now()
    if winner is None:
        ev.state = "unmatched"
        audit(db, "event_unmatched", detail={"event_id": ev.id, "attempted_teams": chain})
        return ev
    rule = winner
    team_id = rule.team_id
    fingerprint_template = rule.dedup_template or "{{canonical.service}}:{{canonical.schema}}"
    identity = render(fingerprint_template, fields)
    # Explicit grouping policy: different matching error messages from the same
    # instrument coalesce into one ticket for this configured time bucket.
    if rule.group_template and not recovery:
        window = max(1, rule.throttle_seconds or 300)
        bucket = int(ev.received_at.timestamp() // window)
        identity = f"GROUP:{render(rule.group_template, fields)}:{bucket}"
    active_key = _key(f"{team_id}:{rule.id}:{identity}")
    # Lock the incident identity before find/create on PostgreSQL. Concurrent
    # horizontally scaled workers cannot race to create duplicate active tickets.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": active_key})
    ticket = db.query(Ticket).filter_by(active_key=active_key).first()
    # Recovery is correlated to still-open incident identity, including if rule changed.
    if recovery and not ticket:
        ticket = (
            db.query(Ticket)
            .filter_by(source_id=source.id, fingerprint=identity, type="incident")
            .filter(Ticket.state != "archived")
            .order_by(Ticket.id.desc())
            .first()
        )
    if ticket is None and recovery:
        ev.state = "unmatched"
        ev.error = "Recovery with no active correlated incident"
        return ev
    if ticket is None:
        prior = (
            db.query(Ticket)
            .filter_by(fingerprint=identity, team_id=team_id, type="incident", state="archived")
            .order_by(Ticket.id.desc())
            .first()
        )
        title = render(
            (rule.presentation or {}).get("title")
            or "{{canonical.schema}} — {{canonical.service}}",
            fields,
        )
        body = render(
            (rule.presentation or {}).get("body") or "{{error.message}}{{annotations.summary}}",
            fields,
        )
        rb = db.get(Runbook, rule.runbook_id) if rule.runbook_id else None
        ticket = Ticket(
            team_id=team_id,
            rule_id=rule.id,
            source_id=source.id,
            event_id=ev.id,
            type="incident",
            active_key=active_key,
            fingerprint=identity,
            title=title,
            body=body,
            severity=normalized["severity"],
            environment=normalized["environment"],
            schema=normalized["schema"],
            occurred_at=normalized["occurred_at"],
            last_occurrence_at=normalized["occurred_at"],
            created_at=now(),
            updated_at=now(),
            occurrences=1,
            first_payload=payload if isinstance(payload, dict) else {"line": payload},
            latest_payload=payload if isinstance(payload, dict) else {"line": payload},
            samples=[{"event_id": ev.id, "at": normalized["occurred_at"].isoformat()}],
            runbook_steps=ticket_steps(rb, fields),
            flags=["recurrence"] if prior else [],
            recurrence_of=prior.id if prior else None,
        )
        db.add(ticket)
        db.flush()
        activity(db, team_id, ticket.id, "incident", "New incident: " + title)
        audit(
            db,
            "ticket_created",
            team_id,
            ticket_id=ticket.id,
            detail={"event_id": ev.id, "route": trace},
        )
        queue_ticket(db, ticket)
    else:
        if not recovery:
            ticket.occurrences += 1
            ticket.last_occurrence_at = normalized["occurred_at"]
            ticket.latest_payload = payload if isinstance(payload, dict) else {"line": payload}
            ticket.samples = (
                [
                    *(ticket.samples or []),
                    {"event_id": ev.id, "at": normalized["occurred_at"].isoformat()},
                ]
            )[-20:]
            if ORDER.index(normalized["severity"]) > ORDER.index(ticket.severity):
                ticket.severity = normalized["severity"]
            if ticket.state == "done":
                ticket.state = "in_progress"
                ticket.done_by = None
                ticket.flags = list(set((ticket.flags or []) + ["relapse"]))
                ticket.incident_status = "firing"
                audit(db, "ticket_relapse", team_id, ticket_id=ticket.id)
            ticket.incident_status = "firing"
        ticket.updated_at = now()
        activity(
            db,
            ticket.team_id,
            ticket.id,
            "incident",
            "Incident updated: " + ticket.title,
            coalesce=True,
            count=ticket.occurrences,
        )
    if recovery:
        ticket.incident_status = "resolved"
        ticket.resolved_at = now()
        ticket.state = "done"
        ticket.done_by = None
        ticket.flags = list(set((ticket.flags or []) + ["attention"]))
        audit(
            db,
            "incident_self_healed",
            ticket.team_id,
            ticket.id,
            detail={"event_id": ev.id, "reason": "recovery_event"},
        )
        activity(
            db,
            ticket.team_id,
            ticket.id,
            "recovery",
            "Self-healed: attention needed — " + ticket.title,
        )
        queue_ticket(db, ticket, "approval_needed")
    ev.state = "matched"
    ev.ticket_id = ticket.id
    return ev


def pump_events(db: Session, limit: int = 500) -> int:
    # A single worker is the demo default; PostgreSQL SKIP LOCKED supports worker replication.
    q = db.query(Event).filter_by(state="pending").order_by(Event.id)
    if db.get_bind().dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    pending = q.limit(limit).all()
    # One rulebook snapshot per processing batch; next batch reloads it. This avoids
    # an N×teams database-query storm during a burst without stale caches.
    if pending:
        from collections import defaultdict

        rulebooks = defaultdict(list)
        for rule in (
            db.query(Rule)
            .filter_by(enabled=True)
            .order_by(Rule.team_id, Rule.priority, Rule.id)
            .all()
        ):
            rulebooks[rule.team_id].append(rule)
        for ev in pending:
            process_event(db, ev, rulebooks)
    return len(pending)
