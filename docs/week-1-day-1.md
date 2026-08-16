# Day 1 — FastAPI Project Scaffolding

## What was built

- **`pyproject.toml`** — a `uv`-managed project (`uv init`), with `fastapi`, `uvicorn[standard]`, and `pydantic-settings` as runtime dependencies, and `pytest`, `pytest-asyncio`, `ruff`, `httpx` as dev dependencies.
- **Folder structure**: `src/routers/`, `src/services/`, `src/schemas/`, `src/dependencies/`, `src/config/` — created up front, with `services/` and `dependencies/` intentionally left empty pending Day 2.
- **`src/config/settings.py`** — a `pydantic-settings` `Settings` class, validated eagerly. `environment` is a `Literal["development", "test", "production"]` with a default, so there's a real, testable case for "invalid config fails fast" even before any required (no-default) fields existed.
- **`src/main.py` + `src/routers/health.py`** — `GET /health` → `{"status": "ok"}`, wired through a router rather than inlined directly in `main.py`.
- **`tests/test_settings.py`, `tests/test_health.py`** — the first tests in the project: settings default/valid/invalid behavior, and the health endpoint returning the expected body.
- **Ruff** configured in `pyproject.toml` for linting and formatting.

## Why it's built this way

- **Routers/services/schemas/dependencies split**: routers stay thin — parse input, call a service, shape a response. Nothing lives in a route handler beyond that. This is what keeps business logic (arriving in later days) testable independent of HTTP entirely.
- **Env validation at startup, not on first use**: a required config value missing should crash the process immediately with a clear error, not fail confusingly the first time some route tries to use it. `Settings` is built once and the process won't serve traffic if it can't construct successfully.
- **Test runner wired up on Day 1, before there's much to test**: every day after this one just adds a test file to an already-working setup, instead of the first real feature also having to double as "first time configuring pytest under time pressure."
- **`services/` and `dependencies/` created empty rather than skipped entirely**: Day 2's GitHub OAuth code has an obvious, already-agreed-upon place to go. (Practically, this also meant adding an `__init__.py` to each — git doesn't track empty directories, so without some file inside them, these folders wouldn't have persisted in the repo at all until Day 2 populated them.)

## Python-specific things worth calling out

- **No `export` keyword, unlike TypeScript.** Every top-level name in a Python module (a function, class, variable) is importable by default — the module boundary is open, not closed. `from src.routers import health` then reaching `health.router` works with zero export ceremony; a single leading underscore (`_name`) is the *convention* for "don't import this," but it's not enforced by the interpreter the way TS's module privacy is.
- **`app.listen()` doesn't exist here because FastAPI doesn't listen for connections at all.** `FastAPI()` only builds an ASGI application object — a callable that knows how to handle one request. A separate program, `uvicorn`, is what actually binds a socket and speaks HTTP; `uv run uvicorn src.main:app` tells uvicorn where to find the app object. This split (framework vs. server) is deliberate — the same `app` could be served by a different ASGI server without any app code changing.
- **`__pycache__/` directories appear next to every module that gets imported** — Python's compiled-bytecode cache, one per directory, regenerated automatically and already covered by `.gitignore`. Nothing to worry about; safe to delete anytime.

## .NET parallel

- `pyproject.toml` + `uv` ≈ a `.csproj` + NuGet, with `uv.lock` playing the role of `packages.lock.json`.
- FastAPI's routers ≈ ASP.NET Core controllers; `app.include_router()` ≈ endpoint routing registration (`MapControllers()`).
- `pydantic-settings` reading env vars into a validated, typed object at startup ≈ `IOptions<T>` bound from `appsettings.json`/environment, validated via `ValidateOnStart()`.
- `pytest` ≈ xUnit/NUnit; `pytest-asyncio` exists because Python's `async def` test functions need explicit event-loop wiring the way `async Task` test methods don't in .NET, where the test framework already understands `Task`-returning tests natively.
- The FastAPI/uvicorn split ≈ the ASP.NET Core middleware pipeline vs. Kestrel underneath it — `dotnet run` just hides that seam more than Python's tooling does.

## Verified manually

- `uv sync` installs cleanly.
- `uv run fastapi dev src/main.py` starts without errors; `curl localhost:8000/health` → `{"status":"ok"}`.
- Constructing `Settings` with an invalid `environment` value fails immediately with a clear Pydantic validation error, not a silent default or a confusing later failure — confirmed directly via `ENVIRONMENT=bogus uv run python -c "from src.config.settings import Settings; Settings()"`.
- `uv run pytest` — all tests pass.
- `uv run ruff check .` and `uv run ruff format --check .` — both clean.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- The routers/services/schemas split, the fail-fast env-validation-at-startup pattern, and "wire up the test runner on day one so later days just add files" are all direct repeats of Day 1 of the previous TypeScript project — same reasoning, translated from Fastify/Zod/vitest to FastAPI/Pydantic/pytest.
- New this time: `uv` itself (filling the same role `pnpm` did, but a different tool), and Pydantic's `BaseModel`/`BaseSettings` doing double duty as both validator and type in a way that's structurally closer to Zod than to a typical C# DTO — worth noticing explicitly, since it's a genuinely good "the pattern generalizes across ecosystems" moment, not just a naming coincidence.
- Nothing GitHub- or LLM-related happens yet, same as the earlier project's own Day 1 — "Day 1 is skeleton only, Day 2 starts the real integration" is being repeated as a deliberate rhythm, not a coincidence of scope.