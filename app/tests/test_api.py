import time
from dataclasses import replace

from conftest import png
from fastapi.testclient import TestClient

from image_verifier.api import create_app


def submit(client, key="test", content=None, **kwargs):
    return client.post(
        "/v1/batches",
        headers={"Idempotency-Key": key},
        files=[("files", ("sample.png", content or png(), "image/png"))],
        **kwargs,
    )


def test_full_mock_flow(settings):
    with TestClient(create_app(settings)) as client:
        response = submit(client)
        assert response.status_code == 202
        url = response.json()["status_url"]
        for _ in range(100):
            batch = client.get(url).json()
            if batch["done"]:
                break
            time.sleep(0.02)
        assert batch["done"]
        task = batch["tasks"][0]
        assert task["status"] == "succeeded"
        assert task["result"]["simulated"] is True
        assert task["result"]["verdict"]["verdict"] == "uncertain"
        assert client.get("/v1/metrics").json()["attempts"] == 1
        assert "lease_token" not in client.get(f"/v1/tasks/{task['id']}").json()


def test_authentication_and_admin_separation(settings):
    secured = replace(settings, api_key="business-secret", admin_key="admin-secret")
    with TestClient(create_app(secured, start_workers=False)) as client:
        assert submit(client).status_code == 401
        client.headers["Authorization"] = "Bearer business-secret"
        assert submit(client).status_code == 202
        assert (
            client.put("/admin/resource", json={"state": "paused", "reason": "test"}).status_code
            == 401
        )
        client.headers["Authorization"] = "Bearer admin-secret"
        assert (
            client.put("/admin/resource", json={"state": "paused", "reason": "test"}).status_code
            == 200
        )
        assert client.get("/health/ready").status_code == 503


def test_invalid_file_rejected_without_task(settings):
    with TestClient(create_app(settings, start_workers=False)) as client:
        response = submit(client, content=b"not-an-image")
        assert response.status_code == 400
        assert client.get("/v1/metrics").json()["tasks"] == {}


def test_file_size_and_pixel_limits(settings):
    with TestClient(
        create_app(replace(settings, max_image_bytes=10), start_workers=False)
    ) as client:
        assert submit(client).status_code == 413
    with TestClient(create_app(replace(settings, max_pixels=10), start_workers=False)) as client:
        assert submit(client).status_code == 413


def test_idempotency_http_and_cancel(settings):
    with TestClient(create_app(settings, start_workers=False)) as client:
        first = submit(client).json()
        assert submit(client).json()["replayed"] is True
        assert submit(client, data={"deadline_seconds": 10}).status_code == 409
        task_id = first["task_ids"][0]
        assert client.post(f"/v1/tasks/{task_id}/cancel").json()["status"] == "cancelled"
        assert client.get(first["status_url"]).json()["done"] is True
        assert client.get("/v1/tasks/missing").status_code == 404


def test_missing_idempotency_key_and_admin_disabled(settings):
    with TestClient(create_app(settings, start_workers=False)) as client:
        response = client.post("/v1/batches", files={"files": ("test.png", png(), "image/png")})
        assert response.status_code == 422
        assert (
            client.put("/admin/resource", json={"state": "ready", "reason": "test"}).status_code
            == 403
        )


def test_content_length_limit(settings):
    with TestClient(create_app(settings, start_workers=False)) as client:
        response = client.post(
            "/v1/batches",
            content=b"x",
            headers={
                "Content-Length": str(settings.max_body_bytes + 1),
            },
        )
        assert response.status_code == 413


def test_batch_limit_and_queue_backpressure(settings):
    limited = replace(settings, max_batch=1, max_pending=1)
    with TestClient(create_app(limited, start_workers=False)) as client:
        response = client.post(
            "/v1/batches",
            headers={"Idempotency-Key": "many"},
            files=[
                ("files", ("a.png", png(), "image/png")),
                ("files", ("b.png", png(), "image/png")),
            ],
        )
        assert response.status_code == 413
        assert submit(client, key="one").status_code == 202
        response = submit(client, key="two")
        assert response.status_code == 429
        assert "Retry-After" in response.headers


def test_chunked_body_limit(settings):
    tiny = replace(settings, max_image_bytes=1, max_batch=1)
    with TestClient(create_app(tiny, start_workers=False)) as client:
        body = (
            b'--boundary\r\nContent-Disposition: form-data; name="files"; filename="a.png"'
            b"\r\nContent-Type: image/png\r\n\r\n"
            + b"x" * (tiny.max_body_bytes + 1)
            + b"\r\n--boundary--\r\n"
        )
        response = client.post(
            "/v1/batches",
            content=iter([body]),
            headers={
                "Content-Type": "multipart/form-data; boundary=boundary",
                "Idempotency-Key": "large-stream",
            },
        )
        assert response.status_code == 413


def test_readiness_fails_without_worker(settings):
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
