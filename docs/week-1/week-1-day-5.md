# Day 5 — Docker Compose (Local Only)

## What was built

- **`Dockerfile`** — a multi-stage build. The `builder` stage (`python:3.12-slim-bookworm` + `pip install uv`) copies only `pyproject.toml`/`uv.lock` first, runs `uv sync --frozen --no-dev`, then copies `src/`. The `runtime` stage (a fresh `python:3.12-slim-bookworm`) copies just the resulting `.venv` and `src/` from the builder, creates and switches to a non-root `appuser`, and runs `uvicorn src.main:app --host 0.0.0.0 --port 8000`.
- **`docker-compose.yml`** — a single `api` service, building from the `Dockerfile`, `ports: 8000:8000`, `env_file: .env` (reusing the exact same env file `uv run` already used), and a read-only bind mount of `github-app-private-key.pem` into the container at the path `GITHUB_APP_PRIVATE_KEY_PATH` expects.
- **`.dockerignore`** — excludes `.venv/`, `.git/`, `__pycache__/`, `.env`, `*.pem`, `docs/`, `tests/`, `README.md` from the build context.

## Why it's built this way

- **Multi-stage, so the runtime image never contains build tooling or dev dependencies.** `uv sync --frozen --no-dev` in the builder stage installs only what's declared as a runtime dependency; the `--no-dev` flag is what keeps `pytest`/`ruff`/`respx` out of what actually ships. Confirmed directly: `pytest` is absent from the running container's virtual environment.
- **The private key is bind-mounted, never copied into an image layer.** This mirrors the same secrets discipline `.gitignore`'s `*.pem` rule already established for git — an image is something that could eventually be pushed to a registry, and a key baked into a layer would be retrievable by anyone who can pull that image, the same way a key in git history would be retrievable by anyone with repo access. A bind mount means the container reads the same file living on the host at runtime; the file never becomes part of the built artifact at all, and `.dockerignore` backs this up by excluding `*.pem` from the build context outright, so even an accidental `COPY .` couldn't pull it in.
- **Non-root by default.** Python's slim base images don't run as non-root out of the box the way ASP.NET's official Docker images do — creating and switching to `appuser` explicitly closes that gap, so a compromised app process doesn't also hand over root inside the container.
- **`ghcr.io`'s official `uv` base image was swapped for `pip install uv` on a plain Python base**, deviating from the original plan. `ghcr.io` timed out repeatedly in this environment while `docker.io` pulled fine — rather than fight an unreliable registry, installing `uv` via `pip` on `python:3.12-slim-bookworm` (the same base already used for the runtime stage) gets the same result with one fewer external dependency the build relies on.

## Python-specific things worth calling out

- **`uv sync --frozen`** is the containerized equivalent of Day 1's "fail fast" instinct applied to dependency installation: if `uv.lock` and `pyproject.toml` have drifted out of sync, the build fails loudly right there instead of silently resolving a different dependency set than what's actually checked into the repo.
- **Copying `pyproject.toml`/`uv.lock` before the rest of the source** is a Docker layer-caching technique, not Python-specific, but worth noting: as long as dependencies haven't changed, Docker reuses the cached `uv sync` layer even when application code changes, so most rebuilds during active development skip dependency installation entirely.

## .NET parallel

- The multi-stage build (SDK-equivalent stage → slim runtime stage) is the same shape .NET's own official Docker guidance recommends (`dotnet/sdk` to build, `dotnet/aspnet` to run) — for the identical reason: build tooling has no business in what actually ships.
- The non-root `appuser` setup is explicitly closing a gap that Microsoft's own ASP.NET Docker images already close by default (they run as a non-root user out of the box); Python's ecosystem doesn't standardize this the same way, so it's a deliberate addition here rather than an inherited default.
- A bind-mounted secret, read-only, kept entirely out of the image ≈ the same reasoning behind mounting a Kubernetes `Secret` as a volume into a .NET container rather than baking an `appsettings.Production.json` with real credentials into the image.

## Verified manually

- `docker compose build` succeeds (after the `ghcr.io` → `pip install uv` swap).
- `docker compose up -d` starts cleanly; `curl http://localhost:8000/health` → `{"status":"ok"}` from inside the container.
- `docker compose exec api ls -l /app/github-app-private-key.pem` confirms the bind-mounted key is present and readable at the exact path `GITHUB_APP_PRIVATE_KEY_PATH` expects.
- Confirmed no dev dependencies leaked into the runtime image (`pytest` absent from the container's virtual environment).
- Fail-fast confirmed properly: initially tried overriding `GITHUB_APP_ID=` (empty) via `docker compose run`, which did **not** fail — an empty string still satisfies a required `str` field in Pydantic, it isn't the same as "missing." Correctly testing this meant actually `unset`-ting the variable inside the container (`docker compose run --rm --entrypoint sh api -c "unset GITHUB_APP_ID; .venv/bin/python -c '...'"`), which produced the expected `ValidationError: Field required`.
- `/github-app/install` redirects to the real App's install URL (built from `GITHUB_APP_SLUG`) through the container; unauthenticated `/github-app/installations/current` still returns `401` — both matching the exact behavior already proven outside Docker.
- `uv run pytest` (24 tests) and `uv run ruff check .` both clean — Day 5 added no application code, so no new test coverage was needed.

**Not exercised:** the full browser install → callback → `/installations/current` round-trip against the containerized app specifically (as opposed to `uv run`) — the redirect URL and the 401 behavior were confirmed instead, which cover the parts that could plausibly differ between the two ways of running the app (env loading, networking); the OAuth-adjacent GitHub-side UI interaction itself doesn't change based on how the server is hosted.

---

## Learning Notes: Similarities to Prior Work

Private cross-reference only — doesn't affect anything above.

- **Multi-stage build, layer-cache-friendly dependency-file-first copy order, non-root runtime user** are all direct repeats of the same Day 5 pattern from the earlier TypeScript project — same reasoning, different base images and package manager.
- **Bind-mounting a secret instead of baking it into the image** is a new, concrete instance of a lesson this project already learned the hard way once (the `.gitignore` `.pem` pattern bug from the GitHub App migration) — the same underlying principle now deliberately applied to a second distribution mechanism (container images) before a mistake happened there too, rather than after.
- **Registry unavailability forcing a build-time swap (`ghcr.io` → `pip install uv`)** is a new kind of friction for this project — the earlier project's infra day didn't hit an equivalent external-dependency outage. Worth remembering as a reminder that a Dockerfile's `FROM` line is itself an external dependency with its own availability characteristics, same category of risk as any other third-party service this project relies on.
- **Deliberately deferring real deploy** (this doc covers local-only) rather than matching the earlier project's Day 5, which did deploy for real, continues to be the right call, not a shortcut — that project already had a server and reverse proxy in place from earlier work; this one doesn't yet, and there's no real trigger requiring one today.
