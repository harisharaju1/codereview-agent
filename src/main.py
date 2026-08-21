from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.dependencies import anthropic_client, http_client
from src.routers import github_app, health, pull_requests


# Summary: composes this app's separate resource lifespans (the shared
# httpx client, the shared Anthropic client) into the single lifespan
# callable FastAPI's constructor accepts. Exists because FastAPI only
# takes one `lifespan=` argument, but each resource's own setup/teardown
# logic deliberately stays in the module that owns that resource (see
# dependencies/http_client.py and dependencies/anthropic_client.py)
# instead of being merged into one big function here.
#
# HOW THIS ACTUALLY WORKS: both http_client.lifespan and
# anthropic_client.lifespan are async context managers (functions decorated
# with @asynccontextmanager). `async with a(), b():` is ordinary Python
# syntax for entering two context managers in one statement — it's exactly
# equivalent to nesting `async with a():` then `async with b():` inside it,
# just written flatter. Each one's "before yield" code runs (in order) when
# this composed lifespan starts, and each one's "after yield" cleanup code
# runs (in *reverse* order) when it ends — the same guarantee a single
# context manager gives, just for two resources instead of one. Adding a
# third shared resource later means adding a third entry to this one line,
# not restructuring anything.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with http_client.lifespan(app), anthropic_client.lifespan(app):
        yield


# This is the one place the app object itself gets constructed — everything
# else (routers, dependencies, services) is written to be wired together
# here rather than reaching out and registering itself. `lifespan=lifespan`
# is what tells FastAPI to run the composed startup/shutdown logic above
# (opening and closing both shared clients) around the app's whole lifetime.
app = FastAPI(title="codereview-agent", lifespan=lifespan)

# include_router() mounts every route defined on that router onto this app,
# under whatever prefix the router itself declared (github_app.router's
# routes all end up under /github-app, e.g. /github-app/install).
app.include_router(health.router)
app.include_router(github_app.router)
app.include_router(pull_requests.router)
