# WeCom TraceMind

### Traceable community intelligence for customer operations

**WeCom TraceMind** is a local-first AI operations system that turns community conversations into **structured events, risk alerts, demand signals, periodic reports, and evidence-grounded answers**.

The central design goal is not simply to summarize chat. It is to make every downstream conclusion **auditable**: quantitative claims come from deterministic SQL aggregation, while generated answers and reports are constrained by retrieved evidence that can be traced back to the original group, sender, timestamp, and message.

> Current public release: downstream analytics, structured classification, reporting, risk handling, and traceable QA. Production WeCom collection should be connected through an authorized official or enterprise-approved ingestion path.

## Product Walkthrough

### 1. AI Summary & Operations Workspace

![AI summary and operations workspace](docs/images/ai-summary.jpg)

The main workspace combines **multi-group selection, collection controls, AI summaries, historical summaries, and weekly operations reports**. Summaries are generated over an explicitly selected message scope so operators can review what data was included before downstream actions are taken.

### 2. Evidence-Traceable AI QA

![Evidence-traceable AI QA](docs/images/traceable-qa.jpg)

The QA interface answers over raw messages, structured events, demand records, summaries, and reports while exposing the provenance behind the answer. It can surface the **group, group identifier, sender, timestamp, original message, and evidence ID**, allowing a generated conclusion to be checked against the underlying conversation rather than treated as an opaque answer.

### 3. Deep Analysis Dashboard

![Deep analysis dashboard](docs/images/deep-analysis.jpg)

The analysis dashboard separates **risk alerts, structured events, demand signals, reports, and the raw message stream** into reviewable views. Risk predictions remain human-confirmable or rejectable, and higher-risk items can be routed through a WeCom robot without making the model prediction itself the final operational decision.

## Why this project

Operational community chat is noisy and difficult to use directly. A useful system needs to answer three different questions at once:

1. **What happened?** — identify risks, product feedback, demands, release issues, questions, style preferences, and sentiment at message level.
2. **What is changing?** — aggregate stable counts and trends without asking an LLM to invent or recalculate numbers.
3. **Where did this conclusion come from?** — preserve provenance so summaries and answers can be traced to the original evidence.

This leads to a hybrid design in which **LLMs interpret language, SQL computes facts, and the retrieval layer preserves evidence provenance**.

## System architecture

```text
Community messages
      │
      ▼
SQLite message store
      │
      ├── Noise filtering
      │
      ├── Message-level LLM classification
      │      ├── risk
      │      ├── feedback
      │      ├── demand
      │      ├── style
      │      ├── release_issue / release_intel
      │      ├── question
      │      └── sentiment
      │
      ├── Demand registry + human confirmation
      │
      ├── Risk routing + WeCom webhook
      │
      ├── SQL aggregation
      │      └── deterministic counts / trends / rankings
      │
      ├── Daily / weekly report generation
      │      └── LLM writes wording from precomputed statistics
      │
      └── Traceable QA agent
             ├── multi-table lexical retrieval
             ├── demand → event → message expansion
             ├── nearby-message expansion
             ├── evidence quota / truncation
             └── SSE streaming answer with evidence IDs
```

## Core engineering ideas

### 1. Traceable QA instead of opaque RAG

The QA layer searches across five evidence sources:

```text
messages + events + demands + summaries + reports
```

The retrieval pipeline is deliberately inspectable:

```text
question
  → Chinese substring candidates
  → multi-table retrieval
  → evidence scoring
  → graph-like expansion
  → quota-based evidence packing
  → LLM answer
  → evidence IDs
```

A direct message match is not always enough. Reports and summaries may paraphrase the source conversation, so the system can expand through relationships such as:

```text
demand → event → original message
```

It also adds nearby messages from the same group to recover local conversational context. The final prompt is bounded by per-type quotas and a global character budget rather than blindly passing every match to the model.

See [`docs/evidence-tracing.md`](docs/evidence-tracing.md) for the retrieval design.

### 2. Structured message understanding

The classification pipeline converts raw messages into normalized operational events. It includes:

- an 8-type event schema;
- demand-name normalization against an existing demand registry;
- low-temperature structured generation;
- tolerant JSON parsing;
- batch splitting after retriable model/API failures;
- idempotent event reconstruction;
- human confirmation/rejection state;
- risk delivery state persisted separately from classification state.

This separates **raw conversation** from a more stable operational representation that can be aggregated and reviewed.

### 3. LLMs do not calculate report numbers

A key reliability rule in this project is:

> **SQL computes numbers; the LLM only turns those numbers into readable operational language.**

The aggregation layer computes message volume, active users, risk counts, feedback rankings, demand frequency, trend comparisons, and sentiment statistics before report generation. The report prompt explicitly forbids modifying, estimating, or inventing missing values.

This makes the numeric part of daily and weekly reports reproducible independently of the language model.

### 4. Human-in-the-loop risk handling

Risk events are classified into three levels. Higher-risk events can be pushed to a configured WeCom robot while remaining reviewable in the dashboard.

The system keeps separate state for:

- model classification;
- human confirmation/rejection;
- webhook delivery;
- re-push operations.

This avoids treating a model prediction as a final operational decision.

### 5. Local-first, dependency-light architecture

The core server uses the Python standard library, SQLite, and vanilla JavaScript. This keeps the prototype easy to inspect and deploy while leaving clear boundaries for later replacement with a production web framework, search engine, or vector store.

## Deterministic vs. generative responsibilities

| Component | Deterministic / auditable | LLM-assisted |
| --- | --- | --- |
| Message storage | SQLite schema, IDs, timestamps | — |
| Noise handling | rule-based filtering | — |
| Event extraction | schema validation, persistence, retry state | semantic classification |
| Risk workflow | state transitions, webhook routing | risk/event interpretation |
| Metrics | SQL aggregation | — |
| Daily / weekly reports | fixed statistics payload | wording and interpretation |
| QA retrieval | lexical search, relationship expansion, quotas | final grounded answer |
| Provenance | evidence IDs + source metadata | citation-aware response wording |

## Repository structure

```text
.
├── app.py                     # HTTP API, SSE endpoints and schedulers
├── lib/
│   ├── classifier.py          # structured message classification
│   ├── chat.py                # retrieval, evidence expansion and QA prompts
│   ├── db.py                  # SQLite schema, environment and runtime paths
│   ├── llm.py                 # OpenAI-compatible model client
│   ├── noise.py               # message noise rules
│   ├── reports.py             # daily / weekly report generation
│   ├── stats.py               # deterministic SQL aggregation
│   └── wecom.py               # WeCom robot delivery and risk alerts
├── public/
│   ├── index.html             # operations workspace
│   ├── panel.html             # deep analysis dashboard
│   ├── app.js / panel.js      # browser logic
│   ├── markdown.js            # lightweight markdown rendering
│   └── styles.css             # UI styling
├── scripts/
│   ├── classify_once.py       # one-shot classification CLI
│   └── run_report.py          # report-generation CLI
├── docs/
│   ├── evidence-tracing.md    # retrieval / provenance design notes
│   ├── roadmap.md             # development notes and planned extensions
│   └── images/
│       ├── ai-summary.jpg
│       ├── traceable-qa.jpg
│       └── deep-analysis.jpg
├── .env.example
├── .gitignore
├── start.sh
├── stop.sh
└── restart.sh
```

## Quick start

The project intentionally has no third-party Python runtime dependency for the core server.

```bash
cp .env.example .env
```

Configure at least:

```bash
MODEL_API_URL=https://api.openai.com/v1/chat/completions
MODEL_API_KEY=...
MODEL_NAME=gpt-4o-mini
```

Then start the local service:

```bash
./start.sh
```

or directly:

```bash
python3 app.py
```

The server opens on `http://127.0.0.1:8787` by default and automatically tries the next few ports if that port is occupied.

### Useful CLI commands

```bash
# Classify pending messages
python3 scripts/classify_once.py --days 7

# Generate a daily report
python3 scripts/run_report.py --type daily --date 2026-09-01

# Generate a weekly report
python3 scripts/run_report.py --type weekly --date 2026-09-01
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `MODEL_API_URL` | OpenAI-compatible `chat/completions` endpoint |
| `MODEL_API_KEY` | model API credential |
| `MODEL_NAME` | model identifier |
| `MODEL_TIMEOUT_SECONDS` | request timeout |
| `MODEL_MAX_TOKENS` | generation limit |
| `PORT` | local web-server port |
| `WECHAT_WEBHOOK_URL` | summary/report WeCom robot webhook |
| `RISK_WEBHOOK_URL` | dedicated risk-alert webhook |
| `CLASSIFY_DAYS` | default classification look-back window |
| `SUMMARY_INTERVAL_MINUTES` | optional collection/summarization interval |
| `REPORT_DAILY_HOUR` | scheduled daily-report hour |
| `AUTO_PUSH_DAILY` / `AUTO_PUSH_WEEKLY` | optional automatic report delivery |
| `IGNORE_SENDERS` | sender names/keys to exclude from analysis |

The `WECOM_*` variables in `.env.example` are reserved for an **authorized local collection adapter**. The public repository does not ship a production WeCom data-extraction implementation.

## Data model

The main tables are intentionally separated by semantic responsibility:

```text
messages
   │
   ├── events
   │      └── demands
   │
   ├── summaries
   ├── reports
   └── chat_messages
```

`messages` remain the primary source of truth. Derived tables store structured or generated views while retaining message IDs and timestamps needed for provenance.

## Reproducibility and evaluation

The public repository currently exposes the implementation and deterministic data-processing rules, but it does **not** contain a sanitized benchmark dataset or a verified end-to-end accuracy result. No classification or QA quality number is claimed here without a reproducible artifact.

Useful evaluation directions for a sanitized test set include:

- event extraction precision / recall by event type;
- risk-level precision and false-alert rate;
- demand normalization consistency;
- evidence retrieval Recall@K;
- answer citation correctness / groundedness;
- report numeric consistency;
- latency and model-call cost.

A lightweight GitHub Actions workflow performs Python syntax checks on the public source tree.

## Data and privacy

Runtime SQLite databases, WAL files, local exports, `.env`, logs, and other operational artifacts are excluded through `.gitignore`.

This repository is intended to contain **system code and design evidence, not customer conversation data or credentials**. If a historical commit ever contained real operational data, removing it from the current tree does not erase that historical Git object; repository history should be rewritten separately before treating the repository as fully sanitized.

## Design scope

This is a compact research/engineering prototype rather than a production customer-support platform. Its strongest focus is the boundary between **language understanding, deterministic analytics, provenance, and human review**.

The most important design principle is simple:

> A useful AI operations system should not only produce an answer — it should preserve enough evidence to explain where that answer came from.
