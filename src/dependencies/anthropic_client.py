from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, Request

from src.config.settings import get_settings


# Summary: opens one shared anthropic.AsyncAnthropic client when the app
# starts and closes it when the app stops. Exists for exactly the same
# reason dependencies/http_client.py's lifespan() does — one shared client
# (and its underlying connection pool) reused across every request, rather
# than a fresh client constructed per call.
#
# WHY THIS IS ITS OWN LIFESPAN FUNCTION, IN ITS OWN FILE, RATHER THAN
# FOLDED INTO http_client.py's lifespan():
# FastAPI's FastAPI(lifespan=...) only accepts one callable, but a
# "lifespan" is really just an async context manager — nothing about
# FastAPI requires all of an app's startup/shutdown logic to live in one
# function. Keeping each resource's setup/teardown in the module that owns
# that resource (this file owns the Anthropic client the same way
# http_client.py owns the httpx client) means: neither module needs to
# import or know about the other, each can be tested/reasoned about in
# isolation, and adding a THIRD shared resource later (a Redis client, say)
# means adding a third small lifespan function, not editing an
# ever-growing shared one. main.py is where these get composed together
# into the single lifespan FastAPI actually receives — see main.py's own
# `lifespan` function for how.
#
# THE ALTERNATIVE CONSIDERED AND REJECTED: constructing an
# anthropic.AsyncAnthropic() directly inside review_agent.py (Day 4),
# right where it's used, with no Depends()-based indirection at all. That
# would be less code today, but it would remove the one seam that makes
# "let a caller supply their own API key, base URL, or model" (a real
# product question already discussed, not being built now) a one-function
# change later, rather than a refactor of every place that currently
# constructs a client. Paying that small structural cost now, while it's
# cheap, is the whole reason this file exists as its own dependency module
# instead of being inlined where it's first needed.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    async with anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key) as client:
        app.state.anthropic_client = client
        yield


# Summary: hands a caller the one shared Anthropic client created by
# lifespan() above. Exists as the Depends()-injectable seam every piece of
# this project that needs to call Claude goes through — mirroring
# get_http_client's role in dependencies/http_client.py exactly. Nothing
# in this project should ever write `anthropic.AsyncAnthropic(...)`
# directly outside this one function; every other call site should reach
# the client through this dependency instead, the same discipline already
# applied to the shared httpx client.
def get_anthropic_client(request: Request) -> anthropic.AsyncAnthropic:
    return request.app.state.anthropic_client
