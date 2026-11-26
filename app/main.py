"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import router as api_router
from app.core.config import Settings, get_settings
from app.core.localization import normalize_lang, t
from app.core.logging_config import setup_logging
from app.core.runtime_settings import runtime
from app.dashboard import router as dashboard_router
from app.dashboard.deps import is_htmx
from app.database.database import dispose_engine, init_engine, session_scope
from app.database.models import Admin
from app.services.container import Services, build_services

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "dashboard" / "static"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app(settings: Settings | None = None, services: Services | None = None, *, start_background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_dir, runtime.get("log_level"))
        init_engine(settings.async_database_url)
        svc = services or build_services(settings)
        app.state.services = svc
        async with session_scope() as s:
            await runtime.load(s)
        await svc.providers.load()
        if start_background:
            await svc.tasks.start()
            await svc.runner.start()
            await svc.maintenance.start()
        async with session_scope() as s:
            if (await s.execute(select(Admin.id).limit(1))).first() is None:
                log.warning("No administrator exists. Run: python -m app.cli create-admin")
        log.info("ScholarBot started on http://%s:%s", settings.dashboard_host, settings.dashboard_port)
        try:
            yield
        finally:
            await svc.maintenance.stop()
            await svc.runner.stop()
            await svc.tasks.stop()
            await svc.providers.aclose()
            await dispose_engine()

    app = FastAPI(title="ScholarBot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    if not settings.secret_key:
        raise RuntimeError("SECRET_KEY is empty. Run the start script (it creates one) or set SECRET_KEY in .env")
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="scholarbot_session",
                       same_site="lax", https_only=settings.cookie_secure, max_age=60 * 60 * 12)
    if settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in settings.trusted_hosts.split(",") if h.strip()])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/static"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code in (301, 302, 303, 307) or (exc.headers and "HX-Redirect" in exc.headers):
            if exc.headers and "Location" in exc.headers:
                return RedirectResponse(exc.headers["Location"], 303)
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        wants_json = request.url.path.startswith("/api") or request.headers.get("accept", "").startswith("application/json") \
            or request.headers.get("x-csrf-token") is not None
        if wants_json or is_htmx(request):
            return JSONResponse({"ok": False, "message": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)
        lang = normalize_lang(request.scope.get("session", {}).get("lang"))
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{exc.status_code}</title>"
                            f"<body style='font-family:sans-serif;padding:3rem'><h1>{exc.status_code}</h1>"
                            f"<p>{t(lang, 'err_http_' + str(exc.status_code)) if exc.status_code in (403, 404) else exc.detail}</p>"
                            f"<p><a href='/'>{t(lang, 'nav_dashboard')}</a></p>", status_code=exc.status_code)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(api_router)
    app.include_router(dashboard_router)
    return app
