# ---- Builder stage ---------------------------------------------------
# Same base image as the runtime stage, with uv installed via pip rather
# than pulled from a separate uv-maintained base image — one fewer
# external registry this build depends on. This stage exists purely to
# resolve and install dependencies; nothing from it except the resulting
# virtual environment and application code survives into the runtime
# stage below.
FROM python:3.12-slim-bookworm AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copying only the lock/manifest files first, before the rest of the
# source, lets Docker cache this layer: as long as dependencies haven't
# changed, `uv sync` doesn't re-run just because application code changed.
COPY pyproject.toml uv.lock ./
# --frozen: fail the build loudly if uv.lock is out of date, rather than
# silently re-resolving a different dependency set than what's checked in.
# --no-dev: this image should never contain pytest/ruff/respx — those are
# for running the test suite, not for anything that ends up shipped.
RUN uv sync --frozen --no-dev

COPY src ./src

# ---- Runtime stage -----------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# A dedicated, unprivileged user rather than running as root — a
# compromised app process shouldn't also hand over root inside the
# container. Python's slim base images don't default to this the way
# ASP.NET's official images do, so it's worth doing explicitly.
RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
