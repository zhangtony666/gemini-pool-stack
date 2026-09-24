from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .backends import Backend, create_backend
from .chat import router as chat_router
from .config import Settings
from .images import validate_image
from .models import ServiceError
from .store import Store
from .worker import WorkerPool


class BodyLimit:
    """Enforce the total body limit even for chunked multipart uploads."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        count = 0

        async def limited_receive():
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > self.limit:
                raise HTTPException(status_code=413, detail="body_too_large")
            return message

        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"error": "invalid_content_length"}, status_code=400)(
                scope,
                receive,
                send,
            )
        if length > self.limit:
            return await JSONResponse({"error": "body_too_large"}, status_code=413)(
                scope,
                receive,
                send,
            )
        return await self.app(scope, limited_receive, send)


class ResourceState(BaseModel):
    state: Literal["ready", "paused"]
    reason: str = Field(min_length=1, max_length=200)


def create_app(
    settings: Settings | None = None, *, backend: Backend | None = None, start_workers: bool = True
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.initialize)
        pool = WorkerPool(store, backend or create_backend(settings))
        app.state.pool = pool
        if start_workers:
            await pool.start()
        try:
            yield
        finally:
            await pool.stop()

    app = FastAPI(title="图片真实性核验服务", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.settings = settings
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        headers = {"Retry-After": "5"} if exc.status == 429 else None
        return JSONResponse(
            {"error": exc.code, "message": exc.message}, status_code=exc.status, headers=headers
        )

    def auth(authorization: Annotated[str | None, Header()] = None):
        if settings.api_key and not secrets.compare_digest(
            authorization or "",
            f"Bearer {settings.api_key}",
        ):
            raise ServiceError("unauthorized", "需要有效的业务访问令牌", 401)

    def admin_auth(authorization: Annotated[str | None, Header()] = None):
        if not settings.admin_key:
            raise ServiceError("admin_disabled", "设置 IV_ADMIN_KEY 后启用管理接口", 403)
        if not secrets.compare_digest(authorization or "", f"Bearer {settings.admin_key}"):
            raise ServiceError("unauthorized", "需要有效的管理访问令牌", 401)

    @app.get("/health/live", tags=["health"])
    async def live():
        return {
            "status": "ok",
            "backend": settings.backend,
            "simulated": settings.backend == "mock",
        }

    @app.get("/health/ready", tags=["health"])
    async def ready():
        try:
            snapshot = await asyncio.to_thread(store.snapshot)
            resource = next(r for r in snapshot["resources"] if r["id"] == settings.resource_id)
            seen = snapshot["last_worker_seen"]
            heartbeat_limit = max(60, settings.attempt_timeout * 2, settings.lease_seconds * 2)
            status = (
                resource["state"] == "ready"
                and seen is not None
                and time.time() - seen < heartbeat_limit
            )
        except Exception:
            status = False
        return JSONResponse({"ready": status}, status_code=200 if status else 503)

    @app.post("/v1/batches", status_code=202, dependencies=[Depends(auth)], tags=["tasks"])
    async def submit(
        files: Annotated[list[UploadFile], File()],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
        deadline_seconds: Annotated[int, Form(ge=1, le=86400)] = 300,
    ):
        try:
            if not 1 <= len(files) <= settings.max_batch:
                raise ServiceError("batch_size", f"每批需1至{settings.max_batch}张图片", 413)
            images = []
            for file in files:
                content = await file.read(settings.max_image_bytes + 1)
                images.append(
                    await asyncio.to_thread(
                        validate_image,
                        content,
                        file.filename or "image",
                        settings,
                    )
                )
            batch_id, replayed = await asyncio.to_thread(
                store.submit,
                images,
                idempotency_key,
                deadline_seconds,
            )
            batch = await asyncio.to_thread(store.get_batch, batch_id)
            return {
                "batch_id": batch_id,
                "replayed": replayed,
                "task_ids": [t["id"] for t in batch["tasks"]],
                "status_url": f"/v1/batches/{batch_id}",
            }
        finally:
            for file in files:
                await file.close()

    @app.get("/v1/batches/{batch_id}", dependencies=[Depends(auth)], tags=["tasks"])
    async def batch_status(batch_id: str):
        return await asyncio.to_thread(store.get_batch, batch_id)

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(auth)], tags=["tasks"])
    async def task_status(task_id: str):
        return await asyncio.to_thread(store.get_task, task_id)

    @app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(auth)], tags=["tasks"])
    async def cancel(task_id: str):
        await asyncio.to_thread(store.cancel, task_id)
        return await asyncio.to_thread(store.get_task, task_id)

    @app.get("/v1/metrics", dependencies=[Depends(auth)], tags=["operations"])
    async def metrics():
        return await asyncio.to_thread(store.snapshot)

    @app.put("/admin/resource", dependencies=[Depends(admin_auth)], tags=["operations"])
    async def resource_state(body: ResourceState):
        await asyncio.to_thread(store.set_resource_state, body.state, body.reason)
        return {"resource": settings.resource_id, "state": body.state}

    app.include_router(chat_router(store, settings, auth))
    return app
