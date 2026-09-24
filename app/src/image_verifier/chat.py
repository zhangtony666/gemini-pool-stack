"""Thin OpenAI-compatible bridge for existing GUI clients, not a new frontend."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .images import validate_image
from .models import ServiceError
from .store import Store

MODEL_ID = "image-verifier-vision"
HELP = (
    "图片核验服务已连接。请在 Cherry Studio 中给本模型开启视觉能力，然后上传图片发送。\n\n"
    "每条消息最多20张（实际以上传限制为准），仅处理最后一条用户消息中的图片。\n\n"
    "可用操作：\n"
    "- `/batches`：最近10个批次\n"
    "- `/status 批次ID`：查看进度和结果\n"
    "- `/cancel 任务ID`：取消任务\n"
    "- `/stats`：查看运行统计\n\n"
    "聊天断开不会删除已接受的任务，可重新查询。模拟后端不提供真实性结论。"
)


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: Literal["image-verifier-vision"]
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    stream: bool = False


def extract_images(body: ChatRequest, settings: Settings):
    latest = next((m for m in reversed(body.messages) if m.role == "user"), None)
    if latest is None:
        raise ServiceError("missing_user_message", "需要用户消息")
    if isinstance(latest.content, str) or latest.content is None:
        return latest.content or "", []
    text, images = [], []
    for part in latest.content:
        if part.get("type") == "text":
            if not isinstance(part.get("text"), str):
                raise ServiceError("invalid_text", "消息文本格式错误")
            text.append(part["text"])
        elif part.get("type") == "image_url":
            if len(images) >= settings.max_batch:
                raise ServiceError("batch_size", f"每次最多{settings.max_batch}张图片", 413)
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if not isinstance(url, str):
                raise ServiceError("invalid_image_url", "图片需使用image_url.url格式")
            match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,(.*)", url, re.DOTALL)
            if not match:
                raise ServiceError(
                    "inline_image_required", "请直接上传图片；不读取远程URL或本地路径"
                )
            encoded = match.group(2)
            if len(encoded) > (settings.max_image_bytes + 2) // 3 * 4:
                raise ServiceError("image_size", "图片超过单图大小上限", 413)
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ServiceError("invalid_base64", "图片编码无效") from exc
            image = validate_image(content, f"image-{len(images) + 1}.{match.group(1)}", settings)
            if image.mime != f"image/{match.group(1)}":
                raise ServiceError("mime_mismatch", "图片声明类型与内容不符")
            images.append(image)
        else:
            raise ServiceError("unsupported_content", "本服务只接受文本和图片")
    return "\n".join(text), images


def render_batch(batch):
    lines = [f"批次：`{batch['id']}`", "状态：" + ("已完成" if batch["done"] else "处理中"), ""]
    labels = {"yes": "高度可能为生成图片", "no": "高度可能为真实拍摄", "uncertain": "不确定"}
    for task in batch["tasks"]:
        output = task["result"]
        label = f"图片 {task['position'] + 1}"
        if output:
            prefix = "【模拟结果】" if output["simulated"] else ""
            reason = output["verdict"]["reason"].replace("\n", " ")
            lines.append(f"- {label}：{prefix}{labels[output['verdict']['verdict']]}。{reason}")
        else:
            lines.append(
                f"- {label}：{task['status']}，任务 `{task['id']}`"
                + (f"，错误 `{task['error_code']}`" if task["error_code"] else "")
            )
    if not batch["done"]:
        lines.extend(["", f"稍后发送 `/status {batch['id']}` 查询；任务会在后台继续。"])
    return "\n".join(lines)


def completion(text, response_id):
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                },
                "finish_reason": "stop",
            }
        ],
    }


def chunk(text, response_id, *, final=False):
    data = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": {} if final else {"role": "assistant", "content": text},
                "finish_reason": "stop" if final else None,
            }
        ],
    }
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def router(store: Store, settings: Settings, auth):
    routes = APIRouter(dependencies=[Depends(auth)], tags=["GUI compatibility"])

    @routes.get("/v1/models")
    async def models():
        return {
            "object": "list",
            "data": [
                {
                    "id": MODEL_ID,
                    "object": "model",
                    "created": 0,
                    "owned_by": "image-verifier",
                    "name": "图片真实性核验（本地服务）",
                }
            ],
        }

    @routes.post("/v1/chat/completions")
    async def chat(
        body: ChatRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
    ):
        text, images = await asyncio.to_thread(extract_images, body, settings)
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        batch_id, immediate = None, None
        if images:
            # GUI sends no idempotency key in most versions; content cache still deduplicates.
            batch_id, _ = await asyncio.to_thread(
                store.submit,
                images,
                idempotency_key or uuid.uuid4().hex,
                300,
            )
        else:
            command = text.strip()
            status_match = re.fullmatch(r"/status ([a-f0-9]{32})", command)
            cancel_match = re.fullmatch(r"/cancel ([a-f0-9]{32})", command)
            if status_match:
                immediate = render_batch(await asyncio.to_thread(store.get_batch, status_match[1]))
            elif cancel_match:
                await asyncio.to_thread(store.cancel, cancel_match[1])
                saved = await asyncio.to_thread(store.get_task, cancel_match[1])
                immediate = f"任务 `{saved['id']}` 当前状态：{saved['status']}。"
            elif command == "/stats":
                immediate = (
                    "```json\n"
                    + json.dumps(
                        await asyncio.to_thread(store.snapshot),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n```"
                )
            elif command == "/batches":
                rows = await asyncio.to_thread(store.recent_batches)
                immediate = (
                    "\n".join(
                        f"- `{b['id']}`：{b['completed']}/{b['total']} 已进入终态。"
                        f"发送 `/status {b['id']}` 查看。"
                        for b in rows
                    )
                    or "暂无批次，请上传图片。"
                )
            else:
                immediate = HELP

        async def final_text():
            if immediate is not None:
                return immediate
            until = time.monotonic() + settings.chat_wait_seconds
            while True:
                batch = await asyncio.to_thread(store.get_batch, batch_id)
                if batch["done"] or time.monotonic() >= until:
                    return render_batch(batch)
                await asyncio.sleep(0.2)

        if not body.stream:
            return completion(await final_text(), response_id)

        async def events():
            if batch_id:
                yield chunk(f"已接收批次 `{batch_id}`，共{len(images)}张图片。\n\n", response_id)
            waiter = asyncio.create_task(final_text())
            try:
                while not waiter.done():
                    if await request.is_disconnected():
                        return
                    done, _ = await asyncio.wait({waiter}, timeout=1)
                    if not done:
                        yield ": keep-alive\n\n"
                yield chunk(await waiter, response_id)
                yield chunk("", response_id, final=True)
                yield "data: [DONE]\n\n"
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return routes
