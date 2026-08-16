from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request


# @asynccontextmanager turns an async generator function (one that `yield`s
# exactly once) into something usable with `async with`. Everything before
# the `yield` runs on entry ("setup"); everything after it runs on exit
# ("teardown") — including when exiting because of an exception. FastAPI
# specifically looks for a callable shaped like this when you pass
# `lifespan=` to FastAPI(): it runs the "before yield" part once when the
# app starts, and the "after yield" part once when the app shuts down.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # `async with` is the async version of a `with` block — it's Python's
    # way of saying "set this resource up, and guarantee it gets cleaned up
    # (even on error) when this block ends." Here: open one httpx.AsyncClient
    # when the app starts, and let httpx close its underlying connections
    # cleanly when the app shuts down (when this `async with` block exits).
    async with httpx.AsyncClient(timeout=10.0) as client:
        # FastAPI (via Starlette) gives every app an `app.state` object —
        # a plain namespace for stashing app-wide objects that need to
        # survive across requests. Storing the client here, once, is what
        # lets every request reuse the same client (and its connection
        # pool) instead of each request paying for a fresh TCP+TLS
        # handshake to GitHub.
        app.state.http_client = client
        # `yield` with nothing after it (no value) is what makes this a
        # "setup/teardown" context manager rather than one that hands a
        # value to the caller — FastAPI just needs the pause-here signal,
        # not a returned object.
        yield


def get_http_client(request: Request) -> httpx.AsyncClient:
    # A FastAPI dependency is just a plain function; declaring a route
    # parameter as `Depends(get_http_client)` tells FastAPI to call this
    # function and pass its return value in. `request.app` reaches back to
    # the same FastAPI app instance whose `.state.http_client` the lifespan
    # function above set — so every route gets the one shared client.
    return request.app.state.http_client
