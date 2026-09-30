# Day 4 Plan — Postgres: Review Records and a Graph That Survives a Crash

## Why this day matters

Up to now, a review exists only for as long as the HTTP request that started it. The result is returned in the response and then forgotten. If the process dies halfway through, everything spent so far is lost. There's no record that the review happened, no way to look it up later, and no way to continue it.

Today gives reviews a **memory that outlives the process**, in two distinct forms:

1. **A `reviews` table:** the *business record*. Who asked, for which PR at which commit, with which engine; its status (queued → running → succeeded/failed); the findings; what it cost. This is what `GET /reviews/{id}` reads today and what Week 4's MCP tools (`get_review_status`, `list_recent_reviews`) will read.
2. **LangGraph checkpoints:** the *execution record*. The graph's state after every step, so an interrupted run can resume where it stopped instead of starting over and paying again.

Day 5's queue depends on both. A worker picks up `{"review_id": ...}` and needs a row to look up (1), and SQS will sometimes deliver a message *again* after a worker crash, at which point the graph should resume, not restart (2). Today builds both and proves them with the queue not yet in the picture.

---

## Background concepts (like I'm five, then for real)

### A relational table
**Like I'm five:** a spreadsheet where every column has strict rules. This column must be a number, that one can never be empty, this one can only say "queued", "running", "succeeded" or "failed". If you try to break a rule, the spreadsheet refuses to save.

**Really:** columns with types, `NOT NULL`, and `CHECK` constraints. That's the same "typed boundary" idea as Pydantic, enforced by the database itself, so even a buggy code path (or a manual `psql` session) can't write an impossible state.

### Transactions
**Like I'm five:** moving a coin from one piggy bank to another. You never want the coin to be out of the first bank but not yet in the second. Either both happen or neither does.

**Really:** `BEGIN … COMMIT`. Today it matters for migrations (a half-applied migration is the worst possible state) and for "update status *and* write the result" in one step.

### Indexes, and a *partial unique* index
**Like I'm five:** an index is the index at the back of a book, so you can find a page without reading everything. A *partial unique* index is a restaurant rule: "each table can have only one *open* order at a time". Closed orders don't count, so the same table can have many old ones.

**Really:** `CREATE UNIQUE INDEX … WHERE status IN ('queued', 'running')` means *at most one active review per (installation, repo, PR, head commit, engine)*, while any number of completed ones can exist. This is what turns "someone clicked review twice" (or, on Day 5, "a webhook fired twice") into *one* paid review instead of two, enforced by Postgres even under concurrency, where an application-level "check, then insert" has a race window.

### Connection pool
**Like I'm five:** calling the database is like making a phone call, and dialing takes a while. A pool keeps a few phones already connected, so whoever needs one picks it up, talks, and puts it back.

**Really:** opening a Postgres connection costs a TCP handshake, TLS, authentication, and a server-side backend process. `psycopg_pool.AsyncConnectionPool` keeps some open and lends them out. This is exactly Week 1's shared `httpx.AsyncClient` reasoning, applied to the database. One pool per process, opened in the lifespan and shared through DI.

### Migrations
**Like I'm five:** numbered instruction cards for building the database's shelves. Card 1, then card 2, never skipped, never done twice. A notebook remembers which cards are done.

**Really:** `src/db/migrations/001_reviews.sql`, `002_….sql`, … applied in order, each in its own transaction, each recorded in a `schema_migrations` table so it never runs twice. Schema changes become reviewable, versioned files instead of commands someone once typed.

### Advisory lock
**Like I'm five:** a talking stick. Only whoever holds the stick may speak. Everyone else waits for it.

**Really:** `pg_advisory_lock(<some fixed number>)` is a named lock that means nothing to Postgres itself; it's purely for coordinating your own processes. On Day 5, `api` and `worker` start at the same time, and both run migrations at startup. The lock guarantees only one applies them, and the other waits, then sees everything is already applied and does nothing.

### JSONB
**Like I'm five:** a shelf where you can put a whole labelled box without first building a compartment for every item inside it.

**Really:** a binary JSON column type that can be queried and indexed. The findings list and usage object are always read and written *together with their review*, and nothing queries individual findings across reviews yet, so JSONB avoids a `findings` table plus joins for no current benefit. If Week 4+ needs "all high-severity findings across this repo," that's the moment to normalize (and `jsonb_path_query` can bridge until then).

### Checkpointer, thread, durability
**Like I'm five:** the bookmark from the week plan. The *thread ID* is the name written on the bookmark, so you find *your* place and not someone else's. *Durability* is how carefully the bookmark is placed: every page (very safe), every page but you don't wait for it to be placed before reading on (fast, tiny risk), or only when you close the book (fastest, no help if you fall asleep mid-chapter).

**Really:** `AsyncPostgresSaver` stores the graph state after each super-step under `thread_id`. Invoking the graph again with the same `thread_id` and `None` as input resumes from the last checkpoint. `durability` (verified: `Literal["sync", "async", "exit"]`) controls *when* checkpoints are persisted relative to the next step starting.

---

## Part A — Postgres in docker-compose

```yaml
# sketch — added to docker-compose.yml
  postgres:
    image: postgres:18            # pin the major; see note below
    environment:
      POSTGRES_USER: codereview
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: codereview
    ports: ["5432:5432"]          # host access for `uv run` dev + integration tests
    volumes: ["pgdata:/var/lib/postgresql"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U codereview"]
      interval: 5s
      retries: 10
  api:
    depends_on:
      postgres: { condition: service_healthy }
volumes:
  pgdata:
```

- **Version:** pin the major (`postgres:18`), never `latest`. A major upgrade changes the on-disk format and needs `pg_upgrade`, not just a new image. **Verify the data-volume mount path on the day:** the official image changed its data-directory layout for 18 (to support in-place major upgrades). Mount whatever the image's current docs specify, or the data silently lands in an anonymous volume and disappears on `docker compose down`.
- **`depends_on: condition: service_healthy`** means the API starts only after `pg_isready` passes. Without it, the API's startup migration races a database that's still initializing.
- **Password** comes from `.env`, the same pattern as every other secret. `DATABASE_URL=postgresql://codereview:${POSTGRES_PASSWORD}@postgres:5432/codereview` inside compose, and `@localhost:5432` for `uv run`.
- **Relationship to Month 1's doctriage Postgres:** on the VPS, the two projects share one Postgres *server* with **separate databases and users**, never shared tables. That decision belongs to Week 4's deploy, and it's noted here so today's naming (`codereview` DB/user) doesn't collide.

**Settings:** `database_url: str` (required, no default, following the fail-fast rule: added today because it's consumed today). `tests/conftest.py` sets a dummy value so non-integration tests never need a database.

---

## Part B — Pool, migrations, lifespan

### `src/db/pool.py`

```python
# sketch
def create_pool(database_url: str) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
```

Why each argument:
- **`open=False`, then `await pool.open()` in the lifespan:** opening is async and can fail. Doing it explicitly in the lifespan means a bad `DATABASE_URL` crashes **startup** with a clear error (fail fast), not the first request.
- **`autocommit=True`, `row_factory=dict_row`, `prepare_threshold=0`:** the Postgres checkpointer's documented connection requirements (it's typed against `AsyncConnection[DictRow]`; confirm the three flags against its README on the day). Because the app's own queries **share this pool**, they inherit them too:
  - `autocommit` means each statement commits on its own, *unless* wrapped in `async with conn.transaction():`. Multi-statement writes must use that explicitly, which is honestly a good discipline anyway.
  - `dict_row` means rows come back as dicts, which suits `ReviewRecord.model_validate(row)` well.
  - `prepare_threshold=0` disables server-side prepared statements, which otherwise break behind a transaction-pooling proxy like PgBouncer. It's irrelevant today and harmless, but it keeps the pool compatible with where this may be deployed.
- **`max_size=10`:** Postgres defaults to 100 max connections. Later the API and each worker process get their own pool, and Month 1's doctriage shares the server, so small pools are a courtesy. Tune only if pool waits show up.

**Alternative:** separate pools for the checkpointer and the app. Rejected: twice the connections for no isolation benefit at this scale, and the flag requirements are compatible with the app's own use.

### `src/db/migrate.py`: a ~40-line runner

1. Take a connection, `SELECT pg_advisory_lock(<constant>)`.
2. `CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())`.
3. For each `migrations/NNN_*.sql` in sorted order not yet in `schema_migrations`: in **one transaction**, execute the file and insert its version.
4. `pg_advisory_unlock` (in a `finally`).

Then `await checkpointer.setup()`. **LangGraph manages its own tables and migrations**. The project's runner never touches them, and `setup()` is idempotent.

**Alternatives considered:**

| Tool | For | Why not now |
|---|---|---|
| **Alembic** | The Python standard; autogenerates migrations from models | Built around SQLAlchemy models, which this project doesn't have. Autogeneration's value is diffing ORM models against the DB, so without an ORM it's just a heavier version of numbered SQL files |
| **yoyo-migrations / dbmate / Atlas / sqitch** | Mature, featureful | One more tool to learn for one table. The 40-line runner *is* the concept, and it stays readable end to end |
| **Migrations as a separate one-shot job** (a `migrate` compose service / CI step) instead of at app startup | The production-grade pattern. Schema changes happen once per deploy, before new code starts, and a failed migration blocks the deploy rather than crash-looping the app | Worth doing at Week 4's VPS deploy. Today, startup migration + advisory lock is safe for one API + one worker, and it keeps `docker compose up` a single command. Recorded as a deliberate "for now" |

### Lifespan (`src/main.py`)

Open the pool → run migrations → create `AsyncPostgresSaver(pool)` + `setup()` → compile the workflow graphs **with** the checkpointer → build the engine registry → store all of it on `app.state`. On shutdown, close the pool. Graph compilation moves here from Day 1's module-level `LOOP_GRAPH` because the checkpointer only exists after the pool opens. New dependencies: `get_db_pool`, `get_engines`.

---

## Part C — The `reviews` table and repository

### `001_reviews.sql`

```sql
-- sketch
CREATE TABLE reviews (
    id              uuid        PRIMARY KEY DEFAULT uuidv7(),
    installation_id bigint      NOT NULL,
    owner           text        NOT NULL,
    repo            text        NOT NULL,
    pr_number       integer     NOT NULL,
    head_sha        text        NOT NULL,
    engine          text        NOT NULL,
    status          text        NOT NULL
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'superseded')),
    attempts        integer     NOT NULL DEFAULT 0,
    result          jsonb,
    usage           jsonb,
    error           jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz
);

CREATE UNIQUE INDEX reviews_one_active_per_head
    ON reviews (installation_id, owner, repo, pr_number, head_sha, engine)
    WHERE status IN ('queued', 'running');

CREATE INDEX reviews_recent_by_repo
    ON reviews (installation_id, owner, repo, created_at DESC);
```

**Column-by-column decisions:**

- **`id uuid DEFAULT uuidv7()`:** `uuidv7()` is built into Postgres 18 (fall back to `gen_random_uuid()` if the image turns out older). UUIDs rather than `bigserial` because the ID appears in URLs, and sequential integers let anyone enumerate reviews and infer volume. Version **7** rather than random v4 because v7 is *time-ordered*, so new rows append to the end of the primary-key index instead of scattering across it (better insert locality, and "newest first" roughly follows key order).
- **`installation_id`** makes every read scoped by installation. It's the authorization boundary (Part D).
- **`owner` / `repo` stored lowercased:** GitHub treats `Owner/Repo` and `owner/repo` as the same repository. Without normalizing, the unique index would treat them as two, and a case difference would bypass deduplication. Lowercase on write, in one place: the repository function.
- **`head_sha`:** a review is *of a specific commit*, not of "the PR right now." See "Pinning" below.
- **`status text CHECK (...)` rather than a Postgres `ENUM`:** enums are easy to extend but awkward to change or remove values from, and they need a migration per change either way. A `CHECK` constraint is a one-line migration to alter.
- **`superseded`:** see "Pinning."
- **`attempts`:** incremented each time a run starts. Day 5's worker uses it (with SQS's receive count) to spot poison messages.
- **`result` / `usage` / `error` as JSONB:** see the concept section. `error` is JSON (`{"type": ..., "message": ...}`) rather than text so the Week 2 exception types survive the trip.
- **`updated_at` set explicitly in every `UPDATE`** rather than by a trigger. Triggers are invisible when reading the Python code, and every write in this project goes through one small repository module anyway.

### Pinning a review to a commit (the `superseded` status)

The gap between "review requested" and "review runs" is milliseconds today. On Day 5 it's however long the queue takes. If someone pushes a new commit in between, which commit should be reviewed?

- **Chosen:** the record pins `head_sha` at request time. When the run starts, `fetch_context` compares it with the PR's current head. **If they differ, the review ends as `superseded`, with no LLM calls.** Reviewing a stale commit is paying to comment on code that's already been replaced. In Week 4 the GitHub `pull_request.synchronize` webhook will request a fresh review for the new head anyway.
- **Alternative:** review exactly the pinned commit, by fetching the diff through the compare API (`GET /repos/{o}/{r}/compare/{base}...{head}`) instead of the PR diff endpoint. That's more correct in an archival sense, but it needs `base_sha` stored as well, a second diff-fetching path, and it still produces comments on an outdated commit. Worth it only if "review every commit" becomes a requirement.

### `src/db/reviews_repository.py`

Plain async functions taking the pool, with SQL as strings and **parameters always passed separately** (`%s` placeholders, never f-strings). That's the exact bug Day 3 plants in the seeded PR, and this project shouldn't commit it itself.

- `create_review(...) -> tuple[ReviewRecord, bool]` does `INSERT … ON CONFLICT (installation_id, owner, repo, pr_number, head_sha, engine) WHERE status IN ('queued','running') DO NOTHING RETURNING *`. If nothing is returned, it selects the existing active row and returns `(existing, False)`. Note the `WHERE` clause **must repeat the partial index's predicate** in the `ON CONFLICT` target, or Postgres can't match the index. That's a classic partial-unique-index gotcha, and worth a comment in the code.
- `get_review(review_id, installation_id) -> ReviewRecord | None`: always filtered by *both*.
- `claim_for_run(review_id) -> ReviewRecord | None` does `UPDATE … SET status='running', attempts=attempts+1, started_at=COALESCE(started_at, now()), updated_at=now() WHERE id=%s AND status IN ('queued','running') RETURNING *`. It returns `None` if the review is already terminal, which is how Day 5's worker detects duplicate deliveries. It's a single atomic statement, so there's no read-then-write race.
- `mark_succeeded`, `mark_failed`, `mark_superseded`, each setting `finished_at` and `updated_at`.

`ReviewRecord` (Pydantic) is the typed boundary for rows, `ReviewStatus` is a `Literal`, and neither exposes `installation_id` in API responses.

**Alternatives:** SQLAlchemy Core/ORM, SQLModel, Piccolo. All reasonable. Rejected for one table with five queries: an ORM's value grows with the number of tables and relationships, and hand-written SQL keeps the partial-index `ON CONFLICT` subtlety *visible* rather than hidden behind an ORM feature you'd have to look up.

---

## Part D — Endpoints

### `POST …/review` (the existing synchronous one) now persists

Flow: resolve the installation token → fetch PR metadata (for `head_sha`) → `create_review(status='running')` → run the engine with `thread_id = review.id` → `mark_succeeded` / `mark_failed` → return the result **plus the review ID**. A duplicate active review returns `409 Conflict` with the existing ID. (Day 5's asynchronous endpoint handles duplicates more gently, by returning the existing review.)

### `GET /github-app/reviews/{review_id}` — NEW (`src/routers/reviews.py`)

- `review_id: uuid.UUID`, so a malformed ID is an automatic `422`, with no hand-written parsing.
- Scoped by `get_current_installation_id`: `get_review(review_id, installation_id)`.
- **Not found *or* belongs to another installation → `404`**, deliberately the same response. A `403` would confirm that the ID exists, which leaks information across tenants. The same principle is behind "user not found" and "wrong password" returning the same login error.
- Response: `id`, `owner`, `repo`, `pr_number`, `head_sha`, `engine`, `status`, `attempts`, `result`, `usage`, `error`, and timestamps.

---

## Part E — Checkpoints: wiring, resume, cleanup

### Wiring

The graph engines run with `config={"configurable": {"thread_id": str(review_id)}, "recursion_limit": …}` and `durability="sync"`.

**Why `sync`:** one super-step here is one or more LLM calls costing cents and taking seconds, while a checkpoint write is a few milliseconds. Paying milliseconds to guarantee that a crash never loses a completed paid step is obviously right. `async` (persist while the next step runs) is for high-throughput graphs with cheap steps. `exit` (persist only at the end) gives no mid-run resume at all, which defeats the point. The stretch goal is to measure the difference once, for the retro.

The `loop` engine has no checkpoints, since it isn't a graph. That's a legitimate comparison point for `engine-comparison.md`: *durability is something the hand-rolled loop would have to build itself* (serialize `messages` after each iteration, reload on restart), and LangGraph gives it for one constructor argument.

### Resume logic in the runner

```python
# sketch
snapshot = await graph.aget_state(config)
if snapshot.next:                     # a checkpoint exists and there's work left → resume
    final = await graph.ainvoke(None, config, context=ctx, durability="sync")
elif snapshot.values:                 # thread already completed (e.g. crash after END, before mark_succeeded)
    final = snapshot.values
else:                                 # fresh run
    final = await graph.ainvoke(initial_input, config, context=ctx, durability="sync")
```

Passing `None` as input is LangGraph's "continue this thread" signal. The middle branch covers a sneaky case: the graph finished and its final checkpoint was written, but the process died before `mark_succeeded`. Without that branch, a retry would re-run a graph that had already finished.

### What resuming a fan-out actually does

When four specialists run in one super-step and one of them (or the process) fails, LangGraph has already saved the **completed tasks' writes** as *pending writes* on the checkpoint. On resume, **only the unfinished tasks run again**. That's the money-saving property, and it's exactly what today's crash test must prove, not assume.

### Enforcing Day 1's plain-data rule

Set `LANGGRAPH_STRICT_MSGPACK=true` in the test environment (the variable name comes straight from the warning reproduced before this plan). Any SDK object that sneaks into state then *fails the test suite* instead of logging a warning nobody reads. That turns a Day 1 convention into an enforced invariant.

### Cleanup

Checkpoints contain PR diffs, file contents, and tool results (other people's code, at rest), and they grow every step.
- **On `succeeded` / `superseded`:** `await checkpointer.adelete_thread(str(review_id))`. The `reviews` row holds everything worth keeping.
- **On `failed`:** keep them. They're the best debugging material there is (`aget_state_history` lets you walk every step of the failed run, LangGraph's "time travel"). A retention sweep (delete failed-run checkpoints after N days) is noted as a Week 4 gap, not built today.
- **Alternative:** keep everything forever for debuggability. Rejected on data-minimization grounds (the same reasoning as rejecting LangSmith on Day 3) and on storage growth.

---

## Files

1. `docker-compose.yml`: `postgres` service + volume; `api` depends on it
2. `src/config/settings.py`: `database_url`
3. `src/db/pool.py`, `migrate.py`, `migrations/001_reviews.sql`, `reviews_repository.py`: NEW
4. `src/dependencies/db.py`: `get_db_pool`; `src/dependencies/engines.py`: `get_engines`
5. `src/main.py`: lifespan (pool, migrations, checkpointer, compiled graphs, engine registry)
6. `src/schemas/review.py`: `ReviewStatus`, `ReviewRecord`, `ReviewRecordResponse`
7. `src/services/review_graph/*`: runners take `review_id`/`thread_id`; resume logic; `superseded` detection in `fetch_context`
8. `src/routers/review.py` (persisting), `src/routers/reviews.py` (GET): NEW/changed
9. `pyproject.toml`: `langgraph-checkpoint-postgres`, `psycopg[binary,pool]`
10. Tests (below)

---

## .NET parallels

- The repository module with raw SQL ≈ **Dapper**: hand-written SQL, typed results, no change tracking. SQLAlchemy/SQLModel would be the EF Core end of the spectrum.
- The numbered-SQL migration runner with a journal table ≈ **DbUp**, almost exactly. Alembic ≈ EF Core migrations.
- `psycopg_pool` ≈ Npgsql's pooling, which in .NET happens implicitly per connection string. Here it's an explicit object with an explicit lifecycle, closer to managing an `NpgsqlDataSource`.
- The partial unique index ≈ a SQL Server **filtered unique index** (`CREATE UNIQUE INDEX … WHERE Status IN (…)`).
- The checkpointer + `thread_id` ≈ Durable Functions' **history table + instance ID**. Resume-from-checkpoint ≈ orchestrator replay (though LangGraph resumes from stored state rather than replaying the event history).
- `404` for other tenants' resources ≈ the standard multi-tenant ASP.NET pattern of filtering by tenant in the query (a global query filter in EF Core) so a foreign ID simply doesn't exist.

---

## Automated verification

Integration tests use the `@pytest.mark.integration` marker and are **skipped unless `INTEGRATION_DATABASE_URL` is set**, so `uv run pytest` stays offline-capable. The run order is `docker compose up -d postgres`, then `INTEGRATION_DATABASE_URL=… uv run pytest -m integration`. Each test session creates a fresh `codereview_test` database, migrates it, and truncates `reviews` between tests.

- **Migrations** (integration): apply once; rerun is a no-op; two concurrent `migrate()` calls (`asyncio.gather`) both succeed and apply each migration exactly once (proves the advisory lock).
- **Repository** (integration): create → get; a second `create` for the same active head → `(existing, False)`; `OWNER/Repo` vs `owner/repo` dedupe to the same row; a completed review doesn't block a new one; `claim_for_run` on a terminal review → `None`; `get_review` with the wrong installation → `None`.
- **Constraints** (integration): an invalid status is rejected by the `CHECK`, not just by Pydantic.
- **Checkpoint resume** (unit, `InMemorySaver`): run the workflow with a fake where `synthesize` raises on its first call only; the run fails; re-invoke via the resume logic; assert via the fake client's call log that **no specialist was called again** and that the final result is correct.
- **Fan-out partial resume** (unit): one specialist raises an *unexpected* exception outside Day 2's failure isolation (simulating a process death mid-super-step); on resume, only that specialist runs again.
- **Completed-but-unrecorded** (unit): the thread is at `END`, the runner's resume logic returns the stored values without invoking anything.
- **Same checkpoint tests against `AsyncPostgresSaver`** (integration), because serialization through Postgres is precisely what `InMemorySaver` doesn't exercise the same way.
- **`LANGGRAPH_STRICT_MSGPACK=true`** set in the pytest environment for all graph tests.
- **Router:** `GET` unknown ID → `404`; other installation's ID → `404`; malformed ID → `422`; own ID → `200` with the record. Duplicate synchronous POST → `409` with the existing ID.
- `uv run pytest` and `uv run ruff check .` clean.

## Manual verification

```bash
docker compose up -d postgres
uv run fastapi dev src/main.py        # startup log: migrations applied, checkpointer set up

curl -X POST -b cookies.txt \
  "http://localhost:8000/github-app/repos/<owner>/<repo>/pulls/<seeded>/review?engine=workflow"
# → result + review id

curl -b cookies.txt http://localhost:8000/github-app/reviews/<id>   # → succeeded, with findings

# Crash test: start a large-PR review, kill the server mid-run (Ctrl-C twice, or kill -9),
# check psql: status='running', checkpoints exist for thread <id>.
# (Resuming it by hand is Day 5's worker's job; for today, a small script calling the
#  runner with the same review id is enough to watch it resume and skip finished specialists.)

docker compose exec postgres psql -U codereview -c \
  "select id, status, attempts, usage->>'estimated_cost_usd' from reviews order by created_at desc limit 5;"
```

---

## End-of-day checklist

- [ ] Postgres in compose with a healthcheck; data volume path verified for the pinned major version
- [ ] Pool opens in the lifespan with the checkpointer-compatible flags; bad `DATABASE_URL` fails at startup
- [ ] Migration runner applies once, is concurrency-safe (tested), and the checkpointer's `setup()` runs
- [ ] `reviews` table with the partial unique index; dedupe and case-normalization tested
- [ ] `GET /reviews/{id}` scoped by installation; foreign IDs → `404`
- [ ] Graph engines checkpoint with `thread_id = review_id`, `durability="sync"`
- [ ] Crash-resume proven: completed specialists are **not** re-run (unit + integration)
- [ ] `superseded` path works when the PR head moved
- [ ] Checkpoints deleted on success/superseded, kept on failure
- [ ] `LANGGRAPH_STRICT_MSGPACK=true` enforced in tests
- [ ] `uv run pytest` / `uv run pytest -m integration` / `uv run ruff check .` pass
- [ ] `docs/week-3/week-3-day-4.md` written, with its "Alternatives, Patterns, and Architecture Decisions" section

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Pool opened in the lifespan and shared via DI** is Week 1's `httpx.AsyncClient` decision, reapplied. The same "fail at startup, not on first request" logic as `Settings` also applies to the pool opening.
- **`404` rather than `403` for other tenants** is the cookie design's principle ("an opaque handle reveals nothing by itself") applied to authorization responses.
- **The partial unique index** is the first time this project pushes an invariant *into the data layer* rather than enforcing it in Python. It's the database-level sibling of Pydantic's typed boundaries: correctness that holds even when the application code is wrong or racing.
- **`LANGGRAPH_STRICT_MSGPACK=true` in tests** converts a written convention (Day 1) into a failing test, the same move as Week 2's decision to test the max-iterations cap directly rather than trust it.
- **Deleting checkpoints on success** applies the same data-minimization principle as the read-only GitHub App permissions in Week 1: hold other people's code for as short a time as the job needs.
- **Doctriage cross-over:** Month 1 also needs "document status" records (pending / classified / needs human review). This `reviews` table, with its status `CHECK` and a partial unique index for "one active job per input," is a reusable template for that project.
