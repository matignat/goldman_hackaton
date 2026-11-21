# Implementation coverage against the original brief and merged specification

**Authority:** Hackathon 2026 original brief and Appendix A/B; merged spec v0.3.1 dated 2026-11-21 (original brief takes precedence). This is a candid implementation checklist, not a certification.

| Original requirement / merged ID | Status | Evidence |
|---|---|---|
| ING-01–06 Two source types, persistent raw events, unfamiliar schemas | **Implemented** | HTTP webhook, persistent RabbitMQ consumer, raw Event table, unknown/log extraction tests; real broker deployment not integration-tested here |
| NRM-01–07 Arbitrary nested schema, typed fields and canonical time/severity | **Implemented (subset of mapping nuances)** | `conditions.py`, nested condition regression and original Appendix fixtures |
| MAT-01–10 Ordered complete team chain; first-match-only; durable unmatched | **Implemented** | `routing.py`, 4-team seed with fallthrough; unmatched tests |
| RUL-01–14 Nested Boolean rule conditions, runtime UI/API/config editing | **Implemented** | Manager-only CRUD/test and signed atomic file reload; template placeholder engine; no advanced design-preview diff |
| FLD-01–08 Dedup 500 alerts and grouping/throttling | **Implemented** | 500-event integration test and instrument/5-minute log grouping; exact-duplicate notifications deduplicated |
| FLD-12–15 Recovery and relapse | **Implemented** | explicit recovered event → Done/attention; relapse before approval → In progress |
| BRD-01–12 Kanban, approvals, archive, coalesced recent activity | **Implemented** | 4 lanes, SSE, M/BM approve, team-manager self-approval for tasks and incidents, manager critical archive/error toggle, audit and 40-item team activity; manager purge/tombstone |
| ORG-01–18 E/M/A/D and BM separation, manager and guest scopes | **Implemented core; partial extended visibility** | distinct server authorization; manager-only mutations; guest read-only; one-hop org tab; subordinate-board views and A oversight archive grants not complete |
| TKT-01–29 Incident data, manual tasks, change requests, members, audit, history | **Implemented core** | ticket drawer, original/normalized event, step state, comments, archived handled-ticket direct access, distinct tasks/proposals, audit |
| PRP-01–04 Manager proposal workflow and revision | **Implemented** | duplicate open-request block, manager rejection, revised resubmission, CHANGE ticket |
| RBK-01–18 Runbook adapters, safe/risky steps, mock action handlers | **Implemented core; action conditions partial** | YAML/JSON/Markdown/checklist/link/text; placeholders; manager-authorized risky steps; simulated external side effects; complex automated conditional checks not implemented |
| DEL-01–25 Preferences, mandatory channels, schedules, buffer, retry/fallback | **Implemented core; extended schedules partial** | primary ≠ backup, default Email unless primary Email, personal schedule window, manager policies, digest jobs, exponential retry, distinct fallback, capture outbox; weekly/holiday rota, full per-team subscriptions not complete |
| UI-01–29 Live web UI, permissions, presets, semantic markup | **Implemented core** | navy-white-gold styled semantic HTML, vanilla JS, SSE invalidation, detail drawer, role-specific views; three presets; not a formally audited WCAG certification |
| MOD-01–04 Modular provider/source/format/action registries | **Implemented with caveat** | registries for runbooks/actions/channels, config-defined webhooks, broker adapter; arbitrary new broker queue source instances still require subscriber generalization |
| NFR-01–03 Throughput & latency measured | **Partially demonstrated** | actual in-process SQLite and captured-channel p50/p99 results in `benchmark-results.json`; separate real HTTP load generator; not a fully instrumented Docker/Postgres/RabbitMQ-to-browser measured run |
| NFR-10–12 Durability and replay safety | **Implemented core** | SQL raw queue, SKIP LOCKED, advisory dedup locks, durable jobs, signed atomic rule reload; delivery at-least-once, not exactly once |
| NFR-20–23 Security/privacy | **Demo-level** | capability enforcement and mock sessions; no SSO, comprehensive field redaction, KMS or secret vault |
| NFR-40–43 One command, seeded events, disclosure, demo video | **One-command app and documentation implemented; submission video not created** | `./start.sh`, fixtures, README, benchmark; user/team must record and upload ≤5 min video |

**Unimplemented or partial extensions:** personal notifications per-team/schedule/weekly on-call, admin oversight archive grants, advanced event tagging/redaction, comprehensive archive search fields, independent read-only boards via manager-reporting tree, real automatic action condition execution and external integrations, auto-heal quiet-period rule, horizontal API/browser load measurement, signed user action replies and optional LLM transformation. Do not demo or claim these as complete.

**Measured latest local smoke benchmark** (SQLite in-process on this container, 600 generated events: 500 repeated payments + 80 settlement variations + 20 unmatched): measured rates and latencies are stored in `benchmark-results.json`; the result is reproducible with `python scripts/benchmark_local.py`. This is **not a production-scale benchmark**. `pytest -q`: **19 passed**; browser rendering checked using Chromium with deterministic real API fixture payloads, not production browser networking.
