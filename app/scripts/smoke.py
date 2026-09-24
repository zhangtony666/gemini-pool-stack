"""Real HTTP + process restart smoke test. Isolated mock data; no external calls."""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cherry-sdk", action="store_true", help="Also test Cherry SDK packages")
    args = parser.parse_args()
    work = ROOT / "work" / "smoke" / uuid.uuid4().hex
    work.mkdir(parents=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("IV_")}
    env.update(
        {
            "IV_DB_PATH": str(work / "smoke.sqlite3"),
            "IV_BACKEND": "mock",
            "IV_PORT": str(port),
            "IV_RPM": "60000",
            "IV_RPH": "3600000",
            "IV_API_KEY": "smoke-business-key",
            "IV_ADMIN_KEY": "smoke-admin-key",
            "PYTHONUTF8": "1",
        }
    )
    base_url = f"http://127.0.0.1:{port}"
    log = (work / "server.log").open("wb")

    def launch(api_only=False):
        command = [sys.executable, "-m", "image_verifier.cli", "serve"]
        if api_only:
            command.append("--api-only")
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(f"Server exited; inspect {work / 'server.log'}")
                try:
                    if httpx.get(f"{base_url}/health/live", timeout=0.2).status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            raise RuntimeError("Server startup timeout")
        except BaseException:
            process.terminate()
            process.wait(timeout=10)
            raise

    def stop(process):
        process.terminate()
        process.wait(timeout=10)

    process = None
    try:
        process = launch(api_only=True)
        image = io.BytesIO()
        Image.new("RGB", (32, 32), "blue").save(image, "PNG")
        headers = {"Authorization": "Bearer smoke-business-key", "Idempotency-Key": "smoke"}
        with httpx.Client(base_url=base_url, headers=headers, timeout=10) as client:
            response = client.post(
                "/v1/batches",
                files=[
                    ("files", ("a.png", image.getvalue(), "image/png")),
                    ("files", ("b.png", image.getvalue(), "image/png")),
                ],
            )
            response.raise_for_status()
            status_url = response.json()["status_url"]
            assert not client.get(status_url).json()["done"]
            # Kill the API before any worker can execute; queued tasks must survive.
            stop(process)
            process = launch()
            for _ in range(100):
                batch = client.get(status_url).json()
                if batch["done"]:
                    break
                time.sleep(0.05)
            assert batch["done"]
            assert all(t["status"] == "succeeded" for t in batch["tasks"])
            assert all(t["result"]["simulated"] for t in batch["tasks"])
            metrics = client.get("/v1/metrics").json()
            assert metrics["attempts"] == 1 and metrics["cache_hits"] == 1
            assert client.get("/health/ready").status_code == 200
            if args.cherry_sdk:
                image_path = work / "sample.png"
                image_path.write_bytes(image.getvalue())
                sdk_env = os.environ | {
                    "IV_TEST_BASE_URL": base_url + "/v1",
                    "IV_TEST_IMAGE": str(image_path),
                }
                sdk_run = subprocess.run(
                    ["node", str(ROOT / "tests" / "cherry_sdk" / "contract.mjs")],
                    env=sdk_env,
                    check=True,
                    timeout=30,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                sdk_report = json.loads(sdk_run.stdout.strip())
                assert sdk_report["sdkContract"] and sdk_report["streamedImage"]
                print(json.dumps(sdk_report))
            report = {
                "passed": True,
                "restart_recovery": True,
                "tasks": 2,
                "upstream_attempts": 1,
                "cache_hits": 1,
                "backend": "mock",
                "cherry_sdk": args.cherry_sdk,
            }
            (work / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report))
    finally:
        if process and process.poll() is None:
            stop(process)
        log.close()


if __name__ == "__main__":
    main()
