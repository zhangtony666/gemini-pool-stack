from __future__ import annotations

import asyncio
import base64
import json
import time
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx
from pydantic import ValidationError

from .config import PROMPT, Settings
from .models import BackendError, BackendResult, Verdict


class Backend(Protocol):
    async def analyze(self, content: bytes, mime: str, timeout: float) -> BackendResult: ...

    async def close(self) -> None: ...


class MockBackend:
    async def analyze(self, content: bytes, mime: str, timeout: float) -> BackendResult:
        await asyncio.sleep(0.01)
        return BackendResult(
            verdict=Verdict(verdict="uncertain", reason="模拟后端结果，仅验证系统流程"),
            actual_model="mock-v1",
            simulated=True,
        )

    async def close(self):
        pass


def retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 5
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            seconds = 5
    if not 0 <= seconds < float("inf"):
        return 5
    return seconds


def extract_json_object(text: str) -> str:
    """Pull the JSON object out of a free-form model reply.

    OpenAI-shaped gateways do not honour a response schema, so the model may
    wrap its answer in a fenced block or surround it with prose. Only the
    outermost object is returned; the caller validates it strictly.
    """
    body = text.strip()
    if body.startswith("```"):
        newline = body.find("\n")
        body = body[newline + 1 :] if newline != -1 else ""
        closing = body.rfind("```")
        if closing != -1:
            body = body[:closing]
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return body[start : end + 1]


class GeminiBackend:
    """Official generateContent API adapter. No hidden retries at this layer."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=settings.concurrency),
            timeout=settings.attempt_timeout,
        )

    async def analyze(self, content: bytes, mime: str, timeout: float) -> BackendResult:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.model}:generateContent"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": PROMPT},
                        {
                            "inlineData": {
                                "mimeType": mime,
                                "data": base64.b64encode(content).decode(),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "required": ["verdict", "reason"],
                    "properties": {
                        "verdict": {"type": "STRING", "enum": ["yes", "no", "uncertain"]},
                        "reason": {"type": "STRING"},
                    },
                },
            },
        }
        try:
            response = await self.client.post(
                url,
                headers={"x-goog-api-key": self.settings.gemini_key},
                json=payload,
                timeout=timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise BackendError("connection_failed", retryable=True) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise BackendError("execution_unknown", unknown=True) from exc
        status = response.status_code
        if status in {401, 403}:
            raise BackendError("authentication_or_permission", pause=True)
        if status == 429:
            raise BackendError(
                "upstream_rate_limited",
                retryable=True,
                retry_after=retry_after_seconds(response.headers.get("retry-after")),
            )
        if 300 <= status < 400:
            raise BackendError("unexpected_redirect", pause=True)
        if status >= 500:
            # A server error does not prove inference did not already execute.
            raise BackendError("upstream_execution_unknown", unknown=True)
        if status != 200:
            raise BackendError("upstream_rejected")
        try:
            data = response.json()
            candidate = data["candidates"][0]
            if candidate.get("finishReason") != "STOP":
                raise BackendError("incomplete_response")
            output = "".join(
                part["text"]
                for part in candidate["content"]["parts"]
                if "text" in part and not part.get("thought")
            )
            return BackendResult(
                verdict=Verdict.model_validate_json(output),
                actual_model=data.get("modelVersion") or self.settings.model,
                provider_request_id=data.get("responseId"),
            )
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            raise BackendError("invalid_output") from exc

    async def close(self):
        await self.client.aclose()


class WebBackend:
    """Adapter for an OpenAI-shaped gateway in front of a Web session pool.

    The gateway owns the pool: session credentials, egress bindings, upstream
    pacing, keep-alive and its own error signals. This adapter only converts the
    protocol, maps errors onto the local taxonomy and validates the verdict, so
    the scheduler above it never has to know how the capacity is produced.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=settings.concurrency),
            timeout=settings.attempt_timeout,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.settings.web_base_url.rstrip('/')}/chat/completions"

    async def analyze(self, content: bytes, mime: str, timeout: float) -> BackendResult:
        data_url = f"data:{mime};base64,{base64.b64encode(content).decode()}"
        payload = {
            "model": self.settings.model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.web_api_key:
            headers["Authorization"] = f"Bearer {self.settings.web_api_key}"
        try:
            response = await self.client.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise BackendError("connection_failed", retryable=True) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise BackendError("execution_unknown", unknown=True) from exc
        status = response.status_code
        if status in {401, 403}:
            raise BackendError("authentication_or_permission", pause=True)
        if status == 429:
            raise BackendError(
                "upstream_rate_limited",
                retryable=True,
                retry_after=retry_after_seconds(response.headers.get("retry-after")),
            )
        if 300 <= status < 400:
            raise BackendError("unexpected_redirect", pause=True)
        if status >= 500:
            raise BackendError("upstream_execution_unknown", unknown=True)
        if status != 200:
            raise BackendError("upstream_rejected")
        try:
            data = response.json()
            message = data["choices"][0]["message"]
            text = message["content"]
            if not isinstance(text, str) or not text.strip():
                raise BackendError("incomplete_response")
            return BackendResult(
                verdict=Verdict.model_validate_json(extract_json_object(text)),
                actual_model=data.get("model") or self.settings.model,
                provider_request_id=data.get("id"),
            )
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            raise BackendError("invalid_output") from exc

    async def close(self):
        await self.client.aclose()


def create_backend(settings: Settings) -> Backend:
    if settings.backend == "mock":
        return MockBackend()
    if settings.backend == "web":
        return WebBackend(settings)
    return GeminiBackend(settings)
