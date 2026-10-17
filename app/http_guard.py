"""Protect the HTTP stream before FastAPI's multipart parser consumes it."""
from __future__ import annotations

import asyncio
import hmac
import time

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.job_service import JobService, ServiceError


class ApiGuard:
    def __init__(self, app: ASGIApp, settings: Settings, service: JobService) -> None:
        self.app, self.settings, self.service = app, settings, service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")])
                message = {**message, "headers": headers}
            await send(message)

        headers = dict(scope["headers"])
        given = headers.get(b"x-api-key", b"")
        if not self.settings.api_key or not hmac.compare_digest(given, self.settings.api_key.encode()):
            await JSONResponse(status_code=401, content={"detail": {
                "code": "invalid_api_key", "message": "Invalid API key",
            }})(scope, receive, private_send)
            return

        if scope["method"] != "POST" or scope["path"] != "/api/v1/analyze":
            await self.app(scope, receive, private_send)
            return

        try:
            with self.service.admit_upload() as admission:
                self._check_content_length(headers)
                scope.setdefault("state", {})["upload_admission"] = admission
                await self.app(scope, self._limited_receive(receive), private_send)
        except ServiceError as exc:
            await service_error_response(exc)(scope, receive, private_send)

    def _check_content_length(self, headers: dict) -> None:
        raw_length = headers.get(b"content-length")
        if raw_length is None:
            return
        try:
            body_size = int(raw_length)
            if body_size < 0:
                raise ValueError
        except ValueError as exc:
            raise ServiceError(400, "invalid_content_length", "Invalid Content-Length") from exc
        if body_size > self.settings.max_upload_bytes + 1024 * 1024:
            raise ServiceError(413, "upload_too_large", "Upload is too large")

    def _limited_receive(self, receive: Receive) -> Receive:
        received_bytes = 0
        deadline = time.monotonic() + self.settings.upload_timeout_sec

        async def limited_receive():
            nonlocal received_bytes
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError as exc:
                # FastAPI preserves HTTPException raised by the multipart stream.
                raise HTTPException(408, detail={
                    "code": "upload_timeout", "message": "Upload exceeded its time limit",
                }) from exc
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.settings.max_upload_bytes + 1024 * 1024:
                    raise HTTPException(413, detail={"code": "upload_too_large", "message": "Upload is too large"})
            return message

        return limited_receive


def service_error_response(error: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail}, headers=error.headers)
