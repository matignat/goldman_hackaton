# SignalDesk architecture and development guide

## Service boundaries

- **HTTP API (`app/api.py`)** exposes `POST /api/ingest/{source}`, scoped board/ticket/rule/runbook/preferences/configuration endpoints, and a team-filtered SSE invalidation stream. A handler checks business authorization; UI hiding is never the security boundary.
- **Raw ingest (`services/ingestion.py`)** stores exactly the received UTF-8 message before matching; each `(source_id, external_id)` has an event idempotency constraint. Malformed JSON survives as a rejected Event with a reason.
- **Normalization (`services/conditions.py`)** flattens arbitrary objects and arrays and derives time, service, schema, severity and technical status from configurable mapping candidates. Rules can address every flattened path; typed comparisons and boolean groups are evaluated without schema-specific code.
- **Routing (`services/routing.py`)** walks the configured source's ordered complete team chain, and each team's ordered rulebook, stopping at the first match. Attempts and rulebook versions are in `route_trace`. The default source chain includes all seeded teams; unmatched events are retained. A hash of the matching team, rule and identity is an indexed unique active dedup key. PostgreSQL advisory transaction locks prevent separate workers from creating two tickets for that key. For a 500-event burst, raw Event and occurrence count grow; incident tickets, first notifications and coalesced Activity records do not.
- **Ticket lifecycle (`models.py`, `api.py`)** keeps Kanban state independent from the technical firing/resolved state. Recovery moves the incident to Done with attention; a relapse reopens it. The team manager can approve any Done incident or task on their team, including their own. A board maintainer can approve work marked Done by someone else. Archived tickets are retained; only the team leader may explicitly purge with a confirmation phrase and tombstone.
- **Runbooks (`services/runbooks.py`)** are converted into ordered canonical steps by a parser registry. Registered action handlers isolate side effects. Risky actions wait for **M** approval before execution. Unknown formats cannot trigger arbitrary code.
- **Delivery (`services/delivery.py`)** keeps per-user, per-channel jobs and stable idempotency keys in SQL. Registered adapters send captured UI, Mailpit SMTP, or simulated phone/SMS. Every user must have a primary and distinct backup. Policies add mandatory channels; schedules, buffer windows, retry intervals and eventual fallback are durable. PostgreSQL uses `SKIP LOCKED` for concurrent workers claiming due jobs. Browser UI is pushed with SSE invalidation; it always reads authorized server state rather than receiving unrestricted raw payloads on the stream.
- **Organization/access (`auth.py`)** scopes privileges by explicit team leader, team membership, ticket membership and guest grants. Four personas: E, M, A, D; BM is a team-scoped grant. Hierarchy and team membership are intentionally different structures.
- **Activity/audit (`services/records.py`)** stores append-only timeline entries and one coalesced team Activity view. The UI shows the most recent 40; this limit does not affect history retention.

## Scaling path

API instances are stateless apart from their PostgreSQL connection pools and per-request auth. Worker instances share DB state; event claims and due delivery claims use `FOR UPDATE SKIP LOCKED`. Dedup creation is protected by a transaction-scoped advisory lock on the dedup key, and active-key uniqueness rejects duplicate live tickets. This is a first step toward horizontal scale, **not** a claim of arbitrarily high throughput. Real implementations should add versioned migrations (Alembic), background outbox publishing, worker partitions keyed by dedup identity, alert-processing capacity limits, dedicated scheduler leadership for digest scans, cached rulebook compilation/invalidation, distributed tracing, read replicas and backup/restore drills.

Known bottlenecks: sequential Python condition evaluation and ORM writes per raw Event; a single RabbitMQ consumer thread per seed queue; database polling and SSE signatures each second; one shared PostgreSQL primary; default SMTP client connects per job; retention without automatic partitioning/archival. The checked-in benchmark exposes the present throughput honestly.

## Authorized runtime config file reload

1. Authenticate as **M** via the demo selector. Create a JSON with the team ID and desired rules. `disable_missing=false` upserts named rules without disabling other manually configured rules; `true` disables other rules (retained for historical references).
2. `POST /api/rules/sign-config` with that JSON. This endpoint validates that the authenticated actor is the current designated team manager, checks all rule conditions and runbook ownership and returns the complete signed manifest.
3. Save the entire response into `config/rules/approved.json`. The host directory is **read-only mounted** into the worker container.
4. The worker scans every two seconds, verifies HMAC and signature age, then validates **every** rule before doing any write. It upserts changed rules and writes a manager-attributed audit entry in one SQL transaction. Invalid files are ignored with a worker warning; the previously published rules stay active.

This guards against an arbitrary file writer bypassing manager privileges. It assumes the manager can save a file to the watched location. A production configuration deployment should use managed secrets, signed CI artifacts, release provenance and signature replay prevention.

## Add modules

- **Webhook/log instance**: `POST /api/admin/sources` with a distinct `slug`, `type`, ordered `routing_chain`, and optional named-group regex + canonical field `mappings`. No Python changes. `X-Event-ID` supports HTTP retry idempotency; use `configuration.webhook_key` for ingestion-authenticated sources. The seeded RabbitMQ source uses the queue `signaldesk.events` and broker publisher in `app/adapters/broker_publish.py`.
- **Condition operator**: extend `compare` and `validate` in `conditions.py` and add tests. Existing nested condition groups do not change.
- **Runbook format**: `formats.register('name', callable)` returning canonical steps; unknown inputs degrade to a safe manual display.
- **Action handler**: `actions.register('name', executor)`; return an auditable result. Ensure manager approval for risky steps, secret isolation, timeouts and parameters validation in a production handler.
- **Channel provider**: `channels.register('name', sender)`; maintain delivery idempotency and retry/error signaling. SMTP uses Mailpit when `SMTP_HOST` set; SMS/phone currently only mark capture jobs sent.
- **UI preset**: modify the CSS rules targeting `[data-preset=...]`, without changing permissions or HTML behavior hooks.

## Operational correctness and limitations

- To recover queued events after a restart, restart worker. Every raw input is present before processing. Failed SMTP attempts are retried on a durable schedule; a distinct backup is queued on exhaustion. Provider acknowledgment is **not** a proof of human receipt.
- The demo accepts only one 1-MiB UTF-8 event per HTTP request; large batch submissions should be broker-published rather than overloading a webhook.
- The app does not implement real external restart, workflow retry, HTTP checks, ticket-creation side effects or signed email response approval. These action handlers simulate execution.
- Advanced personal per-team subscriptions, weekly on-call rotation/holiday schedule exceptions, manager subordinate-board read-only views, administrator explicit archive oversight grants, exhaustive historical archive filters and per-field guest redaction remain future scope.
