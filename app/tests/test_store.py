import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from conftest import png

from image_verifier.images import validate_image
from image_verifier.models import BackendError, BackendResult, ServiceError, Verdict
from image_verifier.store import Store


def result():
    return BackendResult(
        verdict=Verdict(verdict="uncertain", reason="test"), actual_model="mock-v1", simulated=True
    )


def test_atomic_idempotency_and_conflict(store, image):
    first, replayed = store.submit([image], "key", 300)
    assert not replayed
    assert store.submit([image], "key", 300) == (first, True)
    with pytest.raises(ServiceError, match="幂等键"):
        store.submit([image], "key", 301)
    assert len(store.get_batch(first)["tasks"]) == 1


def test_bounded_queue_rejects_entire_batch(store_factory, image):
    store = store_factory(max_pending=1)
    with pytest.raises(ServiceError) as exc:
        store.submit([image, image], "overflow", 300)
    assert exc.value.status == 429
    assert store.snapshot()["tasks"] == {}
    db = store.connect()
    try:
        assert db.execute("SELECT count(*) FROM images").fetchone()[0] == 0
    finally:
        db.close()


def test_inflight_dedup_and_cache_do_not_consume_attempts(store, image):
    now = time.time()
    batch, _ = store.submit([image, image], "duplicates", 300, now)
    task = store.claim("a", now)
    assert store.claim("b", now + 0.01) is None
    assert store.finish(task, result(), now=now + 0.1)
    assert store.claim("b", now + 0.2) is None
    tasks = store.get_batch(batch)["tasks"]
    assert [t["status"] for t in tasks] == ["succeeded", "succeeded"]
    assert sum(t["attempts"] for t in tasks) == 1
    assert sum(t["cache_hit"] for t in tasks) == 1


def test_cache_ttl_requires_new_attempt(store_factory, image):
    store = store_factory(cache_ttl=1)
    now = time.time()
    store.submit([image], "first", 300, now)
    task = store.claim("w", now)
    store.finish(task, result(), now=now + 0.1)
    store.submit([image], "second", 300, now + 2)
    assert store.claim("w", now + 2)["attempts"] == 1


def test_queued_tasks_survive_restart(store, image):
    batch, _ = store.submit([image], "restart", 300)
    replacement = Store(store.settings)
    replacement.initialize()
    assert replacement.claim("new-worker")["batch_id"] == batch


def test_lost_lease_is_unknown_and_stale_completion_is_fenced(store, image):
    now = time.time()
    batch, _ = store.submit([image], "crash", 300, now)
    old = store.claim("old", now)
    assert store.claim("replacement", now + 4) is None
    assert not store.finish(old, result(), now=now + 4)
    task = store.get_batch(batch)["tasks"][0]
    assert task["status"] == "failed"
    assert task["error_code"] == "execution_unknown"
    assert store.get_task(task["id"])["attempt_history"][0]["status"] == "unknown"


def test_heartbeat_extends_lease_but_not_deadline(store, image):
    now = time.time()
    store.submit([image], "heartbeat", 5, now)
    task = store.claim("w", now)
    assert store.heartbeat(task, now + 2)
    assert store.finish(task, result(), now=now + 4)


def test_cancel_does_not_allow_late_result_or_cancel_other_task(store, image):
    now = time.time()
    batch, _ = store.submit([image, image], "cancel", 300, now)
    task = store.claim("w", now)
    store.cancel(task["id"])
    assert not store.finish(task, result(), now=now + 0.1)
    next_task = store.claim("w", now + 0.2)
    assert next_task["id"] != task["id"]
    assert store.get_batch(batch)["tasks"][0]["status"] == "cancelled"


def test_deadline_expires_without_upstream_attempt(store, image):
    now = time.time()
    batch, _ = store.submit([image], "expired", 1, now)
    assert store.claim("w", now + 2) is None
    task = store.get_batch(batch)["tasks"][0]
    assert task["status"] == "expired"
    assert task["attempts"] == 0


def test_retry_budget_and_delay(store, image):
    now = time.time()
    store.submit([image], "retry", 300, now)
    task = store.claim("w", now)
    failure = BackendError("connection_failed", retryable=True)
    store.finish(task, error=failure, retry_delay=2, now=now + 0.1)
    assert store.claim("w", now + 1) is None
    retry = store.claim("w", now + 2.2)
    assert retry["attempts"] == 2
    store.finish(retry, error=failure, now=now + 2.3)
    assert store.get_task(task["id"])["status"] == "failed"


def test_unknown_execution_never_auto_retries(store, image):
    task_batch, _ = store.submit([image], "unknown", 300)
    task = store.claim("w")
    store.finish(task, error=BackendError("unknown", retryable=True, unknown=True))
    assert store.get_batch(task_batch)["tasks"][0]["status"] == "failed"


def test_auth_failure_pauses_resource_and_rejects_admission(store, image):
    store.submit([image], "bad-auth", 300)
    task = store.claim("w")
    store.finish(task, error=BackendError("authentication_or_permission", pause=True))
    with pytest.raises(ServiceError) as exc:
        store.submit([image], "later", 300)
    assert exc.value.status == 503
    assert store.snapshot()["resources"][0]["state"] == "paused"


def test_rate_limits_shared_across_store_instances(store_factory, image):
    store = store_factory(rpm=1, rph=60)
    now = time.time()
    store.submit([image], "rate", 1000, now)
    task = store.claim("one", now)
    store.finish(task, error=BackendError("retry", retryable=True), now=now + 0.1)
    other = Store(store.settings)
    assert other.claim("two", now + 59) is None
    assert other.claim("two", now + 60.01) is not None


def test_hourly_budget_cannot_reset_with_process_restart(store_factory, image):
    store = store_factory(rpm=100, rph=1)
    now = time.time()
    store.submit([image], "hour", 10000, now)
    task = store.claim("w", now)
    store.finish(task, error=BackendError("retry", retryable=True), now=now + 0.1)
    restarted = Store(store.settings)
    restarted.initialize()
    assert restarted.claim("w", now + 3599) is None
    assert restarted.claim("w", now + 3600.01) is not None


def test_concurrent_claim_is_exclusive(store, image):
    store.submit([image], "concurrent", 300)
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda i: store.claim(str(i)), range(8)))
    assert sum(task is not None for task in claims) == 1


def test_concurrency_limit_across_different_images(store_factory, image, settings):
    store = store_factory(concurrency=1)
    second = validate_image(png("blue"), "blue.png", settings)
    now = time.time()
    store.submit([image, second], "concurrency", 300, now)
    assert store.claim("a", now)
    assert store.claim("b", now + 0.1) is None


def test_retry_after_blocks_entire_budget_domain(store, image, settings):
    other = validate_image(png("blue"), "blue.png", settings)
    now = time.time()
    store.submit([image, other], "throttle", 300, now)
    task = store.claim("a", now)
    store.finish(task, error=BackendError("limited", retryable=True, retry_after=60), now=now)
    assert store.claim("b", now + 30) is None
    assert store.claim("b", now + 61)


def test_policy_mismatch_fails_startup(store):
    with pytest.raises(RuntimeError, match="mismatch"):
        Store(replace(store.settings, rpm=1)).initialize()


def test_backup_is_readable_and_preserves_tasks(store, image, tmp_path):
    batch, _ = store.submit([image], "backup", 300)
    destination = tmp_path / "backup.sqlite3"
    store.backup(destination)
    backup = Store(replace(store.settings, db_path=destination))
    backup.initialize()
    assert backup.get_batch(batch)["id"] == batch
    with pytest.raises(ValueError):
        store.backup(destination)


def test_prune_preview_and_preserve_active_batches(store, image):
    past = time.time() - 3 * 86400
    expired, _ = store.submit([image], "expired-old", 1, past)
    store.claim("maintenance")
    active, _ = store.submit([image], "active", 300)
    # Age the terminal update to simulate a task completed days ago.
    with store.transaction() as db:
        db.execute("UPDATE tasks SET updated=? WHERE batch_id=?", (past, expired))
    assert store.prune(1)["eligible_batches"] == 1
    assert store.get_batch(expired)["done"]
    assert store.prune(1, apply=True)["eligible_batches"] == 1
    with pytest.raises(ServiceError):
        store.get_batch(expired)
    assert not store.get_batch(active)["done"]
    assert store.load_image(image.digest) == image.content
    with pytest.raises(ValueError):
        store.prune(0, apply=True)


def test_duplicate_backlog_does_not_block_unrelated_image(store, image, settings):
    second = validate_image(png("blue"), "blue.png", settings)
    now = time.time()
    store.submit([image] * 101 + [second], "many-duplicates", 300, now)
    first = store.claim("a", now)
    other = store.claim("b", now + 0.1)
    assert first["image_digest"] == image.digest
    assert other["image_digest"] == second.digest
