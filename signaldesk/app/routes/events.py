"""SignalDesk events."""

import json
import os

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Request,
)
from sqlalchemy.orm import Session

from ..auth import current_user, db_session, require_debug
from ..models import ChannelState, Event, Source, User
from ..services.delivery import process_deliveries
from ..services.ingestion import ingest
from ..services.records import audit
from ..services.routing import process_event, pump_events
from .presentation import row

router = APIRouter()


@router.post("/api/ingest/{slug}")
async def ingest_webhook(
    slug: str,
    request: Request,
    x_event_id: str | None = Header(default=None),
    x_webhook_key: str | None = Header(default=None),
    db: Session = Depends(db_session),
):
    source = db.query(Source).filter_by(slug=slug).first()
    if not source or source.type not in ("webhook", "log") or not source.enabled:
        raise HTTPException(404, "Unknown webhook")
    secret = (source.configuration or {}).get("webhook_key")
    if secret and secret != x_webhook_key:
        raise HTTPException(401, "Invalid webhook key")
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        ev = ingest(db, source, raw, x_event_id)
        db.commit()
    except ValueError as e:
        raise HTTPException(413, str(e))
    if os.getenv("DEMO_INLINE_WORKER", "0") == "1":
        process_event(db, ev)
        process_deliveries(db)
        db.commit()
    return {"event_id": ev.id, "status": "accepted"}


@router.get("/api/debug")
def debug_info(u: User = Depends(current_user), db: Session = Depends(db_session)):
    require_debug(u)
    return {
        "channels": [row(c, ["name", "failing"]) for c in db.query(ChannelState)],
        "events": db.query(Event).count(),
        "pending": db.query(Event).filter_by(state="pending").count(),
        "unmatched": db.query(Event).filter_by(state="unmatched").count(),
    }


@router.post("/api/debug/channel")
def debug_channel(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    require_debug(u)
    channel = db.get(ChannelState, str(data.get("name", "")))
    if not channel:
        raise HTTPException(404)
    channel.failing = bool(data.get("failing", False))
    audit(
        db,
        "debug_channel_failure_toggle",
        actor=u,
        detail={"channel": channel.name, "failing": channel.failing},
    )
    db.commit()
    return {"ok": True, "failing": channel.failing}


@router.post("/api/debug/generate")
def debug_generate(
    data: dict = Body(...), u: User = Depends(current_user), db: Session = Depends(db_session)
):
    require_debug(u)
    s = db.query(Source).filter_by(slug="demo").first()
    if not s:
        raise HTTPException(503, "Demo source not configured")
    payload = data.get(
        "event",
        {
            "alertname": "HighErrorRate",
            "labels": {"service": "payments-api", "severity": "critical"},
            "status": "firing",
            "annotations": {"summary": "Demo error rate high"},
        },
    )
    count = min(1000, max(1, int(data.get("count", 1))))
    raw = json.dumps(payload)
    for i in range(count):
        ingest(db, s, raw)
    db.commit()
    if os.getenv("DEMO_INLINE_WORKER", "0") == "1":
        pump_events(db, limit=count)
        process_deliveries(db)
        db.commit()
    return {
        "accepted": count,
        "source_id": s.id,
        "note": "Events queued in durable ingestion table",
    }


@router.get("/api/debug/events")
def debug_events(u: User = Depends(current_user), db: Session = Depends(db_session)):
    require_debug(u)
    return [
        row(
            e,
            [
                "id",
                "source_id",
                "received_at",
                "processed_at",
                "state",
                "ticket_id",
                "route_trace",
                "error",
            ],
        )
        for e in db.query(Event).order_by(Event.id.desc()).limit(70)
    ]
