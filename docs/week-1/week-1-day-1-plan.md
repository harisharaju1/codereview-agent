# Day 1 Plan — FastAPI Project Scaffolding

## What we're doing and why

This repo is currently empty except for `docs/`. Day 1 stands up the actual Python project: a `uv`-managed FastAPI app with a real folder structure, env validation that fails fast at startup, a working test runner, and linting — so every day after this one is "add a file in the right place," not "first figure out where things go."

Nothing here talks to GitHub or an LLM yet. The only functional endpoint today is `GET /health`. The point of the day is the skeleton being right, not features.

---

## File 1 — Project init: `pyproject.toml` + `uv`

**What it is:** `uv init` creates a `pyproject.toml` (dependency manifest + project metadata) and a virtual environment `uv` manages for you — the Python analogue of `pnpm init` + a managed `node_modules`.

**Dependencies added:**
- `fastapi` — the web framework
- `uvicorn[standard]` — the ASGI server that actually runs a FastAPI app (FastAPI defines routes; uvicorn is what listens on a port and serves them — comparable to how ASP.NET Core's `Program.cs` app depends on Kestrel underneath it)
- `pydantic-settings` — env var loading + validation
- `pytest`, `pytest-asyncio` — test runner + async test support (dev dependency)
- `ruff` — linter + formatter in one tool (dev dependency)

**Why `uv` over Poetry or plain `pip`+`venv`:** one tool for venv creation, dependency locking (`uv.lock`), and running scripts (`uv run ...`), and it's fast. Functionally it's doing the same job `pnpm` did last time — resolve dependencies, lock versions, give you a `run` command that uses the project's own environment rather than whatever's globally installed.

---

## File 2 — Folder structure

```
src/
  routers/        # FastAPI routers — thin: parse input, call a service, shape the response
  services/       # business logic (empty today — first tenant is GitHub OAuth, Day 2)
  schemas/        # Pydantic models — request/response shapes
  dependencies/   # FastAPI dependency-injection providers (settings, later: HTTP client)
  config/
    settings.py   # the Pydantic-settings class + a cached getter
  main.py         # creates the FastAPI() app, includes routers
```

`services/` and `dependencies/` are created empty (with a `.gitkeep` or just left for Day 2 to populate) — the point of creating the structure now is that Day 2's GitHub OAuth code has an obvious place to go, not that it's used today.

**Why this split:** routers stay thin — no business logic lives in a route handler beyond "validate input, call a service function, return its result." Services hold the actual logic and are the layer that gets unit-tested without needing to spin up HTTP at all. Schemas are the single source of truth for a shape — both the runtime validation *and* the Python type, via Pydantic's `BaseModel` (this is precisely the job Zod's `z.infer` does — a schema you write once gives you both a type checker and a runtime validator, instead of maintaining a TypeScript interface and a separate validation schema by hand).

---

## File 3 — `src/config/settings.py` — env validation at startup

**What it is:** A Pydantic `BaseSettings` subclass that reads env vars (from process env or a `.env` file), validates their types/presence, and raises immediately if something required is missing.

```python
class Settings(BaseSettings):
    environment: str = "development"
    # Day 2 will add github_client_id / github_client_secret here —
    # left out today since nothing reads them yet, and an unused
    # required field would just be dead configuration
```

**Why validate at startup, not on first use:** a service that crashes on boot with "field required: github_client_id" is far easier to diagnose than one that starts fine and then throws a confusing error the first time someone hits `/auth/github/login`. This mirrors the fail-fast env pattern from an earlier project (Zod-validated env there) — same idea, Pydantic instead of Zod.

**A note on why `github_client_id` isn't stubbed in today:** it would be a required field with nothing consuming it — dead configuration that looks load-bearing but isn't. Day 2 adds it exactly when the OAuth code that needs it lands, so `settings.py`'s field list always reflects what the app actually uses right now.

---

## File 4 — `src/main.py` + `src/routers/health.py`

**What it is:** `main.py` constructs the `FastAPI()` app instance and registers routers. `routers/health.py` defines `GET /health` → `{"status": "ok"}`.

**Why a separate router file for one endpoint:** it's a small amount of ceremony for one route today, but it establishes the pattern — every future router (auth, PRs, diffs) plugs into `main.py` the same way, via `app.include_router(...)`, rather than routes accumulating directly in `main.py` as the project grows.

---

## File 5 — Tests: `pytest` wiring

**What it is:** `tests/test_settings.py` (or colocated under `src/`, whichever `pytest`'s discovery picks up cleanly with this layout) with one real test: constructing `Settings` with valid env values succeeds; a required field being absent raises a validation error.

**Why one trivial-seeming test matters:** it proves the whole chain — `uv run pytest` actually discovers and runs tests, async test support (if needed) is wired correctly, and the settings validation logic genuinely does what the previous section claims. Every later day just adds a file to an already-working test setup instead of debugging test infra for the first time under time pressure.

---

## File 6 — Ruff config

**What it is:** A `[tool.ruff]` section in `pyproject.toml` (or a separate `ruff.toml`) enabling a reasonable default lint rule set plus formatting.

**Why now:** same reasoning as setting up ESLint+Prettier on day one of a TypeScript project — catching style/lint issues from the first commit is cheaper than retrofitting a linter onto a codebase that's already inconsistent.

---

## .NET parallels (for orientation, not because the code is equivalent)

- `pyproject.toml` + `uv` ≈ a `.csproj` + NuGet, with `uv.lock` playing the role of `packages.lock.json`.
- FastAPI's routers ≈ ASP.NET Core controllers; `app.include_router()` ≈ `MapControllers()` / endpoint routing registration.
- Pydantic `BaseModel` ≈ a C# record/DTO with data-annotation validation attributes, except the same class is both the validator and the type — closer to how a Zod schema behaves than to a plain C# DTO, which usually needs separate validation attributes bolted on.
- `pydantic-settings` reading env vars into a typed, validated object at startup ≈ `IOptions<T>` bound from `appsettings.json`/environment variables in `Program.cs`, validated via data annotations or `ValidateOnStart()`.
- `pytest` ≈ xUnit/NUnit; `pytest-asyncio` exists because Python's `async def` test functions need explicit event-loop wiring the way `async Task` test methods don't in .NET (the test framework there already understands `Task`-returning tests natively).

---

## Local verification steps

Run these in order after implementation:

```bash
# 1. Install dependencies into a uv-managed venv
uv sync

# 2. Run the app
uv run fastapi dev src/main.py
# or: uv run uvicorn src.main:app --reload

# 3. In a separate terminal — hit the health endpoint
curl localhost:8000/health
# Expected: {"status":"ok"}

# 4. Run the test suite
uv run pytest
# Expected: settings validation tests pass

# 5. Run the linter
uv run ruff check .
uv run ruff format --check .
```

---

## End-of-day checklist

- [ ] `uv sync` installs cleanly
- [ ] `uv run fastapi dev src/main.py` starts without errors
- [ ] `curl localhost:8000/health` → `{"status":"ok"}`
- [ ] Removing a required env var (simulate: unset anything `Settings` requires) causes a clear startup failure, not a silent default or a confusing runtime error
- [ ] `uv run pytest` passes
- [ ] `uv run ruff check .` passes with no errors

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- The routers/services/schemas split, the fail-fast env-validation-at-startup pattern, and "wire up the test runner on day one so later days just add files" are all direct repeats of Day 1 of the previous TypeScript project — same reasoning, translated from Fastify/Zod/vitest to FastAPI/Pydantic/pytest.
- New this time: `uv` itself (a different tremendous-speed-focused tool than `pnpm`, though filling the identical role), and the fact that Pydantic's `BaseModel` does double duty as both validator and type in a way that's structurally closer to Zod than to a typical C# DTO — worth noticing explicitly since it's a genuinely good "the pattern generalizes across ecosystems" moment, not just a naming coincidence.
- Nothing GitHub- or LLM-related happens yet, unlike the earlier project's Day 1 which was also purely scaffolding — the shape of "Day 1 is skeleton only, Day 2 starts the real integration" is intentionally being repeated as a rhythm, not just a coincidence of scope.
