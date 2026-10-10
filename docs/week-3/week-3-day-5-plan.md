# Day 5 Plan — SQS, the Worker, and Going Asynchronous

## Why this day matters

After Day 4, reviews are durable: they have a record, and they can resume. But they still run *inside an HTTP request*. The caller waits for minutes with a connection open. A deploy or restart of the API kills every in-flight review. And ten PRs opened at once means ten concurrent multi-minute requests hammering one process.

Today splits "accept the request" from "do the work":

- The **API** accepts a review request, writes a `queued` row, drops a tiny message on an SQS queue, and answers `202 Accepted` in milliseconds.
- A separate **worker** process pulls messages off the queue, runs the review with Day 4's checkpointing, and writes the result.
- The **caller** polls `GET /reviews/{id}` (Week 4 adds webhooks and MCP on top of the same flow).

This is the shape almost every production system uses for slow work, and the one Week 4 builds on directly: GitHub's webhook for "PR opened" must be answered within seconds, which is impossible if the review runs inline.

**Like I'm five:** a busy restaurant. The waiter doesn't cook. The waiter writes your order on a ticket, clips it to the kitchen rail, gives you a buzzer, and goes back to the tables. Cooks take tickets off the rail when they have free hands. If a cook burns something, the ticket goes back on the rail. If one ticket keeps going wrong, it's put in a special box for the manager to look at.

---

## Background concepts (like I'm five, then for real)

### Producer, consumer, queue
**Really:** the API is the *producer* (`SendMessage`), the worker is the *consumer* (`ReceiveMessage`), and SQS is the durable buffer between them. Neither needs the other to be up at the same moment. Messages wait in the queue (for up to the retention period, 4 days by default) until a consumer takes them.

### Long polling
**Like I'm five:** instead of running to the mailbox every second to check for letters, you stand by it for 20 seconds, and if a letter arrives you get it straight away.

**Really:** `ReceiveMessage(WaitTimeSeconds=20)` holds the request open until a message arrives or 20 s pass. Short polling (`0`) returns immediately and often empty, which burns requests (SQS bills per request) and CPU. **The arithmetic:** one idle worker long-polling = 3 requests/minute ≈ **130,000 requests/month**, comfortably inside SQS's free tier of 1M/month. Short polling in a tight loop could burn that in a day.

### Visibility timeout
**Like I'm five:** when a cook takes a ticket, it becomes invisible for 15 minutes. If the cook finishes, they bin the ticket. If they don't bin it in time (they fainted, or they're just very slow), it reappears for someone else.

**Really:** receiving a message doesn't delete it. It hides it for `VisibilityTimeout` seconds, and the worker must call `DeleteMessage` (with the *receipt handle* from that specific receive) once it's truly done. This is how SQS survives worker crashes with zero coordination. It's also the source of the most important subtlety of the day: **if the work takes longer than the visibility timeout, the message reappears *while still being processed*, and a second worker starts the same job.** See "Heartbeat" in Part C.

### At-least-once delivery and idempotency
**Like I'm five:** very occasionally the kitchen gets the same ticket twice. A smart cook first checks the order book: "table 7's soup: already served?" If so, they bin the duplicate instead of cooking it again.

**Really:** Standard SQS queues guarantee each message is delivered **at least once**, never "exactly once." Duplicates come from redelivery after a timeout, and occasionally from SQS's own distributed internals. So the worker must be **idempotent**: processing a message twice must have the same effect as processing it once. Here that's built from Day 4's pieces: `claim_for_run` returns nothing for an already-finished review (duplicate → delete, done), and checkpoints make a *re-run* of an in-progress review a *resume*, not a do-over.

### Dead-letter queue and redrive policy
**Like I'm five:** if a ticket has gone wrong three times, it goes in the manager's special box instead of back on the rail, so it can't keep ruining the kitchen, and so someone can find out what's wrong with it.

**Really:** a second queue plus a `RedrivePolicy` on the main one: `{"deadLetterTargetArn": "<dlq arn>", "maxReceiveCount": 3}`. Once a message has been received 3 times without being deleted, SQS moves it to the DLQ on the next receive attempt. A **poison message** (one that crashes the worker every time, perhaps from a bug or a malformed payload) is contained to 3 attempts, and preserved for inspection instead of silently lost. Set the DLQ's retention to the maximum (14 days), longer than the main queue's, so evidence isn't expiring before anyone looks.

### The dual-write problem
**Like I'm five:** you have to write the order in *two* notebooks: the order book and the kitchen ticket. If you trip after writing the first and before the second, they disagree.

**Really:** "insert the row" and "send the message" go to two different systems, and there is no transaction spanning both. Some ordering has to be chosen, and each ordering fails differently (Part B).

### Backpressure
**Like I'm five:** a cook with two free hands takes two tickets, not ten. If they grabbed ten, eight would sit going cold (invisible to everyone else) and might time out.

**Really:** the worker only requests as many messages as it has free processing slots (`MaxNumberOfMessages = free slots`, max 10). Taking more than it can process would hide messages from other workers and risk visibility timeouts on work that never even started.

### Least-privilege IAM
**Like I'm five:** the delivery driver gets a key to the front door only, not the whole building.

**Really:** the AWS credentials this app uses can do exactly five things to exactly one queue, and nothing else in the account.

---

## Manual steps (do these first)

1. **LocalStack auth token** (new since March 2026, confirmed while writing the week plan): create a free account at `app.localstack.cloud` (Hobby plan, non-commercial), generate a token under Settings → Auth Tokens, and add `LOCALSTACK_AUTH_TOKEN=...` to `.env`.
   *No-account fallback:* **ElasticMQ** (`softwaremill/elasticmq-native`), an SQS-compatible server configured with a small `elasticmq.conf` that defines both queues and the dead-letter relationship. The app code is identical, since only `AWS_ENDPOINT_URL` changes.
2. **AWS (for Part E only):** an IAM user with the policy in Part E, and its access key in `.env`. Region `ap-south-1`.

---

## Part A — Infrastructure: LocalStack + queues

```yaml
# sketch — added to docker-compose.yml
  localstack:
    image: localstack/localstack:<pinned calendar-version tag>   # LocalStack moved to YYYY.MM tags; pin one
    environment:
      LOCALSTACK_AUTH_TOKEN: ${LOCALSTACK_AUTH_TOKEN}
      SERVICES: sqs
    ports: ["4566:4566"]
    volumes:
      - ./localstack/init/ready.d:/etc/localstack/init/ready.d:ro
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:4566/_localstack/health"]
```

`localstack/init/ready.d/create-queues.sh` (runs once LocalStack is ready) uses `awslocal sqs create-queue` to create:
- `codereview-reviews-dlq` with `MessageRetentionPeriod=1209600` (14 days)
- `codereview-reviews` with `VisibilityTimeout=900`, `ReceiveMessageWaitTimeSeconds=20`, and `RedrivePolicy` pointing at the DLQ's ARN with `maxReceiveCount=3`

**Why the queue is created by an init script, not by the app:**
- *Alternative:* the app calls `CreateQueue` at startup if the queue is missing. That's convenient locally, but it means production credentials need `sqs:CreateQueue`, which breaks least privilege, and it quietly creates queues with whatever attributes the code happened to specify.
- Queues are infrastructure. Locally that's the init script; in AWS it's a one-time CLI/console step (Part E), and Terraform/CloudFormation later if the project ever grows into infrastructure-as-code.

**Settings** (added today, because they're consumed today):
- `sqs_queue_url: str` (required)
- `aws_region: str = "ap-south-1"`
- `aws_endpoint_url: str | None = None` (set to `http://localstack:4566` in compose, `http://localhost:4566` for `uv run`; **unset for real AWS**)
- `worker_concurrency: int = 2`

**AWS credentials are deliberately *not* `Settings` fields.** botocore's standard credential chain reads `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from the environment, `~/.aws/credentials`, or an instance role. Duplicating that in `Settings` would bypass role-based credentials if this ever runs somewhere with an IAM role, and it would put secrets on an object that's easy to accidentally log. LocalStack accepts any dummy credentials (`test`/`test`), set in compose.

---

## Part B — The producer: `POST …/reviews` → `202`

`POST /github-app/repos/{owner}/{repo}/pulls/{number}/reviews?engine=workflow`

1. Resolve the installation token; fetch PR metadata. A missing PR → `404` *now*, not minutes later in the worker.
2. `create_review(status='queued', head_sha=…)` (Day 4).
   - **An active review already exists for this head** → return `202` with the *existing* ID (`"created": false`). A repeated POST is harmless and idempotent, which is exactly what Week 4's webhooks need, since GitHub can deliver the same webhook more than once.
3. `SendMessage(MessageBody=json.dumps({"review_id": str(id)}))`.
4. Return `202 Accepted`, a `Location: /github-app/reviews/{id}` header, and a body of `{"review_id", "status": "queued", "status_url"}`.

**The dual-write decision: DB first, then send.**

| Order | If the process dies between the two writes | Verdict |
|---|---|---|
| **Insert row, then send** ✅ | A row stuck in `queued` with no message. Nothing runs, but nothing *wrong* runs, and it's detectable (`queued` and older than N minutes) | Chosen. The failure is visible and harmless |
| Send, then insert row | The worker may receive a message whose row doesn't exist yet (a race even without a crash), or never will | Rejected. The failure is confusing and the race is real |
| **Transactional outbox:** insert the row *and* an `outbox` row in one DB transaction; a relay process publishes outbox rows to SQS and marks them sent | Nothing is lost. The relay retries until published | **The rigorous production answer.** Deferred: it's a new process and table for a failure window of milliseconds at this scale. Named explicitly so it's a known choice, not an unknown gap |

If `SendMessage` itself fails (SQS/LocalStack down): `mark_failed(error={"type": "EnqueueFailed"})` and return `502`. Because the row is no longer *active*, the client's retry creates a fresh review instead of hitting the dedupe index.

**Known gap, recorded for Week 4:** a reconciliation sweep that re-enqueues (or fails) rows stuck in `queued`/`running` past a threshold. Today a stuck row is visible via `GET` (`status` + `updated_at`), just not automatically repaired.

**The Day 4 synchronous `POST …/review`** stays for this week (dev convenience; the Day 3 harness runs in-process anyway). Week 4 decides whether it survives. The MCP server's `review_pr` tool will most likely enqueue, not block.

---

## Part C — The worker: `src/worker.py`

### Startup and shutdown

`python -m src.worker`:
1. `Settings`, logging, pool open, migrations (advisory-locked, so safe alongside the API), checkpointer `setup()`, compiled graphs, `httpx.AsyncClient`, `AsyncAnthropic`, and the SQS client, via `AioSession().create_client("sqs", region_name=…, endpoint_url=…)` as an async context manager.
2. Install `SIGTERM`/`SIGINT` handlers that set a `stop` event.
3. Run the poll loop until `stop` is set, then **stop receiving**, wait for in-flight jobs up to a grace period, close everything, exit.

**Graceful shutdown is cheap here because of Day 4.** A job killed mid-run simply isn't deleted, so its message reappears after the visibility timeout and the next worker *resumes from the checkpoint*. So the grace period doesn't need to cover a whole review (compose `stop_grace_period: 60s` is enough), and a hard kill is safe, just slower to recover.

### The poll loop (backpressure built in)

```python
# sketch
semaphore = asyncio.Semaphore(settings.worker_concurrency)
while not stop.is_set():
    free = available_slots(semaphore)
    if free == 0:
        await wait_for_a_free_slot(); continue
    jobs = await receive_jobs(sqs, queue_url, max_messages=min(free, 10), wait_seconds=20)
    for job in jobs:
        spawn(process(job))          # acquires the semaphore, runs handle_job, acts on its decision
```

### `handle_job`: the decision table (the heart of the day)

`handle_job(job, deps) -> Decision` returns `DELETE` or `RETRY`, and the loop performs the SQS call. This keeps the decision logic testable with no SQS in the test at all.

| Situation | DB action | Decision | Why |
|---|---|---|---|
| Message body unparseable / no `review_id` | — | **DELETE** (and log loudly) | A malformed message can never succeed. Retrying it 3 times just delays the DLQ. (Alternative: let it go to the DLQ for inspection. Chosen instead: log the full body at `ERROR` and delete, because a message this service produces itself should never be malformed, so if one is, it's a bug to fix, not data to keep) |
| `claim_for_run` → `None` (already succeeded/failed/superseded) | — | **DELETE** | A duplicate delivery. The idempotency check doing its job |
| PR head moved since the request | `mark_superseded` | **DELETE** | Day 4's pinning rule |
| Success | `mark_succeeded`; delete checkpoints | **DELETE** | Done |
| **Deterministic** failure: `AgentExceededMaxIterationsError`, `AgentDidNotSubmitReviewError`, all specialists failed, GitHub `404`/`403` (PR deleted, app uninstalled) | `mark_failed` (keep checkpoints for debugging) | **DELETE** | Retrying produces the same failure *and* re-pays for it. This is the most important row: a naive "retry everything" worker would triple the cost of every failed review |
| **Transient** failure: `anthropic.APIError` after SDK retries, `httpx` transport errors, GitHub `5xx` after `call_with_retry`, DB errors, **any unexpected exception** | stays `running`, error logged | **RETRY**, with `ChangeMessageVisibility` → 60 s × attempt (backoff) | It might work next time, and checkpoints mean the retry resumes rather than restarts |
| Transient failure **and** `ApproximateReceiveCount >= 3` | `mark_failed` (`"gave up after 3 attempts"`) | **RETRY** (don't delete) | The row records the final outcome for API callers; SQS moves the message to the DLQ on its next receive, keeping the evidence |
| Worker process dies mid-job | — (nothing runs) | *(implicit)* | The message reappears after the visibility timeout; `claim_for_run` accepts `running` → `running` (attempts + 1); the graph resumes from its checkpoint |

**Unknown exceptions default to *transient*.** The other default ("unknown = deterministic, give up") would turn every novel bug into a permanent failure after one try. Transient-by-default is bounded by `maxReceiveCount` (at most 3 attempts), and checkpoint resume makes attempts 2 and 3 cheap. The error type is recorded either way, so a bug that shows up as a transient-looking failure is visible in the `error` column.

**Why backoff via `ChangeMessageVisibility`:** without it, a transient failure's retry waits the full 15-minute visibility timeout. Shortening visibility on the failed message ("try this again in 60 s, then 120 s") is SQS's native way to schedule a delayed retry, with no scheduler, `sleep`, or held worker slot required.

### Heartbeat: keeping the ticket invisible while cooking

**The problem:** if a review ever runs longer than the 900 s visibility timeout (a huge PR, slow API responses, 429 backoffs), the message reappears and a *second worker* starts the same review on the same checkpoint thread, concurrently. That means two sets of paid calls, and racing checkpoint writes.

**Chosen:** while a job runs, a small background task calls `ChangeMessageVisibility(VisibilityTimeout=900)` every 5 minutes, and it's cancelled when the job finishes. The visibility timeout then means "how long after the worker *stops responding* do we retry," which is a liveness signal, not a guess at the maximum runtime.

**Alternatives:**
- *Set the timeout far above any plausible runtime (e.g. 1 h), no heartbeat:* simple, but a genuinely crashed worker's job then waits an hour to retry.
- *A DB lease (`claimed_by`, `claimed_until` columns, renewed by the worker):* works across any queue technology, but it duplicates what SQS already provides natively.

Day 2's per-specialist timeouts bound the *normal* runtime to a few minutes, so the heartbeat is protection for the abnormal case. It's the first thing to cut if the day runs long (with a comment explaining the risk it leaves).

### Concurrency: two layers, two different jobs

- **Within one review:** Day 2's fan-out (up to 4 specialists), optionally capped by `max_concurrency`.
- **Across reviews:** `worker_concurrency` (default 2). With 4 specialists each, that's at most ~8 concurrent Claude requests per worker process, a deliberate ceiling that respects Anthropic rate limits and the monthly budget.

Scaling out means more worker *containers* (`docker compose up --scale worker=2`). SQS handles distribution between them natively, and the partial unique index + `claim_for_run` + checkpoints keep them from duplicating work.

---

## Part D — `src/queue/sqs.py`

A thin, typed adapter (the same role `github_retry.py` plays for GitHub), so neither the router nor the worker touches raw `aiobotocore` responses:

- `send_review_job(sqs, queue_url, review_id) -> None`
- `receive_jobs(sqs, queue_url, max_messages, wait_seconds) -> list[ReviewJob]`, where `ReviewJob` holds the parsed `review_id` (or `None` if unparseable), the `receipt_handle`, and the `receive_count` (from the `ApproximateReceiveCount` system attribute; confirm on the day whether the current API wants `MessageSystemAttributeNames` rather than the older `AttributeNames` parameter for this).
- `delete_job(sqs, queue_url, receipt_handle)`
- `extend_visibility(sqs, queue_url, receipt_handle, seconds)` (used for both the heartbeat and the backoff)

**Why aiobotocore (recap of the week plan's Decision 7):** native async, and actively maintained (3.9.1, September 2026). `aioboto3` lags (last release October 2025). If aiobotocore's low-level API turns out to be painful, the fallback is `boto3` + `asyncio.to_thread`, which is also perfectly adequate for one long-polling loop.

---

## Part E — One real AWS run

1. Create the queues (AWS CLI, `ap-south-1`): the DLQ first (14-day retention), read its ARN, then the main queue with `VisibilityTimeout=900`, `ReceiveMessageWaitTimeSeconds=20`, and the `RedrivePolicy`.
2. Create an IAM user `codereview-app` with an inline policy scoped to the main queue only:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes"
    ],
    "Resource": "arn:aws:sqs:ap-south-1:<account-id>:codereview-reviews"
  }]
}
```

   **No permissions on the DLQ at all:** SQS moves messages there itself, and inspecting it is a human task done with your own console user. **Stricter still (noted for Week 4):** separate identities for the API (`SendMessage` only) and the worker (receive/delete/visibility only), so a compromised API can't consume or delete jobs.
3. Put the access key in `.env`, set the real `SQS_QUEUE_URL`, **unset `AWS_ENDPOINT_URL`**, and run `api` + `worker`.
4. Run one review end to end. Then check in the SQS console that *Messages sent/received/deleted* moved and the queue is empty. Also do one deliberate poison message (send `{"review_id": "not-a-uuid"}` by hand with the redrive test variant) to see the DLQ path on the real service, or skip that if the decision table deletes malformed bodies. Try the redrive with a transient-failure simulation instead.
5. Record in the day retro: latency from `POST` to `running`, and anything that behaved differently from LocalStack. (Week 2's lesson: the real service is where assumptions break.)
6. Keep the queues for Week 4's deploy, or delete them. Either way, **never commit the keys**, and consider rotating them after the week.

---

## Part F — Closing the week

- `docs/week-3/week-3-day-5.md`: the day retro, with its "Alternatives, Patterns, and Architecture Decisions" section.
- `docs/week-3/engine-comparison.md`: finalized, now including durability as a comparison dimension (the `loop` engine can't resume; the graph engines can).
- `docs/architecture-patterns.md`, **Week 3 section:** the engine registry; context vs state; outcome as data; map-reduce specialists with partial results; commit pinning (`superseded`); claim-check messages; DB-first enqueue (and the outbox alternative); the idempotent consumer decision table; checkpoint resume as the enabler of cheap retries and cheap shutdowns. Plus known gaps (stuck-row reconciliation, failed-checkpoint retention, identity split).
- `docs/week-3/week-3-retrospective.md`: with diagrams (the async flow's sequence diagram; the workflow graph via `draw_mermaid()`; the worker decision table as a flowchart).
- Update the Week 2 memory note's scope if the verbose-comment convention should explicitly continue into Week 4 (it says "from Week 2 forward," so it already does; just confirm).

---

## Files

1. `docker-compose.yml`: `localstack` (+ init script mount), `worker` service (same image, `command: python -m src.worker`, `stop_grace_period: 60s`, also mounts the GitHub App private key read-only, because **the worker mints installation tokens**)
2. `localstack/init/ready.d/create-queues.sh`: NEW
3. `src/config/settings.py`: `sqs_queue_url`, `aws_region`, `aws_endpoint_url`, `worker_concurrency`
4. `src/queue/sqs.py`: NEW
5. `src/worker.py`: NEW (startup, poll loop, `handle_job`, heartbeat, shutdown)
6. `src/routers/reviews.py`: `POST …/reviews` (`202`)
7. `src/main.py`: SQS client in the lifespan (the API only sends)
8. `pyproject.toml`: `aiobotocore`
9. Tests (below)

---

## .NET parallels

- The worker ≈ a **`BackgroundService`** with `ExecuteAsync` running a receive loop, and `IHostApplicationLifetime` / `stoppingToken` for graceful shutdown on `SIGTERM`.
- SQS semantics ≈ **Azure Service Bus peek-lock**: receive ≈ `PeekLock`, `DeleteMessage` ≈ `CompleteMessageAsync`, visibility timeout ≈ lock duration, heartbeat ≈ `RenewMessageLockAsync` (or the processor's `MaxAutoLockRenewalDuration`), `maxReceiveCount` → DLQ ≈ `MaxDeliveryCount` → `$DeadLetterQueue`. Backoff via visibility ≈ `AbandonMessageAsync` plus scheduled redelivery.
- Deterministic vs transient classification ≈ Polly's `Handle<TransientException>()` policies, or MassTransit's retry filters that exclude specific exception types from retry.
- The transactional outbox ≈ **MassTransit's / NServiceBus's outbox** features, the exact same pattern, built in there. Worth saying in an interview: "I chose DB-first + reconciliation for now; at scale I'd use an outbox, like MassTransit's."
- `202 Accepted` + `Location` + polling ≈ the Azure async request-reply pattern / Durable Functions' `CreateCheckStatusResponse`.

---

## Automated verification

- **`handle_job` decision table (unit, fakes for engine/SQS, real Postgres under the integration marker for the claim/mark steps):** one test per row of the table. Success → DELETE + `succeeded`; duplicate (already succeeded) → DELETE, engine never called; superseded → DELETE; deterministic agent failure → DELETE + `failed`; `anthropic.APIError` → RETRY with visibility 60 s × attempt, status still `running`; transient on the 3rd receive → RETRY + `failed`; malformed body → DELETE, engine never called; unknown exception → RETRY (transient default).
- **Resume on redelivery (unit, `InMemorySaver`):** the first `handle_job` crashes after two specialists complete; the second `handle_job` for the same message resumes, and the fake client confirms those two specialists weren't called again.
- **Heartbeat (unit):** with a patched short interval, a long-running fake job triggers `extend_visibility` repeatedly; it's cancelled promptly on completion; no heartbeat after DELETE.
- **Backpressure (unit):** with `worker_concurrency=2` and two in-flight jobs, the loop doesn't call `receive_jobs` until a slot frees.
- **Producer (router):** `202` + `Location`; duplicate active → `202` with the same ID and `created: false`; missing PR → `404` with no row and no message; `SendMessage` failure → `502` + row `failed` + a retry creates a new review.
- **SQS adapter (integration, LocalStack, skipped unless `INTEGRATION_SQS_ENDPOINT` is set):** send → receive → `receive_count == 1` → delete → the queue is empty; with a temporary 1-second-visibility test queue, receiving 3 times without deleting → the message appears in the DLQ.
- **Why not `moto` for SQS tests:** moto intercepts botocore's HTTP layer, and aiobotocore uses its own aiohttp-based transport, which has historically needed moto's *server mode* rather than its in-process mocks. At that point it's another emulator, and LocalStack is already running for development. Check on the day whether that's still true; if moto's in-process mocks work with the current aiobotocore, they'd make the adapter tests runnable without Docker, which is worth having.
- `uv run pytest`, `uv run pytest -m integration`, and `uv run ruff check .` clean.

## Manual verification

```bash
docker compose up --build        # api, worker, postgres, localstack

curl -i -X POST -b cookies.txt \
  "http://localhost:8000/github-app/repos/<owner>/<repo>/pulls/<seeded>/reviews?engine=workflow"
# → HTTP/1.1 202; Location: /github-app/reviews/<id>; returns in well under a second

watch -n 2 'curl -s -b cookies.txt http://localhost:8000/github-app/reviews/<id> | jq .status'
# queued → running → succeeded

# Idempotent POST: repeat the POST while it's running → same review_id, created=false

# Crash + resume: start a large-PR review, then
docker compose kill worker; docker compose up -d worker
# Worker logs, after the visibility timeout (shorten it for this test): claim (attempt 2),
# graph RESUMES, completed specialists are not re-run; final usage < 2× a clean run's.

# DLQ: make the engine raise a transient-classified error 3 times (a debug env flag, dev only)
docker compose exec localstack awslocal sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/codereview-reviews-dlq \
  --attribute-names ApproximateNumberOfMessages          # → 1

# Scale out
docker compose up -d --scale worker=2   # two reviews in parallel, no duplicates
```

---

## End-of-day checklist

- [ ] LocalStack (or ElasticMQ) running with the queue + DLQ + redrive created by the init script
- [ ] `POST …/reviews` returns `202` + `Location`; duplicate POST → same ID; missing PR → `404`
- [ ] DB-first enqueue; `SendMessage` failure handled; the outbox alternative documented
- [ ] Worker: long-poll loop, backpressure, graceful shutdown
- [ ] `handle_job` decision table implemented and every row tested
- [ ] Redelivery after a crash resumes from the checkpoint (tested + seen manually)
- [ ] Heartbeat extends visibility during long jobs (or explicitly cut, with the risk commented)
- [ ] A poison message lands in the DLQ after 3 receives
- [ ] `--scale worker=2` produces no duplicate reviews
- [ ] One real-AWS end-to-end run with the least-privilege IAM user (or explicitly deferred to Week 4 per the scope order)
- [ ] `uv run pytest` / `-m integration` / `ruff` pass
- [ ] Day retro, final engine comparison, `architecture-patterns.md` Week 3 section, and week retrospective written

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **The deterministic vs transient split** is the direct descendant of `github_retry.py` (Week 1), which already refused to retry 4xx while retrying 5xx/429. The same judgment is now applied at the job level, where getting it wrong costs LLM money rather than a few wasted HTTP calls.
- **The claim-check message**, **404-not-403**, **credentials in context not state**, and **no secrets in `Settings` for AWS** are the fourth-through-seventh appearances of the project's core rule: *what travels, or gets persisted, carries a handle, never the secret or the payload.*
- **"Queues are infrastructure, not app startup code"** mirrors Week 1's decision that the GitHub App's registration is a manual, un-automatable step: some setup lives outside the app, deliberately.
- **Backoff via visibility timeout** reuses Week 1's exponential-backoff idea with SQS as the scheduler. It's the same curve and a different clock.
- **Checkpoints making both retries and shutdowns cheap** is the first time one design decision (Day 4) visibly simplifies *two* later ones (Day 5's retry policy and grace period). That's the "decisions that compound" pattern worth pointing out in the week retrospective.
- **Month 3 preview:** the idempotent consumer, DLQ, and at-least-once reasoning here are exactly Month 3's Kafka lessons, learned first on a simpler queue. Kafka adds ordering and replay (an *event log*), but the consumer-side discipline is the same. Month 3's workflow engine should reuse today's decision-table approach for its action handlers, including the doctriage action added to the Month 3 plan.
