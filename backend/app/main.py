import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import current_user
from .config import API_DOCS, FRONTEND_ORIGINS
from .db import init_db
from .routers import auth, companies, emails, export, jobs, lists, people, usage, users
from .worker import start_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines can contain API keys


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Tests set LEADGEN_DISABLE_WORKER=1 so no background thread picks up their jobs.
    stop = None if os.getenv("LEADGEN_DISABLE_WORKER") == "1" else start_worker()
    yield
    if stop is not None:
        stop.set()


app = FastAPI(
    title="Lead Generation API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if API_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if API_DOCS else None,
)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The dashboard sends this header with every request. Another website can't add it to a request
# made with your login cookie (the browser would first ask us, and CORS says no), so requiring
# it on every change blocks cross-site request forgery.
CSRF_HEADER, CSRF_VALUE = "x-requested-with", "leadgen"


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method not in SAFE_METHODS and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE or (origin and origin not in FRONTEND_ORIGINS):
            return JSONResponse({"detail": "Request blocked: it didn't come from the dashboard."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        # Lead data must not stay in shared or browser caches.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Requested-With"],
)

# Public: logging in. Everything else needs a logged-in user.
app.include_router(auth.router)
app.include_router(users.router)  # admins only (checked per endpoint)
logged_in = [Depends(current_user)]
for module in (jobs, companies, usage, people, lists, export, emails):
    app.include_router(module.router, dependencies=logged_in)


@app.get("/api/health")
def health():
    return {"status": "ok"}
