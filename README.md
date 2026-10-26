# RCA Copilot

A source-agnostic root cause analysis accelerator, built on top of the NL-to-Data
Insight platform (its FastAPI backend, local Ollama LLM, Mongo connector and React
shell are reused). Give it an incident (service, time window, symptom) and it pulls
evidence from every enabled source in parallel, correlates it deterministically,
and asks a local LLM to rank evidence-backed root causes.

The original natural-language-to-chart product is documented further down and still works.

## How it works

```
 Incident (service, start, end, symptom)
        │
        ▼
 POST /rca/analyze ──► RCAEngine
        │
        │  1. COLLECT  (asyncio.gather, one task per connector, own timeout each)
        │     ┌────────────────────────────────────────────────────────────┐
        │     │ QueryPlanner: LLM writes ONE bounded query fragment per    │
        │     │ source from its schema (falls back to a safe default)      │
        │     └───────┬──────────────────┬──────────────────┬──────────────┘
        │             ▼                  ▼                  ▼
        │      Elasticsearch          MongoDB             Hive      ... any class decorated
        │      (app logs)         (deploys/changes)    (metrics)        with @register_connector
        │             └──────────────────┼──────────────────┘
        │                                ▼
        │                 Evidence[] (normalised EvidenceRecord rows,
        │                 failed/slow sources reported, not fatal)
        │
        │  2. CORRELATE  (deterministic, no LLM)
        │       • per-source error-rate spikes: z-score vs zero-filled baseline window
        │       • recurring error signatures (numbers/ids masked), novel vs baseline
        │       • numeric metric anomalies (z-score) and change events (deploys...)
        │       • one cross-source timeline, offsets from the first anomaly
        │
        │  3. RANK  (LLM, strict JSON)
        │       • sees only the correlator report + citable evidence ids
        │       • response schema-validated; hypotheses citing no real evidence are rejected
        │       • one retry with the validation error fed back
        ▼
 RCAResult: ranked hypotheses + evidence ids, timeline, per-source query/latency,
            warnings, source failures, timings
```

Backend code: `backend/app/connectors/` (source layer), `backend/app/rca/` (engine),
`backend/app/api/routes/rca.py` (API). Frontend: `frontend/src/pages/Rca.tsx` (route `/rca`).

### API

| Method | Path | Purpose |
|---|---|---|
| POST | `/rca/analyze` | Body: `{service, start, end, symptom, connectors?}` (ISO 8601). Returns `RCAResult`. |
| GET | `/rca/connectors/health` | Health and latency of every enabled evidence source. |

Both require the same Bearer JWT as the rest of the app (`/auth/register`, `/auth/token`).

### Adding a data source

1. Create `backend/app/connectors/<name>_connector.py`, subclass `MCPConnectorBase`
   (`connectors/base.py`) and decorate it with `@register_connector("<type>")`.
2. Add `{"name": "...", "type": "<type>", "options": {...}}` to `RCA_CONNECTORS`.

No core code changes: modules in the package are auto-discovered. Connectors never expose
free-form queries to the LLM; each validates a small fragment and adds the time range,
ordering and row limit itself.

## Setup

```bash
cp .env.example .env     # set SECRET_KEY and POSTGRES_PASSWORD; RCA_* and ES_* are pre-filled
docker compose up --build
docker exec nl_insight_ollama ollama pull qwen2.5:7b     # one-time, ~4.7 GB
```

Frontend: http://localhost (sign in, then click **RCA Copilot** in the top bar).
The compose stack adds a single-node Elasticsearch 8.13.2 with security **disabled**
(local demo only).

Without Docker: `pip install -r backend/requirements.txt` (plus `pyhive`, `thrift`, `sasl`,
`thrift-sasl` if you use Hive), then `uvicorn app.main:app --reload` from `backend/`.

### Configuration (names only; see `.env.example`)

`RCA_CONNECTORS`, `RCA_CONNECTOR_TIMEOUT_SECONDS`, `RCA_BASELINE_MINUTES`,
`RCA_ZSCORE_THRESHOLD`, `RCA_MAX_RECORDS_PER_SOURCE`, `ES_URL`, `ES_INDEX`, `ES_USERNAME`,
`ES_PASSWORD`, `ES_API_KEY`, `ES_VERIFY_CERTS`, plus the existing `MONGODB_*`, `HIVE_*`
and `OLLAMA_*` variables.

## Run the demo

The seed script writes one incident across three stores: a deploy that shrinks the DB
connection pool (MongoDB change event), pool wait and p99 latency blow-up (Hive metrics),
Hikari timeout errors and then HTTP 500/503s (Elasticsearch logs).

```bash
pip install "elasticsearch==8.13.2" pymongo          # + pyhive to seed Hive
python scripts/seed_incident.py                       # prints the incident JSON
```

`seed_incident.py` reads the same env vars as the backend (`ES_URL`, `MONGODB_URI`,
`HIVE_*`) with localhost defaults; use `--skip-hive` if you have no Hive cluster. Then open
the RCA page, paste the printed JSON into the "Load" box, and click **Analyse incident**.
The expected top cause is the deploy that reduced the pool size. To also use the Hive
metrics, add `{"name":"metrics","type":"hive","options":{"database":"rca_demo","table":"service_metrics"}}`
to `RCA_CONNECTORS` (in `.env`, or in `docker-compose.yml` for the Docker stack).

### Benchmark: sequential vs parallel collection

```bash
python scripts/benchmark_parallel.py --mode real --runs 10          # seeded, reachable stores
python scripts/benchmark_parallel.py --mode simulated --latencies es=120,mongo=80,hive=900
```

`real` times the production collector against your configured connectors (LLM planner off
unless `--use-llm`). `simulated` uses sleep-based stand-ins, labels its output SIMULATED,
and only demonstrates orchestration overhead.

### Tests

```bash
cd backend && pytest tests -v
```

RCA tests (`tests/test_rca_*.py`) cover the connector contract and query safety, the
correlator, ranker JSON validation, the collector (parallelism, timeouts, partial results,
planner fallback) and the engine/API. The LLM is always mocked there; the live connectors
are not exercised by the unit tests.

## Evaluation

Measured by `scripts/evaluate.py` and written to [`docs/evaluation.md`](docs/evaluation.md) / `docs/evaluation.json`
(full per-scenario tables, per-run raw data, exact command, hardware). **All numbers come from 10 synthetic scenarios
generated by `scripts/scenarios/` (2 of them hard), not from production incidents**, against real Elasticsearch, MongoDB
and Ollama (`qwen2.5:7b`) on one laptop, 5 runs per scenario, 100,000+ background log events each (2026-10-26).

| Metric | Result |
|---|---|
| Top-1 accuracy (strict: all ground-truth keywords + cites an expected source) | 56% (28/50 runs) |
| Top-3 accuracy (strict) | 72% |
| Top-1 / Top-3, lenient (at most one keyword missing; secondary) | 72% / 90% |
| Events in store per run (mean) | 103,948 |
| Records collected per run (mean) | 3,184 |
| Records cited by hypotheses per run (mean) | 8.3 |
| Evidence compression (collected / cited) | 383.6x |
| End-to-end latency p50 / p95 | 20.2s / 38.6s |
| Stage medians: collect / correlate / rank | 4.4s / 0.03s / 15.7s |
| Parallel vs sequential collection, connector I/O only | 1.13x (101ms vs 84ms) |
| Parallel vs sequential collection, with LLM query planning | 1.37x (5.9s vs 4.3s) |

Per scenario, strict top-1: 100% on config_timeout, disk_full, memory_leak_release and red_herring_cache; 80% bad_deploy_pool;
40% cert_expiry and dependency_outage; 0% on dropped_index, traffic_surge and cpu_throttling_metrics_only. The misses and their
reasons are listed in `docs/evaluation.md`.

Caveats: the scenarios were written alongside the system and the matching is keyword-based, so it measures wording and
citation, not reasoning quality. Metrics were stored in MongoDB because no Hive cluster was available, so the Hive connector is
not covered. The Elasticsearch connector drops info/debug logs server-side, which is why collected records are far below the
events in store. Run it yourself with `make eval-up` then `make eval` (see the Makefile for the Mongo port note).

A shareable summary with measured counts and the numbers above is in [docs/PROJECT_STATS.md](docs/PROJECT_STATS.md)
(regenerate with `python scripts/project_stats.py`). A failure analysis of the first run and the fixes it led to is in
[docs/failure_analysis.md](docs/failure_analysis.md); a re-evaluation after those fixes is interim and incomplete.

## Known limits

- The Hive evidence connector (PyHive) is implemented but has not been run against a real
  Hive cluster. The legacy `app/mcp/hive_connector.py` it sits beside is still stubs.
- Elasticsearch and MongoDB connectors are unit-tested with mocked clients, not integration-tested here.
- Hypothesis confidence is self-reported by the model, not calibrated.
- With a 7B local model, lower-ranked hypotheses are often weak; the top one is the useful output.
- Row caps (`RCA_MAX_RECORDS_PER_SOURCE`) truncate noisy sources keeping the newest rows (the incident window), and truncation is flagged.

---

# NL-to-Data Insight Platform (original product)

An agentic AI-powered web application that lets users query a data warehouse
in plain English and download results as chart images (PNG), PDF reports, or
PowerPoint decks. Structure/schema questions generate Mermaid diagram PNGs.

All AI inference runs locally — no cloud API keys required.

---

## Quick Start

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env — set SECRET_KEY, POSTGRES_PASSWORD, HIVE_* vars

# 2. Start all services
docker compose up --build

# 3. Pull the Ollama model (one-time, ~4.7GB)
docker exec nl_insight_ollama ollama pull qwen2.5:7b

# 4. Open the app
#    Frontend: http://localhost
#    API docs: http://localhost:8000/docs  (development only)
```

No GPU required. Everything runs on CPU.

---

## Architecture Overview

```
Browser  ──HTTPS──►  Frontend (React/Vite)
                          │ REST + JWT
                          ▼
                     Backend (FastAPI)
                    ┌──────────────────────────────────┐
                    │ IntentParser     ◄──► Ollama (qwen2.5:7b)
                    │ SchemaManager    ◄──► Redis (schema cache)
                    │ QueryPlanner     ◄──► Ollama (qwen2.5:7b)
                    │ MermaidGenerator ◄──► Ollama + mmdc (diagram PNG)
                    │ MCPConnector     ◄──► Data Warehouse (Hive)
                    │ OutputEngine     ──►  Temp files
                    └──────────────────────────────────┘
                          │
                     PostgreSQL (metadata + audit — no result data)
                     Redis     (session state + schema cache)
```

### Pipeline Routing

The `IntentParser` classifies each prompt as one of two request types:

| `request_type` | Trigger | Pipeline |
|---|---|---|
| `data_query` | "Show sales by region as a bar chart" | SQL → chart/PDF/PPT |
| `structure_query` | "Show me an ER diagram of the schema" | LLM → Mermaid → mmdc → PNG |

See `docs/HLD.md` for the full high-level design and `docs/LLD.md` for
module-level specifications, class signatures, and sequence diagrams.

---

## Project Structure

```
nl-data-insight/
├── backend/
│   ├── app/
│   │   ├── main.py               FastAPI entry point
│   │   ├── config.py             Pydantic-settings (env vars)
│   │   ├── api/
│   │   │   ├── deps.py           Shared FastAPI dependencies
│   │   │   └── routes/
│   │   │       ├── auth.py       /auth endpoints
│   │   │       ├── chat.py       /chat endpoints (NL pipeline)
│   │   │       └── output.py     /output file download
│   │   ├── core/
│   │   │   ├── intent_parser.py  LLM intent extraction + request_type routing
│   │   │   ├── schema_manager.py Schema cache via MCP
│   │   │   ├── query_planner.py  LLM query generation
│   │   │   └── session_manager.py Redis session CRUD
│   │   ├── llm/
│   │   │   ├── base.py           LLMProviderBase ABC
│   │   │   └── ollama_provider.py OllamaProvider (qwen2.5:7b)
│   │   ├── mcp/
│   │   │   ├── base.py           Abstract connector interface
│   │   │   └── hive_connector.py HiveServer2 implementation
│   │   ├── output/
│   │   │   ├── chart.py          PNG chart renderer (matplotlib)
│   │   │   ├── mermaid_gen.py    Mermaid diagram generator (LLM + mmdc)
│   │   │   ├── pdf_gen.py        PDF generator (reportlab)
│   │   │   └── ppt_gen.py        PowerPoint generator (python-pptx)
│   │   ├── models/
│   │   │   ├── db.py             SQLAlchemy ORM models
│   │   │   └── schemas.py        Pydantic schemas + internal dataclasses
│   │   └── utils/
│   │       └── security.py       JWT + bcrypt helpers
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   └── src/
├── docs/
│   ├── HLD.md
│   └── LLD.md
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Adding a New Data Warehouse Connector

The MCP connector layer is fully pluggable. To add Snowflake, for example:

1. Create `backend/app/mcp/snowflake_connector.py`
2. Subclass `MCPConnectorBase` and implement `discover_schema`, `execute_query`, `test_connection`, `close`
3. Add `"snowflake"` to `ConnectorType` in `config.py`
4. Add a branch in `get_mcp_connector()` in `api/deps.py`
5. Document `SNOWFLAKE_*` env vars in `.env.example`

No changes to the core orchestration layer, routes, or frontend are required.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | HS256 JWT signing key (generate: `openssl rand -hex 32`) |
| `POSTGRES_PASSWORD` | Yes | PostgreSQL password |
| `OLLAMA_MODEL` | No | Ollama model for all tasks (default: `qwen2.5:7b`) |
| `HIVE_HOST` | Yes (Hive) | HiveServer2 hostname |
| `HIVE_PORT` | No | HiveServer2 port (default: 10000) |
| `HIVE_AUTH_MECHANISM` | No | PLAIN / KERBEROS / LDAP (default: PLAIN) |
| `MCP_CONNECTOR_TYPE` | No | Active connector (default: hive) |
| `SESSION_TTL_SECONDS` | No | Redis session TTL (default: 86400) |
| `QUERY_TIMEOUT_SECONDS` | No | MCP query hard timeout (default: 25) |

See `.env.example` for the full list.

---

## Development

### Backend (without Docker)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
# Install mermaid-cli (requires Node.js):
npm install -g @mermaid-js/mermaid-cli
cp ../.env.example ../.env   # configure DATABASE_URL, REDIS_URL, etc.
uvicorn app.main:app --reload
```

### Frontend (without Docker)

```bash
cd frontend
npm install
npm run dev   # starts Vite dev server on http://localhost:5173
              # proxies /auth, /chat, /output to http://localhost:8000
```

### Running Tests

```bash
cd backend
pytest tests/ -v
```

---

## Security Notes

- JWT tokens are stored in React component memory (not `localStorage`) to prevent XSS theft
- Passwords are bcrypt-hashed; plaintext is never stored
- Every warehouse query is executed under the authenticated user's identity (RBAC/RLS at the warehouse level)
- Result rows are never written to PostgreSQL — only query text and metadata in the audit log
- Output file download tokens are compound `{session_id}:{uuid}` values stored in Redis
- The internal Docker network (`nl_insight_network`) is isolated; only ports 80 and 8000 are exposed on the host

---

## Local Model Stack

| Task | Model | Runtime |
|---|---|---|
| Intent parsing + routing | `qwen2.5:7b` | Ollama (CPU) |
| HQL/SQL generation | `qwen2.5:7b` | Ollama (CPU) |
| Mermaid diagram syntax | `qwen2.5:7b` | Ollama (CPU) |
| Diagram rendering | N/A | mermaid-cli (mmdc + headless Chromium) |
| Data charts | N/A | matplotlib (server-side) |

No GPU required. `qwen2.5:7b` runs on CPU with ~8GB RAM. First pull: ~4.7GB.
