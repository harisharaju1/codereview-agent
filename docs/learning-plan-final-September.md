# Learning Plan — Final Version

## Technology Update Notes — September 2026

This file is a refresh of `learning-plan-final-August.md`, carrying forward everything from that version unchanged except where noted below. The August file is left as-is, not overwritten — same reasoning this project already applies to its own weekly docs (a pattern that changes later gets a note in the week it changes, not a silent rewrite of history). These notes cover only what's directly relevant to work already in progress (Month 2) or genuinely load-bearing project-wide; they are not a re-verification of every technology named across all 5 months — later months should get their own check-in closer to when they actually start, the same way this one did.

**MCP: a major, breaking spec release landed July 28, 2026 (`2026-07-28`) — directly affects Month 2 Week 4.** The protocol dropped its stateful session model entirely: no more handshake, no more `Mcp-Session-Id`, no sticky-session requirement — an MCP server can now sit behind a plain round-robin load balancer, which is actually *good* news for this project's single-VPS-behind-Nginx deployment story (one less thing to get right). The Python SDK's v2 line renamed the in-SDK `FastMCP` to `MCPServer` and moved transport options off the constructor onto `.run()`; the old decorator-based handlers' automatic return-value wrapping is gone — handlers now return fully-constructed result types. A v2 server still answers the legacy `initialize` handshake for older clients, so this isn't a hard fork of the ecosystem, but Week 4's plan (`mcp` package, `FastMCP`-style tool registration) should be built against the current `MCPServer`/stateless shape, not the pattern implied by the original plan text. Worth reading the SDK's own migration guide directly when Week 4 starts, rather than trusting this summary for exact syntax.

**LangGraph: a May 12, 2026 release adds finer node-level execution control and a new streaming API — relevant to Month 2 Week 3.** Per-node timeouts, error recovery, and graceful shutdown are now first-class; a new channel type cuts checkpoint overhead for long-running threads; and a content-block-centric streaming API (v3, typed per-channel projections) replaced the older event-stream shape. Worth building Week 3's LangGraph rebuild against the current streaming API from the start, rather than the version that existed when this plan was first written — check `docs.langchain.com`'s changelog when Week 3 starts for the exact current syntax.

**`uv` (this project's package manager) was acquired by OpenAI in March 2026, folded into their Codex platform.** No functional or workflow impact on this project as of today — `uv init`/`add`/`run`/`sync` all still work exactly as used so far — but worth knowing as background, since it's a notable shift for a tool this project depends on for every single day of work.

**FastAPI (`fastapi[standard]>=0.141.1` in this project's `pyproject.toml`) is already current** — 0.141.1 is FastAPI's latest release as of this check. No action needed.

**Claude API: the Sonnet 5 introductory pricing window referenced implicitly by this plan's "~₹3,000-5,000 total LLM budget" estimate ended August 31, 2026.** Sonnet 5 is now $3.00/$15.00 per 1M input/output tokens (was $2.00/$10.00 intro). This project's `Settings.anthropic_model` default (`claude-sonnet-5`) is still the right choice — the budget estimate just runs slightly tighter than when the plan was first costed. Not a reason to change models; a reason to keep an eye on actual spend once Week 2's agent loop (Day 4) starts making real Claude calls.

---

## What This Plan Optimizes For

You're not trying to check boxes on 7 specific JDs. You're building toward a **profile** — a senior backend engineer who can design distributed systems and ship production AI-powered features. The roles you shared (ACKO, Polygon, Scapia, and similar) are the compass, not the destination.

You're starting applications in ~4 weeks. That means Month 1 has to produce something you can actually talk about in interviews — not "I'm halfway through a tutorial." Every month after that deepens the story and closes real gaps.

---

## Your Starting Position (What We're Not Wasting Time On)

You already have:
- Production backend experience in C#/.NET and Node/TypeScript (8 years)
- AWS hands-on (deployments, services)
- Production MongoDB + Redis experience with ORMs
- Working knowledge of LLM APIs, Bedrock, vector databases
- Exposure to prompt versioning, evals, function calling (tutorial-level)

The plan skips all of that. Everything below is specifically what you **don't yet have** at the level these roles demand.

---

## The Gaps, Ranked by Impact

### Gap 1: AI engineering in production vs. personal projects
You've called APIs and followed tutorials. These roles want someone who's built fallback logic when the LLM goes down, tracked cost-per-request, versioned prompts with rollback, built eval pipelines that catch regressions, and handled prompt injection. The jump from "I've used the API" to "I've shipped an AI feature and operated it" is the highest-value gap to close.

### Gap 2: Python backend fluency
ACKO prefers it, Polygon requires it, and the AI ecosystem lives in Python. You don't need to become a Python expert — you need to be comfortable enough to build a FastAPI service, write LangGraph agents, build MCP servers, and not fumble in a Python-heavy interview.

### Gap 3: System design at interview-caliber
You likely think about system design daily at work, but articulating it under interview pressure — capacity estimates, tradeoff analysis, deep dives on specific components — is a distinct skill. Most senior engineers underestimate how much structured practice this needs.

### Gap 4: Event-driven architecture / message queues
Kafka or equivalent shows up in almost every serious backend role. If you haven't built a multi-service event-driven system, this is a gap worth closing.

### Gap 5: Observability and production operations
Distributed tracing, structured logging, metrics dashboards, alerting. The difference between "I built it" and "I built it and I know when it breaks."

---

## Infrastructure: VPS + AWS Hybrid

Every project runs on a single VPS you manage yourself, with AWS used only where managed services give you interview-relevant experience.

### VPS Setup (Day 0 — before Month 1 begins, ~2-3 hours)

**Recommended: Hetzner CX32 — 3 vCPU, 8GB RAM, 80GB disk, ~₹550/month**

8GB RAM is the sweet spot — it lets you run Kafka + pgvector + MongoDB + Redis + your app services + monitoring tools simultaneously without swapping to disk.

**What to install and configure:**
- Docker + Docker Compose — all services run in containers
- Nginx as reverse proxy — route `project1.yourdomain.com`, `project2.yourdomain.com` to different containers
- Let's Encrypt SSL via Certbot — free HTTPS on all subdomains
- A cheap domain (~₹500-800/year from Cloudflare or Namecheap)
- Basic firewall (ufw) — only expose ports 80, 443, 22
- Fail2ban — prevent SSH brute force
- A simple GitHub Actions workflow or webhook so `git push` triggers deployment

This setup is reusable for all 5 projects. Each project gets its own `docker-compose.yml` and its own subdomain.

**YT Video #0:** "Setting Up a Dev Server From Scratch — VPS + Docker + Nginx + Auto-Deploy." Record this. This kind of infra-glue content does well because most tutorials skip it.

### Where AWS Fits

AWS is used surgically across the plan — not for hosting, but for specific managed services:

| AWS Service | Used In | Why |
|---|---|---|
| **Bedrock** | Projects 1, 5 | Model access — you already have Bedrock experience, deepen it |
| **SQS** | Project 2 | Queuing PR review requests — shows you know managed queues |
| **SES** | Project 3 | Email delivery channel for notifications |
| **SNS** | Project 3 | Push notification channel |
| **CloudWatch** | Project 3 | Push key metrics here alongside self-hosted Grafana |
| **S3** | Project 5 | File storage for uploaded eval datasets |
| **ECR** | Project 5 | Container registry — shows you know the Docker→ECR→deploy workflow |

### Total Cost Over 5 Months

| Item | Cost |
|---|---|
| Hetzner CX32 × 5 months | ~₹2,750 |
| Domain name | ~₹600 |
| AWS services (Bedrock, SES, SNS, S3, SQS, CloudWatch, ECR) | ~₹2,000 - ₹3,500 |
| LLM API costs (OpenAI + Anthropic direct) | ~₹3,000 - ₹5,000 |
| **Total** | **~₹8,350 - ₹11,850** |

---

## The Plan: 5 Months, 5 Projects

Each project is scoped for 10-15 hours/week. Every project produces a GitHub repo with architecture docs, a live demo on your VPS, and 1-3 YouTube videos.

---

### Month 1: Production-Grade AI Feature (The Interview Anchor)
**Timeline: Weeks 1-4 | 10-15 hrs/week**
**Stack: TypeScript + Node.js (Fastify), Claude/OpenAI APIs, pgvector, MongoDB, Redis, AWS Bedrock**
**Runs on: VPS (Docker) | AWS: Bedrock only**
**Live at: `doctriage.yourdomain.com`**

This is the most important month. You're applying in 4 weeks and you need something concrete to point to.

#### What You Build: "Intelligent Document Triage Service"

A backend service that accepts documents (insurance claims, invoices, support tickets — pick a domain), classifies them, extracts structured data, and routes them for processing. This isn't a chatbot wrapper. It's an AI-powered backend service with proper engineering around it.

#### Architecture:
```
Document Upload API (Fastify)
       ↓
  PDF Parser + Text Extraction
       ↓
  Classification Agent (LLM with structured output)
       ↓
  ┌────────────────┬──────────────────┐
  │ If extractable  │ If ambiguous      │ If unsupported
  ↓                ↓                   ↓
RAG Pipeline    Human Review Queue   Rejection + Reason
(embeddings →   (stored for manual   (logged with
pgvector →      classification)      confidence score)
retrieval →
LLM extraction)
       ↓
Structured Output Validation (Zod schemas)
       ↓
  Store in MongoDB + Return Result
```

#### Week 1 — Core pipeline with production patterns
- Set up the TypeScript project with proper structure (not a single file)
- Build the document upload + text extraction layer
- Implement the classification endpoint using Claude API with structured output (JSON mode)
- Key learning: response validation with Zod, retry logic with exponential backoff, timeout handling
- This is where you go beyond tutorials — handle the cases where the LLM returns garbage, times out, or hits rate limits
- **Infra:** Set up `docker-compose.yml` with your Fastify app + PostgreSQL (pgvector) + MongoDB + Redis. Verify everything runs locally and on your VPS

#### Week 2 — RAG pipeline with eval harness
- Set up pgvector (you've used vector DBs before, so this is about integration quality, not basics)
- Build the retrieval + extraction pipeline for classified documents
- Use **AWS Bedrock** for at least one model path (Titan embeddings or Claude via Bedrock) — so you can speak to Bedrock fluency in interviews alongside your direct API experience
- Build a **prompt versioning system**: prompts stored in a config file or DB with version IDs, so you can A/B test different prompts
- Build a **basic eval harness**: a script that runs 20-30 test documents through the pipeline and scores extraction accuracy against ground truth labels you manually create
- **Extend the eval harness with an LLM-as-a-judge layer** for the fields exact-match scoring can't fairly grade — free-text extracted fields (e.g. injury description) where two differently-worded answers can both be correct. See "A note on LLM-as-a-judge" below for what this requires and why it belongs here specifically, as a second layer on top of the basic harness rather than a replacement for it.
- This is the specific jump from tutorial-level to production-level

#### A note on the pgvector decision — alternatives considered

Considered: dedicated vector DBs (Pinecone/Weaviate/Qdrant — mature ANN indexing, but a new vendor/cost, and overkill at this project's actual scale), vector search bolted onto Mongo/Redis (Mongo's option requires Atlas, not available on the self-hosted `mongo:7` image this project runs; Redis is the wrong tool for durable primary storage), and in-process libraries like FAISS (no persistence, no network interface — you'd rebuild what a database already gives you). pgvector won because it reuses infrastructure already in the stack (no new DB to operate on an 8GB VPS), runs self-hosted with no managed-cloud dependency, and uniquely enables hybrid search — a single atomic SQL query combining a relational `WHERE` filter with `ORDER BY embedding <=> query_vector`, which a separate vector DB can't do without duplicating and syncing metadata. Honest tradeoff: pgvector's ANN indexing is less mature than a dedicated vector DB at very large scale — the right interview answer is "right tool for this scale and operational constraints," not "strictly better."

#### A note on separate `/classify` and `/embed` endpoints

Kept as separate endpoints rather than bundled into upload, because extraction (free, local, deterministic) and classification/embedding (paid, rate-limited, external) are different in kind — bundling would make upload's success depend on an LLM being up, undoing the point of Week 1's retry/timeout work. Not made fully async/queue-based yet either — that's deliberately Month 3's lesson (Kafka), not scope to pull forward into an already-compressed Week 2. The concrete payoff: separate endpoints make Week 2 Day 5's eval harness much easier — it can score classification and retrieval accuracy independently, re-run just one step when A/B testing a prompt version, and attribute a regression to a specific stage instead of a whole-pipeline pass/fail. (An eval harness would also survive a later move to async/SQS — it can call the same underlying functions a queue worker calls directly, in-process, bypassing the queue entirely.)

#### A note on LLM-as-a-judge (Week 2 Day 5, as a layer on top of the basic harness)

Exact-match scoring works for structured fields (`documentType`, a date) but fails free-text ones (an injury description) where differently-worded answers can both be correct. LLM-as-a-judge fixes that: send the extracted and expected values to an LLM with a grading prompt, get a verdict back — reusing the same tool-use + schema-validation pattern already built for the classifier, just pointed at a grading schema. Key things to know: **pointwise** grading (one output vs. ground truth) is what Day 5's basic harness already does; **pairwise** grading (which of two prompt versions' outputs is better, no ground truth needed) is what actually makes Day 4's prompt A/B testing work for free-text fields. Judges are noisy — known self-preference and length biases exist — so treat a judge score as a signal, not ground truth, and prefer a different/stronger model as judge than the one under test (e.g. Sonnet judging Haiku). The judge prompt is also an injection surface, since it quotes real extracted text — same class of problem as the main extraction prompt, not a separate one. Cost: a handful of extra LLM calls per eval run, comfortably within the plan's overall LLM budget.

#### Week 3 — Fallback logic, cost tracking, observability
- Implement fallback: when LLM confidence is below threshold → route to human review queue instead of returning bad data
- Add cost tracking: log token usage per request, calculate cost per document, expose a `/metrics` endpoint
- Add structured logging (pino) with correlation IDs so you can trace a document through the entire pipeline
- Add basic prompt injection defense — input sanitization, output validation against expected schema
- This week's work is directly what ACKO means by "making the build-vs-buy-vs-prompt call and justifying it with data"

#### Week 4 — Deploy to VPS + document + record
- Deploy to your VPS with Docker Compose. Configure Nginx to route `doctriage.yourdomain.com` to the container
- Set up a basic health check endpoint. Add a cron job or simple uptime monitor that alerts you (Telegram/Slack webhook) if the service goes down
- Write an architecture README with diagrams explaining every design decision and tradeoff
- Record 1-2 YT videos: one walkthrough of the architecture, one showing the eval harness catching a prompt regression
- Push to GitHub with clean commit history

#### What You Can Say in Interviews After This Month:
"I built a document processing pipeline that uses LLMs for classification and extraction, with fallback logic when confidence is low, prompt versioning with an evaluation harness, and cost tracking per request. It's running live — here's the URL."

That sentence plus a live demo puts you ahead of 90% of applicants claiming "AI experience."

**Month 1 AWS Spend: ~₹500-1,000** (Bedrock API calls only)

---

### Month 2: Python Backend + AI Agents + MCP
**Timeline: Weeks 5-8 | 10-15 hrs/week**
**Stack: Python + FastAPI, LangGraph, MCP SDK, Claude API with tool use, GitHub API, AWS SQS**
**Runs on: VPS (Docker) | AWS: SQS**
**Live at: `codereview.yourdomain.com` | MCP server at: `mcp.yourdomain.com`**

#### What You Build: "AI Code Review Agent" — as a service AND an MCP server

A service that connects to a GitHub repo, reads pull request diffs, and provides structured code review feedback — security issues, performance concerns, style violations, and suggestions. Uses multi-step agent reasoning with tool use. In the final week, you also expose this as an MCP server — meaning anyone with Claude Desktop, Cursor, or any MCP-compatible client can use your code review tool directly from their editor.

#### Why MCP Matters Now:
MCP (Model Context Protocol) became the de facto standard for connecting AI agents to external tools in 2025-2026. Adopted by Anthropic, OpenAI, Google, and Microsoft, there are now ~10K public MCP servers on the official registry. For AI engineering roles — especially Polygon's AI DevEx position — building and consuming MCP servers is a baseline expectation, not a differentiator. Skipping it would be like skipping REST APIs in 2015.

#### Why Python Specifically:
You already know TypeScript. Building the same kind of thing in Python forces you through the friction of a new ecosystem (virtual envs, type hints, async patterns, dependency management) in a real project context — not a "learn Python" tutorial. By the end of this month, you'll be comfortable enough in Python to not stumble in interviews where the team uses it.

#### Week 1 — Python backend foundations (through building, not studying)
- Set up a FastAPI project with proper structure: routers, dependency injection, Pydantic models, async endpoints
- Build the GitHub integration: OAuth flow, fetch PRs, fetch diffs using the GitHub REST API
- Your TypeScript background makes this fast — the concepts transfer, you're just learning syntax and tooling
- Key friction points to push through: Python's async model (asyncio vs Node's event loop), virtual environments, type hints with Pydantic vs Zod
- **Infra:** Add a new `docker-compose.yml` for this project on your VPS. Reuse the existing Redis and MongoDB containers by putting them on a shared Docker network, or run dedicated instances — your call based on isolation preference

#### Week 2 — Agent with tool use
- Build a code review agent using Claude API with tool use (function calling)
- Define tools the agent can call: `get_file_content`, `search_codebase`, `check_dependency_versions`, `run_linter`
- Implement the agent loop: LLM decides which tool to call → you execute it → feed result back → LLM decides next step or provides final review
- This is the jump from "I've seen function calling tutorials" to "I've built a multi-step agent that orchestrates real tools"

#### Week 3 — LangGraph agent + comparison
*(See "Technology Update Notes — September 2026" at the top of this doc: LangGraph's May 2026 release adds per-node execution control and a new v3 streaming API — build against the current shape, not what existed when this plan was first written.)*
- Rebuild the same agent using **LangGraph** (not plain LangChain). LangChain 1.0 and LangGraph 1.0 both shipped in October 2025. LangChain is now the simpler prototyping layer; LangGraph is what production agent systems use — stateful graphs, cycles, conditional routing, human-in-the-loop patterns
- Model the code review workflow as a LangGraph graph: nodes for "fetch diff," "analyze security," "analyze performance," "synthesize review," with conditional edges based on file types and findings
- Compare: what did LangGraph's graph abstraction give you vs. your raw agent loop from Week 2? When is the structure worth it vs. overkill?
- This is valuable because Polygon explicitly names LangChain/LlamaIndex, and having a grounded opinion on "raw API calls vs. LangGraph for production agents" is a strong interview signal
- Add memory/context management — the agent should understand the full PR context across multiple files
- Integrate **AWS SQS** to queue incoming PR review requests instead of processing synchronously — shows you know when to reach for managed queues

#### Week 4 — MCP server + production patterns + deploy
*(See "Technology Update Notes — September 2026" at the top of this doc: MCP's July 2026 spec release is a breaking, stateless rework — `FastMCP` is now `MCPServer`, no session handshake, no sticky-session requirement. Build against the current SDK's migration guide, not the pattern implied below.)*
- **Build an MCP server** that wraps your code review agent. Use the MCP Python SDK (`mcp` package) to expose tools like `review_pr(repo, pr_number)`, `get_review_status(review_id)`, and `list_recent_reviews(repo)`. This lets anyone connect your service from Claude Desktop, Cursor, VS Code, or any MCP client — which is exactly the kind of developer tooling Polygon's role is about
- The MCP server runs alongside your FastAPI API on the VPS — same backend logic, two interfaces (REST API for webhooks, MCP for AI-native clients). Route MCP traffic through Nginx at `mcp.yourdomain.com`
- Add rate limiting per GitHub org (don't blow your API budget on a monorepo with 500 files)
- Add caching in Redis — if the same file hasn't changed, don't re-analyze it
- Add the webhook endpoint so it can be triggered automatically on PR creation. Since your VPS has a public IP + SSL via Nginx, GitHub can POST directly to `codereview.yourdomain.com/webhook`
- Write the README, record YT videos
- Video angles: "I Built the Same AI Agent Three Ways — Raw API vs LangGraph vs MCP" and "Building an MCP Server From Scratch — The Protocol Every AI Engineer Needs to Know"

**Month 2 AWS Spend: ~₹200-500** (SQS is nearly free at low volume)

---

### Month 3: Event-Driven Architecture + Workflow Orchestration Engine
**Timeline: Weeks 9-12 | 10-15 hrs/week**
**Stack: TypeScript/Node.js (Fastify), Kafka, MongoDB, Redis, Docker**
**Runs on: VPS (Docker) | AWS: SES, SNS, CloudWatch**
**Live at: `eventplatform.yourdomain.com` | Dashboards at: `monitoring.yourdomain.com`**

#### What You Build: "Event-Driven Workflow Orchestration Platform"

Merged scope, not a swap: this is still the Kafka-backed event pipeline the plan originally called for — ingestion, brokers, consumer groups, dead letter queues, delivery, tracing. What changes is the middle layer. Instead of a hardcoded rules engine (`if event type = X → notify`), the "Rules Engine" service becomes a **config-driven workflow engine**: workflows are stored as data (JSON/YAML DAGs of trigger → condition → action nodes) in Postgres, not compiled into the service, and a generic executor walks that graph per event. This is the n8n-shaped piece — minus the drag-and-drop UI, which is out of scope for the reasons below.

#### Architecture:
```
Event Ingestion API (Fastify)
       ↓
   Kafka (3 brokers + Zookeeper — all on VPS)
       ↓
  ┌──────────────────┬──────────────┐
  ↓                   ↓              ↓
Workflow Engine     Aggregation    Deduplication
(DAG executor,      Service        Service
reads workflow
defs from Postgres)
  ↓                   ↓              ↓
  └──────────────────┴──────────────┘
              ↓
     Notification Dispatcher
     ├── AWS SES (email)
     ├── AWS SNS (push)
     └── Webhook (HTTP POST)
              ↓
     Delivery Tracking (MongoDB) + Retry Queue (Redis)
```

**Important note on VPS resources:** Kafka with 3 brokers + Zookeeper + your services + monitoring will eat 6-7GB RAM. The 8GB Hetzner box handles this, but just barely. Monitor memory usage from day one this month. If things get tight, drop to a single Kafka broker — less realistic but functional for learning the patterns.

**Important note on scope, since this month grew:** adding a workflow engine on top of everything Month 3 already carries (Kafka cluster, dedup, notification dispatch, circuit breakers, full observability stack) is real added scope, not a free substitution. Something has to give to keep this inside 10-15 hrs/week for 4 weeks. The plan below assumes you cut the drag-and-drop UI entirely (workflows are authored as JSON files, not built visually) and, if Week 2 still runs long, drop Design 4 or 5 from Month 4's five system designs rather than let Month 3 bleed into Month 4's calendar. Decide which of those two cuts you'd rather make now, not mid-month.

#### Week 1 — Kafka fundamentals + event ingestion
- Set up Kafka cluster in Docker Compose on your VPS (3 brokers, Zookeeper)
- Build the ingestion API: accepts events, validates schema, publishes to Kafka topics
- Learn: producers, consumers, partitions, consumer groups, offset management
- Test by publishing 10K events and watching them flow through — this is where you start feeling the difference between a message queue and an event log
- Unchanged from the original plan — this week is the foundation the workflow engine sits on top of, so it isn't touched by the merge

#### Week 2 — Workflow engine + processing patterns
- Design the workflow schema: a workflow is a DAG of nodes — one or more **trigger** nodes (match on event type/payload fields), **condition** nodes (boolean expression against event fields), and **action** nodes (notify, transform, forward). Store workflow definitions as JSON in Postgres, versioned like Month 1's prompt versioning
- Build the generic executor: given an incoming event and the set of active workflow definitions, walk each matching workflow's graph node-by-node and execute it. This replaces the old hardcoded `if event type = X → notify` block with the same logic expressed as data
- Build a small **action registry**: a pluggable map of action-type → handler function (`notify`, `log`, `forward_webhook`), so adding a new action type doesn't require touching the executor — this is the actual "node types" abstraction n8n is built on, scoped down to what a solo 1-week build can carry
- Build deduplication service: idempotency keys in Redis to prevent duplicate notifications
- Implement dead letter queue for failed processing — events that fail 3 times, or workflows that error mid-execution, get routed to a DLQ topic with the event **and** which node failed
- Key pattern: each service is independent, communicates only through Kafka; the workflow engine is a consumer group like any other, not a special case

#### Week 3 — Notification dispatch + reliability
- Build the dispatcher with pluggable channels:
  - **AWS SES** for email delivery
  - **AWS SNS** for push notifications
  - Webhook (plain HTTP POST) for generic integrations
- Implement retry with exponential backoff for failed deliveries
- Add circuit breaker pattern: if a notification channel is failing consistently, stop sending to it temporarily and route to fallback
- Track delivery status in MongoDB (sent, delivered, failed, retried)
- Push key metrics (events processed, delivery success rate, queue depth) to **AWS CloudWatch** alongside your self-hosted monitoring — so you can speak to both in interviews

#### Week 4 — Observability + document
- Add distributed tracing with OpenTelemetry — trace an event from ingestion through processing to delivery
- Set up **Jaeger** on VPS (Docker) for trace visualization at `monitoring.yourdomain.com/jaeger`
- Set up **Grafana + Prometheus** on VPS (Docker) for metrics dashboards at `monitoring.yourdomain.com/grafana`
- Dashboard should show: events/second, processing latency (p50/p95/p99), delivery success rate, dead letter queue depth, Kafka consumer lag
- Record YT videos — the live Grafana dashboard showing real event flow is compelling content

**YT Videos:**
- "Event-Driven Architecture From Scratch — Not Just Theory"
- "Building a Mini Workflow Engine on Kafka — The n8n Pattern, Minus the UI"
- "The Patterns That Actually Matter: Circuit Breakers, Dead Letters, Idempotency"

#### What You Can Say in Interviews After This Month:
"I built an event-driven platform on Kafka where business logic isn't hardcoded — it's a config-driven workflow engine, so a new automation is a JSON definition, not a deploy. Consumer groups, partitioning, dead letter queues, circuit breakers, and full distributed tracing are all live — here's the dashboard."

**Month 3 AWS Spend: ~₹500-800** (SES, SNS, CloudWatch — all low-volume)

#### A note on plugging Month 1's doctriage service into the workflow engine

Month 1's doctriage pipeline was deliberately split into separate `/classify` and `/embed` endpoints rather than one bundled call (see the Week 2 note in Month 1), specifically so each stage could be invoked independently later. That decision pays off here: doctriage's classify/extract/embed stages can be registered as **action handlers** in this month's action registry (`docs/learning-plan-final-September.md:299`), the same pluggable map already used for `notify`, `log`, and `forward_webhook`. A workflow node then calls `classify_document(event)` or `extract_document(event)` exactly like any other action type — no rework of the Month 1 service itself, no new endpoint, just a thin adapter that maps a workflow action to the existing function call. This is a concrete example of composing an earlier month's project into a later one instead of building each project in isolation, and it's worth calling out explicitly in the architecture README and in one of the Month 3 videos.

---

### Month 4: System Design Practice (Interview-Focused)
**Timeline: Weeks 13-16 | 10-15 hrs/week**
**No new infrastructure — your VPS keeps running all 3 live projects**

This month is different — it's not one big project. It's deliberate practice on the skill that gates senior/lead offers.

Your VPS running 3 live projects is an asset here — you can pull up live demos, Grafana dashboards, and tracing UIs during system design discussions. Most candidates can't do this.

#### What You Produce: 5 Detailed System Design Documents + Videos

Each design follows a consistent structure:
1. Requirements clarification (functional + non-functional)
2. Capacity estimation (back-of-envelope math)
3. API design
4. Data model
5. High-level architecture
6. Deep dive on one critical component
7. Tradeoffs and alternatives you considered

#### The 5 Designs (each mapped to your target role types):

**Design 1: AI-Powered Claims Processing System (ACKO-style)**
- How would you design a system that processes 100K insurance claims/day using LLMs?
- Deep dive: prompt caching strategy, cost optimization, fallback to rules engine
- You've built a version of this in Month 1 — now design it at 1000x scale

**Design 2: Real-Time Payment Notification System (Fintech)**
- Process payment events, detect anomalies, notify users in <2 seconds
- Deep dive: exactly-once delivery guarantees, regional failover
- Maps to Scapia/fintech roles

**Design 3: Developer Productivity Platform (Polygon-style)**
- Internal tooling platform that tracks AI tool adoption across an org
- Deep dive: telemetry pipeline, usage analytics, tool recommendation engine
- Maps directly to Polygon's JD

**Design 4: Content Recommendation Feed**
- Design a personalized content feed for 10M daily active users
- Deep dive: ranking model serving, caching strategy, A/B testing infrastructure
- General system design — shows range

**Design 5: Distributed Task Scheduling System**
- Design a system like a simplified Celery/Temporal that schedules and executes millions of tasks
- Deep dive: task deduplication, failure recovery, priority queues
- General system design — shows distributed systems depth

#### Weekly Rhythm:
- Monday-Wednesday: Research + write the design (3-4 hours)
- Thursday: Record a YT video walking through it (2 hours)
- Friday-Weekend: Review, get feedback (post on Reddit/Twitter, ask Claude to poke holes), revise (2-3 hours)
- This gives you ~2 hours of buffer per week

#### Parallel Track: Mock Interviews
- Do at least 2 mock system design interviews this month (Pramp, Interviewing.io, or find peers)
- Record what went badly. Redesign those systems

**Month 4 AWS Spend: ₹0** (VPS keeps running existing projects, ~₹550)

---

### Month 5: Capstone — Full-Stack AI Product
**Timeline: Weeks 17-20 | 10-15 hrs/week**
**Stack: Python + FastAPI backend, TypeScript + React frontend, Claude API, AWS Bedrock + S3 + ECR**
**Runs on: VPS (Docker) | AWS: Bedrock, S3, ECR**
**Live at: `evaldash.yourdomain.com`**

#### What You Build: "Open-Source AI Eval Dashboard"

A tool that any team can use to evaluate their LLM-powered features. Upload test cases, run them against different prompts/models, visualize accuracy and cost over time, catch regressions.

#### Why This Specific Project:
- ACKO's JD says "building evals, red-teaming prompts, catching regressions before they hit customers" — this IS that tool
- Polygon wants "AI usage dashboard that gives leadership and teams visibility into adoption, productivity metrics, and tooling health"
- It's genuinely useful — you might get real users, which is a stronger story than "I built a demo"
- It combines everything: Python backend, AI integration, system design, observability, real product thinking
- Open-sourcing it is a portfolio piece that speaks louder than a resume line

#### Core Features:
- Upload eval datasets (CSV/JSON with input + expected output pairs)
- Run evals against multiple models/prompts with configurable parameters
- Track metrics over time: accuracy, latency, cost, failure rate
- Visual diff when a prompt change causes regression
- Webhook alerts when accuracy drops below threshold
- Simple auth so teams can manage their own eval suites

#### Week 1 — Backend: eval runner engine
- FastAPI backend with the core eval execution logic
- Support for Claude and OpenAI models via direct API, plus **AWS Bedrock** models (Claude via Bedrock, Titan) — this makes the tool more versatile and deepens your Bedrock experience
- Async execution — queue eval runs in Redis, process in background worker
- Store results in MongoDB (you're comfortable here)
- Store uploaded eval datasets in **AWS S3** — your VPS disk isn't infinite, and this shows S3 integration

#### Week 2 — Backend: metrics + regression detection
- Time-series metrics storage (timestamped docs in MongoDB, or Redis TimeSeries if you want to stretch)
- Regression detection: compare current run against baseline, flag if accuracy drops >X%
- Webhook notification on regression
- Cost calculation per eval run (token usage × model pricing)

#### Week 3 — Frontend dashboard + API polish
- React dashboard showing eval results, trends over time, model comparison charts
- Prompt diff viewer — see exactly what changed between prompt versions
- Keep it functional, not flashy. recharts for graphs, clean layout
- Serve the frontend via Nginx on your VPS alongside the API

#### Week 4 — Open source prep + deploy
- Push Docker images to **AWS ECR** — shows you know the container registry workflow
- Write proper docs: README, setup guide, architecture doc, contributing guide
- Add Docker Compose for one-command local setup (so contributors don't need your VPS)
- Deploy the demo instance to your VPS at `evaldash.yourdomain.com`
- Record a YT video: "I Built an Open-Source LLM Eval Tool — Here's Why"
- Post on Hacker News, Reddit r/MachineLearning, Twitter

**Month 5 AWS Spend: ~₹500-1,000** (S3 + Bedrock + ECR)

---

## Full Timeline View

| Month | Project | Stack | Infra | Key Gaps Closed | Interview Story |
|---|---|---|---|---|---|
| **0** | VPS Setup | Docker, Nginx, Certbot | VPS | Ops fundamentals | "I manage my own infrastructure" |
| **1** | AI Document Triage | TypeScript, Fastify, pgvector, Bedrock | VPS + Bedrock | Production AI patterns | "I built and operate an AI doc processing pipeline with fallback logic, eval harness, and cost tracking" |
| **2** | AI Code Review Agent + MCP Server | Python, FastAPI, LangGraph, MCP SDK, Claude tool use | VPS + SQS | Python fluency, AI agents, MCP, LangGraph | "I built AI agents in raw API, LangGraph, and MCP — here's when I'd use each" |
| **3** | Event-Driven Workflow Orchestration Platform | TypeScript, Kafka, Redis, MongoDB, Postgres | VPS + SES/SNS/CloudWatch | Distributed systems, observability, config-driven orchestration (n8n-style engine, no UI) | "I designed and built a multi-service event-driven system with a config-driven workflow engine, tracing, circuit breakers, and live dashboards" |
| **4** | System Design Portfolio | Documentation + video | VPS (existing projects) | Interview-grade design skills | 5 detailed designs with video walkthroughs + live demos to back them up |
| **5** | AI Eval Dashboard | Python, FastAPI, React, S3, Bedrock, ECR | VPS + S3/Bedrock/ECR | Full product delivery, open source | "I built and open-sourced a tool for evaluating LLM outputs in production" |

---

## Applying Strategy

**Month 1 applications (5-8 apps):** Apply to roles slightly below your target — SDE2/Senior SDE roles at mid-stage startups. The goal isn't to accept an offer, it's to get into interview loops and learn what they actually ask. Treat every rejection as data.

**Month 2-3 applications (5-8 apps):** Apply to your actual target level — Senior SDE / SDE3 roles at companies like ACKO, Scapia, and similar. By now you have 2 projects to discuss and you've calibrated against real interviews.

**Month 4-5 applications (targeted):** Apply to the ambitious ones — roles like Polygon's AI DevEx role or lead-level positions. You now have a system design portfolio, multiple shipped projects, YouTube content that proves your thinking publicly, and a live VPS running everything.

**Where to look beyond your original list:**
- AI engineering roles at Indian startups (Observe.AI, Yellow.ai, Sarvam AI, Razorpay's AI team)
- Backend roles at fintech companies adding AI features
- Platform/developer experience roles at companies going "AI-first"
- Filter for: TypeScript or Python or "language-agnostic" backends + AI/LLM mentioned anywhere in the JD

---

## The Interview Story the Hybrid Infra Creates

"I run my projects on a VPS I manage myself — Docker, Nginx, SSL, monitoring, the whole stack. I use AWS where managed services make sense: Bedrock for model access, SES for email delivery, S3 for file storage, CloudWatch for alerting. I've made deliberate decisions about what to self-host vs. what to offload to AWS based on cost, reliability, and operational overhead."

That sentence signals three things interviewers look for:
1. You understand infrastructure, not just application code
2. You make pragmatic build-vs-buy decisions (a theme across every JD you shared)
3. You've actually operated services, not just deployed and walked away

---

## How to Use Claude Pro Across the Plan

- **Month 1:** Use Claude as your pair programmer for the eval harness and fallback logic. Paste your code and ask it to find edge cases. Use it to generate test documents for your pipeline.
- **Month 2:** Use Claude to explain Python idioms when you hit friction. Compare your FastAPI code against TypeScript equivalents. Ask it to review your LangGraph graph design and MCP server implementation. Use Claude Desktop to test your own MCP server — dogfooding your own tool.
- **Month 3:** Use Claude to debug Kafka consumer lag, explain partition rebalancing, review your circuit breaker implementation.
- **Month 4:** Do mock system design interviews with Claude. Present your design, ask it to poke holes, iterate. Ask it to role-play as a skeptical interviewer.
- **Month 5:** Use Claude to help architect the eval dashboard, write documentation, and review your open-source README for clarity.

---

## YouTube Content Arc

The narrative: **"Senior Backend Engineer Builds AI-Powered Products From Scratch"**

Not tutorials. Not "learn X in 10 minutes." You're documenting real engineering decisions, real mistakes, and real tradeoffs. That framing attracts an audience of engineers in similar positions and doubles as proof-of-work for hiring managers.

Rough content calendar:
- Month 0: 1 video (VPS setup)
- Month 1: 2-3 videos (architecture walkthrough, eval harness demo, "from .NET to AI engineering" story)
- Month 2: 2-3 videos (AI agents deep dive: raw API vs LangGraph vs MCP, building an MCP server, Python-vs-TypeScript-for-AI comparison)
- Month 3: 2 videos (event-driven architecture build, distributed systems patterns)
- Month 4: 5 videos (one per system design)
- Month 5: 2-3 videos (open source launch, capstone walkthrough, "what I learned in 5 months" retrospective)

Total: ~15 videos over 5 months.

---

## What Could Go Wrong

**10 hours/week is the floor, not the ceiling.** Some weeks — especially Week 2 of Month 1 (RAG + eval harness) and Week 1 of Month 3 (Kafka setup + ingestion) — will feel compressed. If you slip, cut scope on that project rather than pushing the whole timeline. A deployed service with 3 features beats an unfinished service with 6.

**Interview feedback loops are slow.** You might apply in Month 1 and not hear back for 3-4 weeks. Don't wait for feedback to keep building. The projects are valuable regardless.

**The AI space will change mid-plan.** Some new framework or model capability will drop and make a piece of your Month 2-3 work feel outdated. The engineering patterns (retries, fallbacks, evals, cost tracking) don't change even when the models do. Adapt the tools, keep the architecture.

**You might discover you hate Python.** That's real data. If by Month 2 you're fighting the language more than learning the concepts, consider whether the roles you're targeting actually require Python or just list it as preferred. Many AI engineering teams are pragmatic about language choice if you can demonstrate the system design thinking.

**VPS operations will eat time.** Budge 1-2 hours/week for maintenance — especially in Month 3 when Kafka is running. Set up that health check cron job early. When Kafka eats all the memory and OOM-kills your MongoDB at midnight, that's a learning moment, but also a time sink. Don't let VPS firefighting derail project work.
