import base64
import json
from dataclasses import replace

import pytest
from conftest import png
from fastapi.testclient import TestClient

from image_verifier.api import create_app
from image_verifier.chat import MODEL_ID


def message(text="核验这张图片", *, image=True):
    content = [{"type": "text", "text": text}]
    if image:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png()).decode(),
                },
            }
        )
    return {"role": "user", "content": content}


def test_model_discovery_and_connectivity_check(settings):
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert client.get("/v1/models").json()["data"][0]["id"] == MODEL_ID
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [message("hello", image=False)],
            },
        )
        assert response.status_code == 200
        assert "已连接" in response.json()["choices"][0]["message"]["content"]
        assert client.get("/v1/metrics").json()["attempts"] == 0


def test_chat_image_nonstream_and_explicit_idempotency(settings):
    with TestClient(create_app(settings)) as client:
        request = {"model": MODEL_ID, "messages": [message()], "temperature": 0.1}
        response = client.post(
            "/v1/chat/completions", json=request, headers={"Idempotency-Key": "chat-test"}
        )
        assert response.status_code == 200
        assert "模拟结果" in response.json()["choices"][0]["message"]["content"]
        client.post("/v1/chat/completions", json=request, headers={"Idempotency-Key": "chat-test"})
        assert client.get("/v1/metrics").json()["attempts"] == 1


def test_chat_image_stream_sse(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [message()],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert lines[-1] == "[DONE]"
        events = [json.loads(line) for line in lines[:-1]]
        assert events[-1]["choices"][0]["finish_reason"] == "stop"
        text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
        assert "已接收批次" in text and "模拟结果" in text


def test_history_images_are_not_resubmitted(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [
                    message(),
                    {"role": "assistant", "content": "old"},
                    message("/stats", image=False),
                ],
            },
        )
        assert response.status_code == 200
        assert client.get("/v1/metrics").json()["attempts"] == 0


def test_gui_batch_status_and_cancel(settings):
    app = create_app(replace(settings, chat_wait_seconds=0.01), start_workers=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [message()],
            },
        )
        assert "后台继续" in response.json()["choices"][0]["message"]["content"]
        batch = app.state.store.recent_batches()[0]
        task = app.state.store.get_batch(batch["id"])["tasks"][0]
        for command in ("/batches", f"/status {batch['id']}", f"/cancel {task['id']}"):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": MODEL_ID,
                    "messages": [message(command, image=False)],
                },
            )
            assert response.status_code == 200
        assert app.state.store.get_task(task["id"])["status"] == "cancelled"


@pytest.mark.parametrize(
    "url", ["https://example.com/a.png", "file:///C:/a.png", "data:image/png;base64,!!!"]
)
def test_chat_rejects_remote_and_invalid_images(settings, url):
    with TestClient(create_app(settings, start_workers=False)) as client:
        user = message()
        user["content"][1]["image_url"]["url"] = url
        response = client.post("/v1/chat/completions", json={"model": MODEL_ID, "messages": [user]})
        assert response.status_code == 400
        assert client.get("/v1/metrics").json()["tasks"] == {}


def test_chat_auth_is_enforced(settings):
    with TestClient(create_app(replace(settings, api_key="secret"))) as client:
        assert client.get("/v1/models").status_code == 401
        assert (
            client.post(
                "/v1/chat/completions",
                json={
                    "model": MODEL_ID,
                    "messages": [message()],
                },
            ).status_code
            == 401
        )
