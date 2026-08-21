"""
Ambivo Content Portal — FastAPI application
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import connect_db, close_db
from app.routes import health, presentations, public, chat, auth_routes, ai_generate

SCANNER_PATTERNS = (
    ".env",
    "phpinfo",
    ".php",
    "/.git",
    "wp-admin",
    "wp-login",
    "wp-content",
    "wp-includes",
    "/administrator/",
    "/_profiler",
    "server-status",
    "server-info",
)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class _ScannerAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage().lower()
        except Exception:
            return True
        return not any(s in msg for s in SCANNER_PATTERNS)


logging.getLogger("uvicorn.access").addFilter(_ScannerAccessLogFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    await connect_db()
    yield
    await close_db()
    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Markdown content hosting with per-page KB chat",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def block_scanners(request: Request, call_next):
    path = request.url.path.lower()
    if any(s in path for s in SCANNER_PATTERNS):
        return Response(status_code=404)
    return await call_next(request)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(health.router)
app.include_router(auth_routes.router)
app.include_router(presentations.router)
app.include_router(public.router)
app.include_router(chat.router)
app.include_router(ai_generate.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.debug)
