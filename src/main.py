from fastapi import FastAPI

from src.dependencies.http_client import lifespan
from src.routers import github_app, health

# This is the one place the app object itself gets constructed — everything
# else (routers, dependencies, services) is written to be wired together
# here rather than reaching out and registering itself. `lifespan=lifespan`
# is what tells FastAPI to run our custom startup/shutdown logic (opening
# and closing the shared httpx client) around the app's whole lifetime.
app = FastAPI(title="codereview-agent", lifespan=lifespan)

# include_router() mounts every route defined on that router onto this app,
# under whatever prefix the router itself declared (github_app.router's
# routes all end up under /github-app, e.g. /github-app/install).
app.include_router(health.router)
app.include_router(github_app.router)
