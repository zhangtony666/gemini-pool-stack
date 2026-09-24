import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from image_verifier.backends import (
    GeminiBackend,
    WebBackend,
    extract_json_object,
    retry_after_seconds,
)
from image_verifier.config import PROMPT
from image_verifier.models import BackendError


def call(settings, handler):
    async def invoke():
        config = replace(settings, backend="gemini", model="test-model", gemini_key="test-secret")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = GeminiBackend(config, client)
        try:
            return await backend.analyze(b"image", "image/png", 5)
        finally:
            await backend.close()

    return asyncio.run(invoke())


def web_call(settings, handler, **overrides):
    async def invoke():
        fields = {
            "backend": "web",
            "model": "test-model",
            "web_base_url": "http://127.0.0.1:8084/v1",
            "web_api_key": "",
        }
        fields.update(overrides)
        config = replace(settings, **fields)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = WebBackend(config, client)
        try:
            return await backend.analyze(b"image", "image/png", 5)
        finally:
            await backend.close()

    return asyncio.run(invoke())


def chat_reply(text, **extra):
    payload = {
        "id": "chatcmpl-1",
        "model": "actual-model",
        "choices": [{"message": {"role": "assistant", "content": text}}],
    }
    payload.update(extra)
    return httpx.Response(200, json=payload)


# --- Gemini official adapter --------------------------------------------------


def test_official_request_contract_and_response(settings):
    def handler(request):
        assert str(request.url).endswith("/v1beta/models/test-model:generateContent")
        assert request.headers["x-goog-api-key"] == "test-secret"
        body = json.loads(request.content)
        assert body["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/png"
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(
            200,
            json={
                "modelVersion": "actual-model",
                "responseId": "r123",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"text": '{"verdict":"uncertain","reason":"无法确认"}'},
                            ]
                        },
                    }
                ],
            },
        )

    output = call(settings, handler)
    assert output.actual_model == "actual-model"
    assert output.provider_request_id == "r123"
    assert not output.simulated


@pytest.mark.parametrize(
    "status,code,retryable,pause,unknown",
    [
        (401, "authentication_or_permission", False, True, False),
        (403, "authentication_or_permission", False, True, False),
        (429, "upstream_rate_limited", True, False, False),
        (302, "unexpected_redirect", False, True, False),
        (503, "upstream_execution_unknown", False, False, True),
        (400, "upstream_rejected", False, False, False),
    ],
)
def test_error_classification(settings, status, code, retryable, pause, unknown):
    with pytest.raises(BackendError) as exc:
        call(settings, lambda request: httpx.Response(status, headers={"Retry-After": "12"}))
    assert (exc.value.code, exc.value.retryable, exc.value.pause, exc.value.unknown) == (
        code,
        retryable,
        pause,
        unknown,
    )
    if status == 429:
        assert exc.value.retry_after == 12


def test_response_timeout_is_not_retried(settings):
    def handler(request):
        raise httpx.ReadTimeout("sensitive response must not be stored")

    with pytest.raises(BackendError) as exc:
        call(settings, handler)
    assert exc.value.unknown
    assert not exc.value.retryable
    assert str(exc.value) == "execution_unknown"


def test_invalid_json_is_not_success(settings):
    with pytest.raises(BackendError, match="invalid_output"):
        call(
            settings,
            lambda request: httpx.Response(
                200,
                json={
                    "candidates": [
                        {"finishReason": "STOP", "content": {"parts": [{"text": "yes"}]}}
                    ],
                },
            ),
        )


@pytest.mark.parametrize("value,expected", [(None, 5), ("15", 15), ("NaN", 5), ("bad", 5)])
def test_retry_after_parser(value, expected):
    assert retry_after_seconds(value) == expected


# --- Web bridge adapter -------------------------------------------------------


def test_web_request_contract_and_response(settings):
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:8084/v1/chat/completions"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["stream"] is False
        content = body["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": PROMPT}
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return chat_reply('{"verdict":"yes","reason":"合成痕迹明显"}')

    output = web_call(settings, handler)
    assert output.verdict.verdict == "yes"
    assert output.verdict.reason == "合成痕迹明显"
    assert output.actual_model == "actual-model"
    assert output.provider_request_id == "chatcmpl-1"
    assert not output.simulated


def test_web_sends_bearer_token_when_configured(settings):
    def handler(request):
        assert request.headers["authorization"] == "Bearer gateway-key"
        return chat_reply('{"verdict":"no","reason":"真实拍摄痕迹"}')

    output = web_call(settings, handler, web_api_key="gateway-key")
    assert output.verdict.verdict == "no"


def test_web_trailing_slash_in_base_url_is_not_doubled(settings):
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:8084/v1/chat/completions"
        return chat_reply('{"verdict":"yes","reason":"ok"}')

    output = web_call(settings, handler, web_base_url="http://127.0.0.1:8084/v1/")
    assert output.verdict.verdict == "yes"


@pytest.mark.parametrize(
    "status,code,retryable,pause,unknown",
    [
        (401, "authentication_or_permission", False, True, False),
        (403, "authentication_or_permission", False, True, False),
        (429, "upstream_rate_limited", True, False, False),
        (302, "unexpected_redirect", False, True, False),
        (503, "upstream_execution_unknown", False, False, True),
        (400, "upstream_rejected", False, False, False),
    ],
)
def test_web_error_classification(settings, status, code, retryable, pause, unknown):
    with pytest.raises(BackendError) as exc:
        web_call(
            settings,
            lambda request: httpx.Response(status, headers={"Retry-After": "12"}),
        )
    assert (exc.value.code, exc.value.retryable, exc.value.pause, exc.value.unknown) == (
        code,
        retryable,
        pause,
        unknown,
    )
    if status == 429:
        assert exc.value.retry_after == 12


def test_web_timeout_is_not_retried(settings):
    def handler(request):
        raise httpx.ReadTimeout("sensitive response must not be stored")

    with pytest.raises(BackendError) as exc:
        web_call(settings, handler)
    assert exc.value.unknown
    assert not exc.value.retryable
    assert str(exc.value) == "execution_unknown"


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict":"no","reason":"真实拍摄痕迹"}',
        '```json\n{"verdict":"no","reason":"真实拍摄痕迹"}\n```',
        '判断如下：\n{"verdict":"no","reason":"真实拍摄痕迹"}\n以上。',
        '```\n{"verdict":"no","reason":"真实拍摄痕迹"}\n```',
    ],
)
def test_web_extracts_json_from_free_form_reply(settings, text):
    output = web_call(settings, lambda request: chat_reply(text))
    assert output.verdict.verdict == "no"


@pytest.mark.parametrize("text", ["", "   "])
def test_web_empty_content_is_incomplete(settings, text):
    with pytest.raises(BackendError, match="incomplete_response"):
        web_call(settings, lambda request: chat_reply(text))


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "没有 JSON",
        '{"verdict":"maybe","reason":"不在枚举内"}',
        '{"verdict":"yes","reason":"x","extra":1}',
        '{"verdict":"yes"}',
    ],
)
def test_web_unusable_reply_is_not_success(settings, text):
    with pytest.raises(BackendError, match="invalid_output"):
        web_call(settings, lambda request: chat_reply(text))


def test_web_missing_choices_is_invalid_output(settings):
    with pytest.raises(BackendError, match="invalid_output"):
        web_call(settings, lambda request: httpx.Response(200, json={"id": "x"}))


def test_web_falls_back_to_configured_model(settings):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-2",
                "choices": [{"message": {"content": '{"verdict":"yes","reason":"ok"}'}}],
            },
        )

    assert web_call(settings, handler).actual_model == "test-model"


def test_extract_json_object_rejects_prose_without_object():
    with pytest.raises(ValueError):
        extract_json_object("模型没有输出 JSON")


# --- Backend selection --------------------------------------------------------


def test_create_backend_selects_each_kind(settings):
    from image_verifier.backends import MockBackend, create_backend

    assert isinstance(create_backend(settings), MockBackend)
    assert isinstance(
        create_backend(replace(settings, backend="web", model="test-model")), WebBackend
    )
    assert isinstance(
        create_backend(replace(settings, backend="gemini", model="test-model", gemini_key="k")),
        GeminiBackend,
    )
