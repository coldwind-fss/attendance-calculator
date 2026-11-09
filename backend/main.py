import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import attendance_sources, event_feed, files, fulltime, hr, jobs, notices, parttime, performance, session
from .config import LEGACY_ATTENDANCE, ROOT, Settings
from .database import Database
from .events import EVENT_CONTRACTS, EventEnvelope
from .http import Problem, fail, problem_responses


def create_app(settings=None):
    settings = settings or Settings.from_environment()
    database = Database(settings.data_dir / "workbench.sqlite")

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        fulltime.initialize(database)
        hr.initialize(database)
        stopping = asyncio.Event()
        handlers = {"parttime": parttime.run_analysis, "fulltime": fulltime.run_fulltime, "hr": hr.sync_organization,
                    "notice": notices.send_notifications}
        work = asyncio.create_task(jobs.worker(database, settings, handlers, stopping))
        receipts = asyncio.create_task(notices.receipt_worker(database, stopping))
        try:
            async with performance.runtime(settings) as compatibility:
                app.state.performance = compatibility
                yield
        finally:
            stopping.set()
            await asyncio.gather(work, receipts)

    app = FastAPI(title="考勤计算系统", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith(("/api/public/", "/attendance-notice/", "/attendance-ack/", "/attendance-review/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def problem(request, status, code, title):
        body = Problem(title=title, status=status, code=code, request_id=request.state.request_id)
        return JSONResponse(body.model_dump(), status_code=status, media_type="application/problem+json")

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        if isinstance(error.detail, dict):
            return problem(request, error.status_code, error.detail["code"], error.detail["title"])
        return problem(request, error.status_code, f"HTTP_{error.status_code}", str(error.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        locations = [".".join(map(str, item["loc"])) for item in error.errors()]
        return problem(request, 422, "VALIDATION_FAILED", "请检查请求字段：" + "、".join(locations))

    validation_problem = problem_responses(422)
    for router in (session.router, files.router, jobs.router, parttime.router, fulltime.router, attendance_sources.router,
                   notices.router, hr.router, performance.router, event_feed.router):
        app.include_router(router, prefix="/api/v1", responses=validation_problem)
    app.include_router(notices.public, prefix="/api/public/v1", responses=validation_problem)
    app.include_router(performance.compat)

    @app.get("/api/v1/event-schema", tags=["events"])
    def event_schema(principal: session.Session = Depends(session.require_session)):
        return {
            "envelope": EventEnvelope.model_json_schema(),
            "event_types": {name: aggregate for name, (aggregate, _) in EVENT_CONTRACTS.items()},
            "payload_schemas": {name: model.model_json_schema() for name, (_, model) in EVENT_CONTRACTS.items()},
        }

    @app.get("/api/v1/health/live", tags=["health"])
    def live():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready", tags=["health"])
    def ready():
        with database.connect() as connection:
            connection.execute("SELECT id FROM jobs LIMIT 1").fetchone()
        if not (LEGACY_ATTENDANCE / "node_modules" / "exceljs").exists():
            fail(503, "PARTTIME_NOT_READY", "兼职计算依赖未安装")
        return {"status": "ready"}

    frontend = ROOT / "frontend" / "dist"
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/attendance-notice/{token}", include_in_schema=False)
    @app.get("/attendance-review/{token}", include_in_schema=False)
    @app.get("/attendance-ack/{token}", include_in_schema=False)
    def index():
        if (frontend / "index.html").is_file():
            return FileResponse(frontend / "index.html")
        fail(404, "FRONTEND_NOT_BUILT", "请先构建 Vue 工作台，或使用 Vite 开发服务")

    return app


app = create_app()
