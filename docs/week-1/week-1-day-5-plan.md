# Day 5 Plan — Docker Compose (Local Only)

## What we're doing and why

The service has run exclusively via `uv run fastapi dev` / `uv run uvicorn` so far — fine for development, but nothing about this project has ever been packaged the way it'd actually run anywhere else. Day 5 containerizes it: a multi-stage `Dockerfile`, a `docker-compose.yml`, and confirmation that `/health` responds from inside a container, not just from `uv run` on the host machine.

**Scope is deliberately local-only today.** A real remote deploy isn't needed yet — this project is still single-operator, and nothing currently requires this service to be reachable from outside `localhost` (the GitHub App's Setup URL already points at `localhost:8000`, and that's correct for now). Real deploy becomes necessary at one of two later points: flipping the GitHub App to "Any account" installs (which needs a real public Setup URL), or Week 4's MCP server work (which explicitly needs a public subdomain for external MCP clients). Building the container image now, without yet needing somewhere to run it in production, means that step is ready when either trigger actually arrives instead of being a rushed addition later.

---

## File 1 — `Dockerfile` — multi-stage build

**Stage 1 (`builder`)**: based on a `uv`-provided Python image (e.g. `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`), copies `pyproject.toml` + `uv.lock`, runs `uv sync --frozen --no-dev` to install only runtime dependencies into a virtual environment — `--frozen` so the build fails loudly if `uv.lock` is out of date rather than silently re-resolving, `--no-dev` so `pytest`/`ruff`/`respx` never end up in the image that actually runs.

**Stage 2 (`runtime`)**: a slim Python base image, copies only the built virtual environment and the `src/` application code from the builder stage — none of `uv`'s own tooling, build caches, or dev dependencies. Runs as a non-root user (a concrete, cheap security improvement: a container compromised via an app-level bug shouldn't also hand the attacker root inside the container). Entrypoint: `uvicorn src.main:app --host 0.0.0.0 --port 8000`.

**Why multi-stage at all, given this could be a single-stage image:** the builder stage needs `uv` itself and pulls in build-time state (the dependency resolution cache) that has no business existing in what actually runs — a smaller final image is both faster to pull and has a smaller attack surface (nothing in it can be used to rebuild or introspect the source dependency graph that isn't already needed at runtime).

---

## File 2 — `docker-compose.yml`

A single service (`api`), building from the `Dockerfile` above, with:
- `ports: ["8000:8000"]`
- `env_file: .env` — reusing the exact same `.env` this project already uses for `uv run`, rather than inventing a separate compose-specific config mechanism
- A bind mount for `github-app-private-key.pem` (or wherever `GITHUB_APP_PRIVATE_KEY_PATH` points), read-only — the key needs to be reachable *inside* the container at the path `Settings` expects, without ever being baked into the image itself (an image is something you might eventually push to a registry; a private key baked into a layer would leak the moment that happens)

**Why the private key is mounted, not copied into the image:** this is the same "secrets never get committed/baked in" discipline already established for `.gitignore` (Week 1's real security bug fix) — a `.pem` in an image layer is functionally almost as bad as one in git history, since anyone who can pull the image can extract it.

---

## File 3 — `.dockerignore`

Excludes `.venv/`, `.git/`, `__pycache__/`, `.env`, `*.pem`, `docs/`, and test files from the build context — mirrors `.gitignore`'s reasoning (don't let secrets or irrelevant bulk end up somewhere they could leak or just slow the build down), applied to Docker's build context instead of git's tracked history.

---

## No changes needed to `.env.example`

Already accurate as of Day 2's GitHub App migration — nothing about containerizing the app changes what configuration it needs, only how that configuration gets supplied to the running process.

---

## .NET parallels

- A multi-stage Dockerfile (SDK-based build stage → slim ASP.NET runtime image) is the exact same pattern .NET's own official Docker guidance recommends — `mcr.microsoft.com/dotnet/sdk` to build, `mcr.microsoft.com/dotnet/aspnet` to run, for the identical "don't ship build tooling in the runtime image" reason.
- `docker-compose.yml`'s `env_file: .env` ≈ `appsettings.json` + environment-variable overrides being read by `IConfiguration` at container startup — same idea, same "don't bake config into the image" discipline.
- Running as a non-root user inside the container ≈ the same recommendation Microsoft's own ASP.NET Docker images already follow by default (they run as a non-root `app` user out of the box) — worth doing explicitly here since Python's slim base images don't default to that.

---

## Automated verification

Nothing new to unit-test here — Day 5 doesn't add application logic, so there's no new `pytest` coverage. Verification is entirely "does the container actually run and serve traffic correctly."

## Manual verification

```bash
# 1. Build and start the container
docker compose up --build

# 2. Confirm the health endpoint responds from inside the container
curl http://localhost:8000/health
# Expect: {"status": "ok"}

# 3. Confirm the app fails fast (not silently) with a clear error if a
#    required env var is missing — matches the fail-fast behavior already
#    proven for `uv run` in Day 1/2
docker compose run --rm -e GITHUB_APP_ID= api
# Expect: a clear Settings validation error on startup, not a silent crash
#    or a container that starts but can't actually serve requests

# 4. Confirm the private key is reachable at the mounted path inside the
#    container, not just present on the host
docker compose exec api ls -l $GITHUB_APP_PRIVATE_KEY_PATH

# 5. Confirm the full GitHub App flow still works end-to-end against the
#    containerized app, not just /health:
open http://localhost:8000/github-app/install
# ... complete the install, confirm /github-app/installations/current
#    returns real data through the container the same way it did running
#    directly via uv run
```

---

## End-of-day checklist

- [ ] `docker compose up --build` succeeds and serves `/health`
- [ ] The runtime image contains no dev dependencies (`pytest`, `ruff`, `respx` — confirm via `docker compose exec api uv pip list` or equivalent, showing only runtime deps)
- [ ] A missing required env var fails the container's startup with a clear error, not a silent or confusing failure
- [ ] The private key is reachable inside the container via a mount, never baked into an image layer
- [ ] The full GitHub App install → callback → `/installations/current` flow works against the containerized app
- [ ] `.dockerignore` keeps `.env`/`*.pem`/`.git`/`.venv` out of the build context

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Multi-stage build (SDK/build tooling stage → slim runtime stage)** is a direct repeat of the same pattern from the earlier TypeScript project's own Day 5 — same reasoning (smaller image, smaller attack surface), same shape, different base images.
- **Mounting a secret rather than baking it into the image** is a new, explicit instance of the exact security discipline this project already learned the hard way once, when `.gitignore`'s `.pem` pattern was found not to actually ignore the private key file — the same underlying principle ("a secret that ends up in something distributable/shareable is a leak, whether it's a git commit or an image layer") now applied to a second, different distribution mechanism.
- **Deliberately deferring real deploy to a concrete future trigger** (the Any-account flip, or Week 4's MCP work) rather than doing it now "since we're already in the infra day" is a genuinely new decision for this project — the earlier project's Day 5 did deploy for real, since it already had a server and reverse proxy in place from earlier work; this project doesn't yet, and manufacturing a deploy target just to match the earlier project's rhythm would be scope creep, not consistency.