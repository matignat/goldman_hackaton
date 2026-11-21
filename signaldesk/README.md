# SignalDesk — Flexible Alerting & Notification Service

A runnable, modular hackathon application implementing the core of **Hackathon 2026 — Flexible Alerting & Notification Service** and its **2026-11-21 merged spec v0.3.1**. It turns heterogeneous events into one accountable incident ticket, lets teams resolve tickets through runbooks and protected approvals, and routes notifications over durable, configurable channels.

> **Status:** a substantive, tested demo—not production software and **not** a certification of complete coverage of every requirement in the long merged spec. The original brief and appendices are included under `docs/`; see `docs/COMPLIANCE.md` for tested features and disclosed gaps.

## Run: one command

Prerequisites: Docker Compose v2, a running Docker daemon (Docker Desktop or Colima), and free ports **8000**, **8025**, **15672**. From this extracted directory:

```bash
./start.sh
```

This generates a local `.env` with a random signing secret when needed, builds the API and worker, and waits for PostgreSQL/RabbitMQ health checks. Demo fixtures are seeded automatically only on an empty database.

| Endpoint | Purpose |
|---|---|
| **http://localhost:8000** | Web application with a persona chooser |
| **http://localhost:8000/api/docs** | OpenAPI interactive API reference |
| **http://localhost:8025** | Mailpit email capture inbox |
| **http://localhost:15672** | RabbitMQ management (guest / guest, local demo only) |

All role capabilities are enforced by the API even though authentication is a mock *act as employee* selector. For demo access, select **Morgan Blake** (M, leads Payments and Settlements), **Avery Chen** (E, works tickets), **Nora Shah** (E + delegated Board Maintainer), **Olivia Reed** (M, Rates and Pricing), **Adrian Cole** (A, platform administrator), **Dana Brooks** (D, Demo/Debug), or **Sofia Ruiz** (E, non-engineer view).

To stop, use Ctrl+C; to remove containers while retaining data, run `docker compose down`. To remove database and broker volumes **permanently**, run `docker compose down -v`.

If you have Docker CLI but get `failed to connect to Docker API`: **start your engine**. On macOS with Colima, `colima start`, then `./start.sh`. `docker compose up --build` is correct; `docker compose --build` is not.

## Five-minute demonstration path

1. Open the board as Morgan. Notice the seeded payment incident, an operations task, the workflow columns and per-ticket details. The ticket detail includes original event, normalized data, occurrence samples, actionable steps and an append-only timeline.
2. Switch to **Dana** (D) → Demo / Debug. Generate **500** identical payments events, then return to Morgan. There is still **one** payment incident with a counter, not 500 cards. Generate an unknown schema and inspect the durable **Unmatched** view.
3. Send the Appendix A workflow via the **actual RabbitMQ adapter**:

   ```bash
   cat fixtures/workflow.json | docker compose exec -T worker python -m app.adapters.broker_publish
   ```

   Or send the other sample over the **HTTP webhook**:

   ```bash
   curl -H 'Content-Type: application/json' -X POST \
     --data-binary @fixtures/payments.json http://localhost:8000/api/ingest/webhook
   ```

4. As Avery, work a settlement ticket and run a safe step. A risky escalation requests **M** authorization. Nora (BM) may approve a completed ticket **but cannot** authorize the risky action or publish a rule. Morgan can authorize the action, but cannot approve their **own** Done transition.
5. As a manager, open Rules, test a nested JSON condition against a sample event, and publish a new rule without restart. The routing chain attempts all eligible team rulebooks in priority order and stops at the **first match**.
6. As Avery, request a rule/runbook change, then review/reject it as Morgan; a revised request can be submitted. As Dana, simulate an Email outage and observe durable retries and eventual distinct fallback. As Adrian (A), inspect platform metrics; the D tools are not accessible to A.
7. As Morgan, open **Major Incident Archive**. It defaults to **critical** approved incidents, with an optional **Include errors** filter. A ticket's archive visibility does not govern durable retention; an employee may directly open a ticket they personally handled.

## Architecture

```text
RabbitMQ adapter ──┐
HTTP / log webhook ├─>  RAW EVENTS (PostgreSQL, durable)  ─>  worker/normalizer
D-only test source ┘                                         │
                        ┌───────────────────────────────────┘
                        v
                ordered team routing + nested condition engine
                        │        └─> persisted unmatched + trace
                        v
             keyed dedup / grouping / ticket lifecycle
                        ├─> Kanban, comments, audited approvals
                        ├─> runbook parser + plugin action registry
                        ├─> notification policies, digest, retry, fallback
                        └─> SSE live invalidation -> semantic web UI
```

The **API** handles input, scopes and UI; the **worker** receives broker events, processes durable queued events, sends queued deliveries, and reloads manager-signed rule files. The two processes share a database but **not process-local incident state**. PostgreSQL advisory transaction locks serialize a dedup identity; `SKIP LOCKED` partitions event and delivery jobs across workers. Source/channel/runbook/action implementations are isolated behind registries/adapters. SQLAlchemy is used for both PostgreSQL and the lightweight SQLite local test mode.

See `docs/ARCHITECTURE.md` for boundaries, deployment topology and the horizontal-scaling path.

### Project structure

```text
app/
  api.py                protected REST + SSE
  auth.py               role/team/ticket capability checks
  models.py             durable entity model and indexes
  database.py           transactional SQLAlchemy engine
  seed.py               Appendix A/B cases and personas
  worker.py             queue processor and RabbitMQ subscriber
  adapters/             broker publisher/receiver boundary
  services/
    ingestion.py        exactly-preserved raw event ingestion, idempotency
    conditions.py       arbitrary fields, nested conditions, canonicalization
    routing.py          first-match team routing, dedup, recovery/relapse
    delivery.py         registered channel adapters, buffers, retries
    runbooks.py         pluggable formats and action handlers
    config_reload.py    manager-signed atomic rule-file reload
    records.py          persistent audit and coalesced recent activity
  static/               semantic HTML + vanilla JS + Goldman-inspired CSS
config/rules/           approved live-reload manifests (mounted in worker)
fixtures/               original-schema workflow, metrics and business events
scripts/                performance and browser verification scripts
tests/                  API, permission and lifecycle integration tests
```

## Extend at runtime

**Add a source:** Sign in as A, add it using the Admin API (`POST /api/admin/sources`) or configuration. An HTTP/log source receives events at `/api/ingest/{slug}`; its `configuration` can specify named-group extraction regex, field mappings, a webhook key and ordered `routing_chain`. A newly seen JSON schema is immediately usable in `field` paths without registration. The seeded broker instance subscribes to durable `signaldesk.events` in RabbitMQ; additional broker queues currently need their own adapter registration (see gaps below).

**Add a rule:** Sign in as the designated team M, then use the Rules editor or `POST /api/rules`. The JSON supports `all`/`any`/`not` conditions; operators `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `starts_with`, `wildcard`, `regex`, `exists`, `not_exists`; nested `field` paths; `presentation.title` and `.body` with `{{field.path}}`; recipient routing, mandatory channels, dedup template, grouping template, grouping window and runbook ID. `POST /api/rules/test` previews title, body and whether a sample event matches.

**Live reload through a file:** A manager may authorize a file **without giving the file watcher unrestricted write privileges**. Log in as M, call `POST /api/rules/sign-config` with `{ "team_id": 1, "rules": [ {"name":"…", "condition": {…}} ], "disable_missing":false }`. Save the complete signed JSON response to `config/rules/signed.json`. The worker validates the HMAC, team manager identity, age (24 hours), conditions and referenced runbooks, then **atomically** upserts the rules within ~2 seconds. Forged/invalid manifests are rejected without altering live rules. The file's signature covers every rule and the `disable_missing` choice. A or D cannot authorize rule reloads. Example in `docs/ARCHITECTURE.md`.

**Add a runbook:** M can paste structured YAML/JSON, Markdown, checklist, link or plain text into the Runbooks editor, then associate the saved ID with a rule. Unknown formats degrade to manual instructions. Add new format adapters in `services/runbooks.py` via `formats.register(...)` and new executable action handlers via `actions.register(...)`. Risky step execution still requires **M approval** at the API. External actions deliberately simulate side effects in the demo.

**Add a channel/provider:** Email, SMS, phone and UI are the four supported demo channel types. Provider implementations belong in `services/delivery.py`'s `ChannelRegistry`, not ticket logic. Mailpit handles SMTP if `SMTP_HOST` is configured. SMS/phone are **outbox-only simulations**. New channel instances/providers can be registered without changing matching/board code; a genuinely new type needs a new adapter implementation and configuration. Users must have distinct primary/backup channels; fallback occurs only after primary retries exhaust. Manager-forced channels are independent.

## Test and measure

Outside Docker, with Python 3.12+, make a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
python scripts/benchmark_local.py --burst 500 --mixed 80 --unmatched 20
python scripts/load_test_http.py --base http://localhost:8000 --count 500 --workers 16 --mode burst
```

The local benchmark runs the **real durable DB processing path** on disposable SQLite, not invented numbers. The checked-in `benchmark-results.json` is a real run from this container. **These figures are not proof of RabbitMQ/PostgreSQL production capacity**. Test a real deployment with `load_test_http.py`, capture browser SSE receipt times for true UI end-to-end latency, and report machine and environment in the hackathon video. The HTTP tester measures persistence acknowledgements and whether the queue drained, **not** true UI latency.

`tests/test_end_to_end.py` checks unknown nested schemas, all-team routing traces, 500-alert flood, log grouping, self-healing and relapse, D/A separation, BM/M and self-approval boundaries, manager-signed config, archive/purge, guest grants, rule editing, digest/forced schedule and fallback retry. **19 passed** in the captured run; browser UI rendered with Chromium via local fixture stubs. Docker and the real RabbitMQ connection were **not** available to be integration-tested in this build environment.

## Security and honest scope

- Mock sign-in is **demo-only**; never place this instance on the open internet or treat signed demo cookies as real authentication. Set `DEMO_MODE=0` and replace the auth integration before any production deployment.
- All business mutations check server-side team/ticket grants. A is not automatically D or M, D is not Director, BM can finalize tickets but cannot publish/approve config or risky actions, and nobody can approve their own Done transition.
- Original input payloads are kept in a database, UI details are permission-checked, and explicitly purged tickets leave an audit tombstone. Real retention, encryption, field redaction, immutable tamper-evident audit and SSO need additional engineering.
- External action handlers and SMS/phone are mocks; email is live SMTP only when configured. No credentials are sent to any AI model; there is **no AI integration** in this deliverable. The **code, documentation and test suite were created with AI assistance**; prompt context contained the project spec and example synthetic events, not real production credentials or customer events.
- A one-command demo is provided. **A 5-minute demo video and upload to Lockbox are submission tasks for the team**, not something this repository performs.

If something fails, inspect `docker compose logs -f api worker`. To isolate the worker or test disaster recovery, stop/restart the `worker` service; ingested events and pending notification jobs remain in PostgreSQL.
