import asyncio

from image_verifier.models import BackendError, BackendResult, Verdict
from image_verifier.worker import WorkerPool


def test_worker_records_auth_failure(store, image):
    class BrokenBackend:
        async def analyze(self, content, mime, timeout):
            raise BackendError("authentication_or_permission", pause=True)

        async def close(self):
            pass

    batch, _ = store.submit([image], "worker-auth", 300)
    task = store.claim("w")
    asyncio.run(WorkerPool(store, BrokenBackend()).execute(task))
    assert store.get_batch(batch)["tasks"][0]["status"] == "failed"
    assert store.snapshot()["resources"][0]["state"] == "paused"


def test_worker_total_timeout_is_unknown(store_factory, image):
    store = store_factory(attempt_timeout=0.02)

    class SlowBackend:
        async def analyze(self, content, mime, timeout):
            await asyncio.sleep(10)

        async def close(self):
            pass

    store.submit([image], "slow", 300)
    task = store.claim("w")
    asyncio.run(WorkerPool(store, SlowBackend()).execute(task))
    saved = store.get_task(task["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "execution_unknown"
    assert saved["attempts"] == 1


def test_worker_cannot_write_success_after_cancel(store, image):
    class CancelDuringCall:
        async def analyze(self, content, mime, timeout):
            store.cancel(task["id"])
            return BackendResult(
                verdict=Verdict(verdict="uncertain", reason="test"),
                actual_model="mock-v1",
                simulated=True,
            )

        async def close(self):
            pass

    store.submit([image], "cancel-during", 300)
    task = store.claim("w")
    asyncio.run(WorkerPool(store, CancelDuringCall()).execute(task))
    saved = store.get_task(task["id"])
    assert saved["status"] == "cancelled"
    assert saved["result"] is None
