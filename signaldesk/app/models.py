from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now():
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    role: Mapped[str] = mapped_column(String(10), default="E")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    primary_channel: Mapped[str] = mapped_column(String(20), default="ui")
    backup_channel: Mapped[str] = mapped_column(String(20), default="email")
    extra_channels: Mapped[list["PreferredChannel"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="PreferredChannel.channel"
    )

    delivery_preferences: Mapped["TicketDeliveryPreferences | None"] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    severity_preferences: Mapped["SeverityPreferences | None"] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def severity_channels(self):
        return self.severity_preferences.channels if self.severity_preferences else {}

    @property
    def preferred_channels(self):
        return [item.channel for item in self.extra_channels]

    subscriptions: Mapped[dict] = mapped_column(JSON, default=dict)
    schedule: Mapped[dict] = mapped_column(
        JSON, default=lambda: {"timezone": "UTC", "start": "00:00", "end": "23:59"}
    )
    buffer_seconds: Mapped[int] = mapped_column(Integer, default=0)
    preset: Mapped[str] = mapped_column(String(24), default="engineer")
    timezone: Mapped[str] = mapped_column(String(48), default="Europe/Warsaw")
    debug_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class ExplicitManagerAssignment(Base):
    """Staff whose reporting line is set by an administrator instead of team membership."""

    __tablename__ = "explicit_manager_assignments"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )


class SeverityPreferences(Base):
    __tablename__ = "user_severity_preferences"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    channels: Mapped[dict] = mapped_column(JSON, default=dict)


class TicketDeliveryPreferences(Base):
    __tablename__ = "ticket_delivery_preferences"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    channels: Mapped[dict] = mapped_column(JSON)


class PreferredChannel(Base):
    __tablename__ = "user_preferred_channels"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), primary_key=True)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    leader_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    position: Mapped[int] = mapped_column(Integer, default=100)


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    maintainer: Mapped[bool] = mapped_column(Boolean, default=False)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    type: Mapped[str] = mapped_column(String(32), default="webhook")
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    routing_chain: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    presentation: Mapped[dict] = mapped_column(JSON, default=dict)
    routing: Mapped[dict] = mapped_column(JSON, default=dict)
    dedup_template: Mapped[str] = mapped_column(String(450), default="")
    group_template: Mapped[str] = mapped_column(String(450), default="")
    throttle_seconds: Mapped[int] = mapped_column(Integer, default=0)
    runbook_id: Mapped[int | None] = mapped_column(ForeignKey("runbooks.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class DeletedRule(Base):
    __tablename__ = "deleted_rules"
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id"), primary_key=True)


def current_rules(db):
    return db.query(Rule).filter(~Rule.id.in_(db.query(DeletedRule.rule_id)))


class Runbook(Base):
    __tablename__ = "runbooks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    source_format: Mapped[str] = mapped_column(String(25), default="markdown")
    source_text: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    raw: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    route_trace: Mapped[list] = mapped_column(JSON, default=list)
    normalized: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="unique_source_event"),)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id"), nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", use_alter=True), nullable=True
    )
    type: Mapped[str] = mapped_column(String(30), default="incident")
    state: Mapped[str] = mapped_column(String(30), default="new", index=True)
    incident_status: Mapped[str] = mapped_column(String(20), default="firing")
    active_key: Mapped[str | None] = mapped_column(String(400), unique=True, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(350), default="", index=True)
    title: Mapped[str] = mapped_column(String(250))
    body: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(20), default="info")
    environment: Mapped[str] = mapped_column(String(60), default="")
    schema: Mapped[str] = mapped_column(String(120), default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_occurrence_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    first_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    latest_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    samples: Mapped[list] = mapped_column(JSON, default=list)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    members: Mapped[list] = mapped_column(JSON, default=list)
    runbook_steps: Mapped[list] = mapped_column(JSON, default=list)
    done_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    recurrence_of: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", use_alter=True), nullable=True
    )
    __table_args__ = (Index("ix_ticket_team_state_updated", "team_id", "state", "updated_at"),)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(100), default="system")
    action: Mapped[str] = mapped_column(String(100))
    team_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Activity(Base):
    __tablename__ = "activity"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    team_id: Mapped[int] = mapped_column(Integer, index=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(300))
    count: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_for: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ChannelState(Base):
    __tablename__ = "channel_states"
    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    failing: Mapped[bool] = mapped_column(Boolean, default=False)


class Policy(Base):
    __tablename__ = "policies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    severity: Mapped[str] = mapped_column(String(20), default="critical")
    channel: Mapped[str] = mapped_column(String(20), default="ui")
    kind: Mapped[str] = mapped_column(String(40), default="new_ticket")
    target: Mapped[str] = mapped_column(String(30), default="all")
    ignore_schedule: Mapped[bool] = mapped_column(Boolean, default=False)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    target_type: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text)
    patch: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")
    comment: Mapped[str] = mapped_column(Text, default="")
    revised_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BoardAccess(Base):
    __tablename__ = "board_access"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uniq_guest_request"),)
