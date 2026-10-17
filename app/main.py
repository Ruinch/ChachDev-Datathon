"""Application composition; job behaviour lives behind JobService's interface."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.analyzer import LocalAnalyzer
from app.config import Settings
from app.http_api import api_router
from app.http_guard import ApiGuard, service_error_response
from app.job_service import JobService, ServiceError
from app.storage import JobStore
from app.video import VideoInfo, executable_available, probe_video
from app.worker import JobRunner


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def create_app(
    settings: Settings | None = None,
    analyzer: LocalAnalyzer | None = None,
    *,
    probe: Callable[[Path, str, float, float], VideoInfo] = probe_video,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings.data_dir)
    runner = JobRunner(settings, store, analyzer)
    service = JobService(settings, store, runner, probe=probe, tools_available=executable_available)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with service.lifespan():
            yield

    app = FastAPI(title="Cash Procedure Audit API", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(ApiGuard, settings=settings, service=service)
    app.add_exception_handler(ServiceError, lambda request, error: service_error_response(error))
    app.include_router(api_router(service))
    # Compatibility for local integration tooling; routes use the module above.
    app.state.store = store
    app.state.runner = runner

    @app.get("/docs", include_in_schema=False)
    def api_docs(request: Request):
        root_path = request.scope.get("root_path", "").rstrip("/")
        return get_swagger_ui_html(
            openapi_url=f"{root_path}{app.openapi_url}",
            title=f"{app.title} · API",
            swagger_js_url=f"{root_path}/static/vendor/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url=f"{root_path}/static/vendor/swagger-ui/swagger-ui.css",
            swagger_favicon_url=f"{root_path}/static/favicon.svg",
            swagger_ui_parameters={"validatorUrl": None, "persistAuthorization": False},
        )

    @app.get("/", include_in_schema=False)
    def index():
        index_file = FRONTEND_DIR / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=404, detail="Frontend has not been installed")
        return FileResponse(index_file)

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR, check_dir=False), name="static")
    return app


app = create_app()
