"""Persistent raw ingestion; adapters are responsible only for receiving bytes."""
import json
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from ..models import Event,Source

def ingest(db:Session,source:Source,raw:str,external_id:str|None=None)->Event:
    if not source or not source.enabled:raise ValueError('Unknown or disabled source')
    if len(raw.encode('utf-8'))>1024*1024:raise ValueError('Event exceeds 1 MiB limit')
    if external_id:
        existing=db.query(Event).filter_by(source_id=source.id,external_id=external_id).first()
        if existing:return existing
    ev=Event(source_id=source.id,external_id=external_id,raw=raw,state='pending')
    # malformed input is still durable and will be moved to rejected by worker
    db.add(ev);db.flush();return ev
