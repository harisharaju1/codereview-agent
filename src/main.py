from fastapi import FastAPI

from src.dependencies.http_client import lifespan
from src.routers import auth, health

# This is the one place the app object itself gets constructed — everything
# else (routers, dependencies, services) is written to be wired together
# here rather than reaching out and registering itself. `lifespan=lifespan`
# is what tells FastAPI to run our custom startup/shutdown logic (opening
# and closing the shared httpx client) around the app's whole lifetime.
app = FastAPI(title="codereview-agent", lifespan=lifespan)

# include_router() mounts every route defined on that router onto this app,
# under whatever prefix the router itself declared (auth.router's routes
# all end up under /auth, e.g. /auth/github/login).
app.include_router(health.router)
app.include_router(auth.router)
