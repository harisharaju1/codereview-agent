from fastapi import APIRouter

router = APIRouter()


# Summary: reports that the process is up and able to serve requests.
# Exists as a cheap liveness check for local dev, container health checks,
# and any future deploy target's load balancer/orchestrator to poll.
#
# The `@router.get("/health")` decorator registers this function as the
# handler for GET /health. A decorator is just syntax sugar for
# `health = router.get("/health")(health)` — it wraps/registers the
# function immediately below it, rather than being a special language
# keyword.
@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
