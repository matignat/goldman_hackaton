"""SignalDesk HTTP API. All mutations check capabilities on the server."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from .auth import (
    db_session,
)
from .database import init_db
from .routes import boards, events, notifications, organization, rules, session, tickets


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Initialize the database before serving requests."""
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="SignalDesk API",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
static = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=str(static)), name="assets")


@app.get("/", response_class=HTMLResponse)
def index():
    return (static / "index.html").read_text()


@app.get("/api/health")
def health(db: Session = Depends(db_session)):
    db.execute(text("SELECT 1"))
    return {
        "ok": True,
        "service": "signaldesk",
        "mode": "demo" if os.getenv("DEMO_MODE", "1") == "1" else "production",
    }


app.include_router(session.router)
app.include_router(tickets.router)
app.include_router(boards.router)
app.include_router(rules.router)
app.include_router(notifications.router)
app.include_router(organization.router)
app.include_router(events.router)
