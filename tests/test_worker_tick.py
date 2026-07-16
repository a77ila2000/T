"""
Regression tests for oracle/worker_tick.py's main() control flow.
Run with: pip install pytest && pytest tests/ -v
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "oracle"))
import barcode_core as bc  # noqa: E402
import worker_tick as wt  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_worker_tick_refs(monkeypatch):
    # worker_tick.py imports these names directly from barcode_core at module load time, so
    # patching bc.redis_command alone wouldn't affect wt's already-bound reference.
    monkeypatch.setattr(wt, "select_warm_target", bc.select_warm_target)
    monkeypatch.setattr(wt, "acquire_warm_lock", bc.acquire_warm_lock)
    monkeypatch.setattr(wt, "release_warm_lock", bc.release_warm_lock)
    monkeypatch.setattr(wt, "acquire_browser_lock", bc.acquire_browser_lock)
    monkeypatch.setattr(wt, "release_browser_lock", bc.release_browser_lock)


class TestIdleTickRedisUsage:
    def test_idle_tick_costs_exactly_one_command(self, monkeypatch):
        # 2026-07-16 bug: main() acquired+released the global warm lock on every ~20s tick
        # even when nothing was due (5 Redis commands per idle tick), projected to exceed
        # Upstash's free-tier monthly command quota at this polling frequency.
        now = int(time.time())
        store = {
            bc.warm_state_key(t["id"], t["type"]): json.dumps({"next_refresh_at": now + 5000})
            for t in bc.WARM_TARGETS
        }
        calls = []

        def counting_redis_command(command, timeout=4):
            calls.append(command[0])
            if command[0] == "MGET":
                return [store.get(k) for k in command[1:]]
            return None

        monkeypatch.setattr(bc, "redis_command", counting_redis_command)
        monkeypatch.setattr(wt, "redis_command", counting_redis_command)

        wt.main()

        assert calls == ["MGET"]


class TestDueTargetStillWorks:
    def test_due_target_is_scraped_and_recorded(self, monkeypatch):
        now = int(time.time())
        store = {
            bc.warm_state_key(t["id"], t["type"]): json.dumps({"next_refresh_at": now + 5000})
            for t in bc.WARM_TARGETS
        }
        store[bc.warm_state_key("a77ila2000", "universe")] = json.dumps({"next_refresh_at": now - 10})

        def fake_redis_command(command, timeout=4):
            if command[0] == "MGET":
                return [store.get(k) for k in command[1:]]
            if command[0] == "SET":
                return "OK"
            if command[0] == "GET":
                return None
            return None

        monkeypatch.setattr(bc, "redis_command", fake_redis_command)
        monkeypatch.setattr(wt, "redis_command", fake_redis_command)
        monkeypatch.setattr(wt, "select_warm_target", bc.select_warm_target)
        monkeypatch.setattr(wt, "acquire_warm_lock", bc.acquire_warm_lock)
        monkeypatch.setattr(wt, "acquire_browser_lock", bc.acquire_browser_lock)
        monkeypatch.setattr(wt, "release_browser_lock", lambda token: None)

        called = {}

        def fake_perform_scrape(target, force_scrape=False):
            called["target"] = target["name"]
            return {"success": True, "code": "200"}

        monkeypatch.setattr(wt, "perform_scrape", fake_perform_scrape)

        recorded = {}

        def fake_record_warm_result(target, token, success, http_code=""):
            recorded["target"] = target["name"]
            recorded["success"] = success
            return {"next_refresh_at": now + 1200}

        monkeypatch.setattr(wt, "record_warm_result", fake_record_warm_result)

        wt.main()

        assert called.get("target") == "me-universe"
        assert recorded.get("target") == "me-universe"
        assert recorded.get("success") is True
