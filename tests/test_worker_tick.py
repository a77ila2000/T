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

        def fake_perform_scrape_batch(requests, on_target_start=None):
            requests = list(requests)
            called["targets"] = [target["name"] for target, _force in requests]
            for target, _force in requests:
                if on_target_start:
                    on_target_start(target)
            return [(target, {"success": True, "code": "200"}) for target, _force in requests]

        monkeypatch.setattr(wt, "perform_scrape_batch", fake_perform_scrape_batch)

        recorded = {}

        def fake_record_warm_result(target, token, success, http_code="", release_lock=True):
            recorded["target"] = target["name"]
            recorded["success"] = success
            recorded["release_lock"] = release_lock
            return {"next_refresh_at": now + 1200}

        monkeypatch.setattr(wt, "record_warm_result", fake_record_warm_result)

        wt.main()

        assert called.get("targets") == ["me-universe"]
        assert recorded.get("target") == "me-universe"
        assert recorded.get("success") is True
        assert recorded.get("release_lock") is False


class TestPairedRefresh:
    def test_same_account_due_types_share_one_batch_and_one_warm_lock(self, monkeypatch):
        now = int(time.time())
        store = {
            bc.warm_state_key(t["id"], t["type"]): json.dumps({"next_refresh_at": now + 5000})
            for t in bc.WARM_TARGETS
        }
        store[bc.warm_state_key("a77ila2000", "universe")] = json.dumps({"next_refresh_at": now - 10})
        store[bc.warm_state_key("a77ila2000", "general")] = json.dumps({"next_refresh_at": now - 9})

        def fake_redis_command(command, timeout=4):
            if command[0] == "MGET":
                return [store.get(key) for key in command[1:]]
            if command[0] == "SET":
                return "OK"
            return None

        monkeypatch.setattr(bc, "redis_command", fake_redis_command)
        monkeypatch.setattr(wt, "redis_command", fake_redis_command)
        monkeypatch.setattr(wt, "select_warm_target", bc.select_warm_target)
        monkeypatch.setattr(wt, "acquire_warm_lock", bc.acquire_warm_lock)
        monkeypatch.setattr(wt, "acquire_browser_lock", bc.acquire_browser_lock)
        monkeypatch.setattr(wt, "release_browser_lock", lambda token: None)

        batches = []

        def fake_batch(requests, on_target_start=None):
            requests = list(requests)
            batches.append([(target["name"], force) for target, force in requests])
            for target, _force in requests:
                if on_target_start:
                    on_target_start(target)
            return [(target, {"success": True, "code": "200"}) for target, _force in requests]

        monkeypatch.setattr(wt, "perform_scrape_batch", fake_batch)

        recorded = []

        def fake_record(target, token, success, http_code="", release_lock=True):
            recorded.append((target["name"], success, release_lock))
            return {"next_refresh_at": now + 1200}

        monkeypatch.setattr(wt, "record_warm_result", fake_record)
        releases = []
        monkeypatch.setattr(wt, "release_warm_lock", lambda token: releases.append(token))

        wt.main()

        assert batches == [[("me-universe", False), ("me-general", False)]]
        assert recorded == [
            ("me-universe", True, False),
            ("me-general", True, False),
        ]
        assert len(releases) == 1

    def test_failure_backoff_is_not_pulled_into_pair_lead(self, monkeypatch):
        now = int(time.time())
        target = {"id": "a77ila2000", "type": "universe", "name": "me-universe"}
        sibling_state = {"next_refresh_at": now + 10, "consecutive_failures": 1}
        monkeypatch.setattr(wt, "mget_padded", lambda keys: [json.dumps(sibling_state)])

        sibling, state = wt.select_pair_sibling(target, now)

        assert sibling is None
        assert state == sibling_state

    def test_one_second_ttl_skew_at_lead_boundary_still_pairs(self, monkeypatch):
        # 2026-07-16 live regression: primary was due at now+20 and sibling at now+21.
        # The shared rotation was the same, but the one-second API TTL rounding difference
        # excluded general and delayed it until the next 20-second systemd tick.
        now = int(time.time())
        target = {"id": "a77ila2000", "type": "universe", "name": "me-universe"}
        sibling_state = {"next_refresh_at": now + 21, "consecutive_failures": 0}
        monkeypatch.setattr(wt, "mget_padded", lambda keys: [json.dumps(sibling_state)])

        sibling, state = wt.select_pair_sibling(target, now, primary_due_at=now + 20)

        assert sibling is not None and sibling["name"] == "me-general"
        assert state == sibling_state

    def test_skew_allowance_never_shortens_failure_backoff(self, monkeypatch):
        now = int(time.time())
        target = {"id": "a77ila2000", "type": "universe", "name": "me-universe"}
        sibling_state = {"next_refresh_at": now + 1, "consecutive_failures": 1}
        monkeypatch.setattr(wt, "mget_padded", lambda keys: [json.dumps(sibling_state)])

        sibling, _state = wt.select_pair_sibling(target, now, primary_due_at=now + 20)

        assert sibling is None

    def test_batch_reuses_one_browser_context_for_both_types(self, monkeypatch):
        target_a = {"id": "same", "type": "universe", "name": "same-universe"}
        target_b = {"id": "same", "type": "general", "name": "same-general"}
        shared_context = object()
        calls = {"connect": 0, "new_context": 0, "close": 0, "contexts": [], "url": ""}

        class FakeBrowser:
            def new_context(self, **kwargs):
                calls["new_context"] += 1
                return shared_context

            def close(self):
                calls["close"] += 1

        class FakeChromium:
            def connect_over_cdp(self, url, timeout):
                calls["connect"] += 1
                calls["url"] = url
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakePlaywrightManager:
            def __enter__(self):
                return FakePlaywright()

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(wt, "sync_playwright", lambda: FakePlaywrightManager())
        monkeypatch.setattr(wt, "decrypt_accounts", lambda: [{"id": "same", "password": "pw"}])

        def fake_scrape(context, target, creds, force_scrape, started, budget_seconds):
            calls["contexts"].append(context)
            assert budget_seconds == wt.PAIR_SCRAPE_BUDGET_SECONDS
            if target["type"] == "universe":
                return {"success": False, "code": "transient"}
            return {"success": True, "code": "200"}

        monkeypatch.setattr(wt, "_scrape_target_in_context", fake_scrape)

        results = wt.perform_scrape_batch([(target_a, True), (target_b, True)])

        # A first-target failure is isolated: the second target still runs in the same
        # context and can succeed independently.
        assert [result[1]["success"] for result in results] == [False, True]
        assert calls["connect"] == 1
        assert calls["new_context"] == 1
        assert calls["contexts"] == [shared_context, shared_context]
        assert "timeout=100000" in calls["url"]
        assert calls["close"] == 1


class TestGeneralWorkerFastPath:
    def test_reuses_callback_my_page_without_fixed_waits_or_second_navigation(self, monkeypatch):
        target = {"id": "same", "type": "general", "name": "same-general"}
        navigations = []

        class FakePage:
            def __init__(self):
                # The initial URL shares the m.tworld.co.kr host but is not an authenticated
                # callback. It must still enter credentials without relying on a fixed sleep.
                self.url = "https://m.tworld.co.kr/common/tid/login?target=/v6/my"
                self.waits = []

            def set_default_timeout(self, _timeout):
                pass

            def wait_for_timeout(self, timeout):
                self.waits.append(timeout)

            def close(self):
                pass

        page = FakePage()

        class FakeContext:
            def new_page(self):
                return page

        def fake_goto(_page, url, **_kwargs):
            navigations.append(url)

        def fake_wait_for_result(_page, _timeout, settle_ms=800):
            assert settle_ms == 0
            page.url = "https://m.tworld.co.kr/v6/my"
            return "callback"

        monkeypatch.setattr(wt, "goto_page", fake_goto)
        monkeypatch.setattr(wt, "wait_for_tid_login_form", lambda *a, **k: None)
        submissions = []
        monkeypatch.setattr(wt, "submit_tid_credentials", lambda *a, **k: submissions.append(True))
        monkeypatch.setattr(wt, "wait_for_tworld_result", fake_wait_for_result)
        monkeypatch.setattr(wt, "wait_for_my_ready", lambda *a, **k: None)
        monkeypatch.setattr(
            wt,
            "fetch_tworld_membership_data",
            lambda _page: {"number": "1234567890123456", "seconds_left": 1200, "grade": "V"},
        )
        monkeypatch.setattr(wt, "set_cached_barcode", lambda *a, **k: None)

        result = wt._scrape_target_in_context(
            FakeContext(),
            target,
            {"id": "same", "password": "pw"},
            False,
            time.monotonic(),
            wt.PAIR_SCRAPE_BUDGET_SECONDS,
        )

        assert result == {"success": True, "code": "200"}
        assert navigations == [wt.TWORLD_LOGIN_URL]
        assert submissions == [True]
        assert page.waits == []
