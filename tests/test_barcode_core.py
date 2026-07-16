"""
Regression tests for api/barcode_core.py covering behaviors that were bugs at some point
this project's history - each test name references the specific incident it guards against.
Run with: pip install pytest && pytest tests/ -v
No network/Redis/browser access required - everything is mocked.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
import barcode_core as bc  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_redis(monkeypatch):
    """Every test gets a fresh fake Redis store instead of touching real network calls."""
    store = {}

    def fake_redis_command(command, timeout=4):
        op = command[0]
        if op == "MGET":
            return [store.get(k) for k in command[1:]]
        if op == "GET":
            return store.get(command[1])
        if op == "SET":
            key, value = command[1], command[2]
            store[key] = value
            return "OK"
        if op == "DEL":
            store.pop(command[1], None)
            return 1
        return None

    monkeypatch.setattr(bc, "redis_command", fake_redis_command)
    return store


class TestParseSecondsLeft:
    def test_none_uses_default(self):
        assert bc.parse_seconds_left(None) == 20 * 60

    def test_real_zero_is_preserved(self):
        # 2026-07-16 bug: `int(x or default)` treated a genuine 0 (barcode exactly at its
        # rotation boundary) the same as a missing field.
        assert bc.parse_seconds_left(0) == 0

    def test_garbage_falls_back_to_default(self):
        assert bc.parse_seconds_left("not-a-number") == 20 * 60


class TestSetCachedBarcodeTTL:
    def test_real_zero_seconds_left_gets_minimum_ttl_not_default(self, _isolate_redis):
        value = bc.set_cached_barcode("a77ila2000", "1234567890123456", 0, "universe", "")
        assert value["expires_at"] - time.time() < 5  # ~1s TTL, not silently 1200s


class TestMgetPadded:
    def test_redis_failure_raises_instead_of_all_none(self, monkeypatch):
        # 2026-07-16 bug: a Redis outage silently became an all-None list, indistinguishable
        # from "every key genuinely has no value" - select_warm_target would then treat every
        # target as overdue and launch unnecessary login scrapes during an infra hiccup.
        monkeypatch.setattr(bc, "redis_command", lambda *a, **k: None)
        with pytest.raises(bc.RedisUnavailable):
            bc.mget_padded(["some-key"])


class TestSelectWarmTargetEarlyLead:
    def test_success_schedule_gets_early_lead(self, _isolate_redis):
        now = int(time.time())
        for t in bc.WARM_TARGETS:
            far = json.dumps({"next_refresh_at": now + 5000})
            _isolate_redis[bc.warm_state_key(t["id"], t["type"])] = far
        _isolate_redis[bc.warm_state_key("a77ila2000", "universe")] = json.dumps(
            {"next_refresh_at": now + 15}
        )
        target, _ = bc.select_warm_target(now)
        assert target is not None and target["name"] == "me-universe"

    def test_lead_includes_timer_ttl_skew_and_login_margin(self, _isolate_redis):
        # The live TTL estimate drifted ~12s from rotation in addition to the 20s timer.
        # A 60s window leaves a real pre-login margin without increasing tick frequency.
        now = int(time.time())
        for t in bc.WARM_TARGETS:
            _isolate_redis[bc.warm_state_key(t["id"], t["type"])] = json.dumps(
                {"next_refresh_at": now + 5000}
            )
        _isolate_redis[bc.warm_state_key("a77ila2000", "universe")] = json.dumps(
            {"next_refresh_at": now + 55}
        )

        target, _ = bc.select_warm_target(now)

        assert bc.WARM_EARLY_LOGIN_LEAD_SECONDS == 60
        assert target is not None and target["name"] == "me-universe"

    def test_failure_backoff_does_not_get_early_lead(self, _isolate_redis):
        # 2026-07-16 bug: the 20s early-lead window was also applied on top of failure
        # backoff delays, letting a 30s backoff be selected again after only ~10s.
        now = int(time.time())
        for t in bc.WARM_TARGETS:
            far = json.dumps({"next_refresh_at": now + 5000})
            _isolate_redis[bc.warm_state_key(t["id"], t["type"])] = far
        _isolate_redis[bc.warm_state_key("a77ila10004", "universe")] = json.dumps(
            {"next_refresh_at": now + 15, "consecutive_failures": 2}
        )
        target, _ = bc.select_warm_target(now)
        assert target is None or target["name"] != "mother-universe"


class TestRecordWarmResult:
    def test_success_resets_consecutive_failures(self, _isolate_redis):
        target = {"id": "a77ila2000", "type": "universe", "name": "me-universe"}
        _isolate_redis[bc.warm_state_key(target["id"], target["type"])] = json.dumps(
            {"consecutive_failures": 3, "next_refresh_at": int(time.time())}
        )
        state = bc.record_warm_result(target, "tok", success=True, http_code="200")
        assert state.get("consecutive_failures") == 0

    def test_batch_caller_can_defer_warm_lock_release(self, _isolate_redis, monkeypatch):
        target = {"id": "a77ila2000", "type": "universe", "name": "me-universe"}
        releases = []
        monkeypatch.setattr(bc, "release_warm_lock", lambda token: releases.append(token))

        bc.record_warm_result(
            target,
            "pair-token",
            success=True,
            http_code="200",
            release_lock=False,
        )

        assert releases == []

    def test_failure_backoff_schedule_is_bounded(self, _isolate_redis):
        target = {"id": "a77ila2000", "type": "universe", "name": "me-universe"}
        delays = []
        for _ in range(7):
            state = bc.record_warm_result(target, "tok", success=False, http_code="502")
            delays.append(state["consecutive_failures"])
            _isolate_redis[bc.warm_state_key(target["id"], target["type"])] = json.dumps(state)
        # consecutive_failures should just keep counting up...
        assert delays == [1, 2, 3, 4, 5, 6, 7]
        # ...but the actual delay caps at the schedule's last entry (300s) rather than growing forever.
        last_delay, _ = bc.compute_failure_retry_delay({"consecutive_failures": 6})
        assert last_delay == bc.FAILURE_BACKOFF_SCHEDULE[-1] == 300


class TestSubmitTidCredentialsStopsOnceNavigatedAway:
    def test_no_further_actions_after_successful_navigation(self, monkeypatch):
        # 2026-07-16 bug: submit_tid_credentials() fired a fixed ladder of fallback submit
        # actions with no check for whether an earlier one had already succeeded and
        # navigated away - the last fallback (force_submit's own loose "click the first big
        # visible button") landed on an unrelated "실시간 이용요금" link on the new page,
        # breaking an already-successful login.
        log = []

        class FakeLocator:
            @property
            def first(self):
                return self

            @property
            def last(self):
                return self

            def press(self, *a, **k):
                log.append("press")

            def click(self, *a, **k):
                log.append("click")

            def wait_for(self, *a, **k):
                pass

            def scroll_into_view_if_needed(self, *a, **k):
                pass

            def fill(self, *a, **k):
                pass

            def type(self, *a, **k):
                pass

            def is_visible(self, *a, **k):
                return True

        class FakePage:
            def __init__(self, urls):
                self._urls = urls
                self._i = 0

            @property
            def url(self):
                idx = min(self._i, len(self._urls) - 1)
                self._i += 1
                return self._urls[idx]

            def locator(self, _sel):
                return FakeLocator()

            def evaluate(self, *a, **k):
                log.append("js-evaluate")
                return "clicked-something"

            def wait_for_timeout(self, *a, **k):
                pass

            def is_closed(self):
                return False

        monkeypatch.setattr(bc, "type_first_visible", lambda *a, **k: "ok")
        monkeypatch.setattr(bc, "ensure_idpw_login_mode", lambda p: None)

        # index 0: still on the login page (first check, right after Enter press).
        # index 1+: navigated away - simulating the js-evaluate DOM click having succeeded.
        urls = ["https://auth.skt-id.co.kr/v2/login"] + ["https://m.tworld.co.kr/common/member/line"] * 20
        page = FakePage(urls)

        bc.submit_tid_credentials(page, {"id": "me", "password": "x"}, "test")

        assert log.count("js-evaluate") == 1
        assert not any(entry == "click" for entry in log)


class TestWaitForTidResult:
    class FakePage:
        def __init__(self, url):
            self.url = url
            self.waits = []

        def is_closed(self):
            return False

        def wait_for_timeout(self, timeout_ms):
            self.waits.append(timeout_ms)

    def test_universe_home_is_a_completed_callback(self):
        # The real T ID flow sometimes strips the callback path and lands on `/` after a
        # successful login.  Waiting for `/my` in that case cost every refresh 10 seconds.
        page = self.FakePage("https://m.sktuniverse.co.kr/")

        assert bc.wait_for_tid_result(page, 50) == "callback"
        assert page.waits == [800]

    @pytest.mark.parametrize(
        "url",
        [
            "https://m.sktuniverse.co.kr/member/login/view?loginRedirectUrl=%2Fmy",
            "https://m.sktuniverse.co.kr/netfunnel?path=%2Fmember%2Flogin%2Fview",
        ],
    )
    def test_login_and_netfunnel_pages_are_not_false_successes(self, monkeypatch, url):
        page = self.FakePage(url)
        monkeypatch.setattr(bc, "get_body_text", lambda *a, **k: "login")

        assert bc.wait_for_tid_result(page, 1) == "timeout"
        assert page.waits == []


class TestWaitForTworldResult:
    class FakePage:
        url = "https://m.tworld.co.kr/v6/my"

        def __init__(self):
            self.waits = []

        def wait_for_timeout(self, timeout_ms):
            self.waits.append(timeout_ms)

    def test_worker_can_delegate_settle_to_my_ready_check(self):
        page = self.FakePage()

        assert bc.wait_for_tworld_result(page, 50, settle_ms=0) == "callback"
        assert page.waits == []

    def test_default_settle_is_preserved_for_other_callers(self):
        page = self.FakePage()

        assert bc.wait_for_tworld_result(page, 50) == "callback"
        assert page.waits == [800]


class TestFetchTworldMembershipData:
    def test_uses_the_real_api_endpoint_and_session_headers(self):
        # 2026-07-16: `/common/my/tmembership` (no /api/v6 prefix) always 404s - it isn't a
        # real endpoint. Guard against silently reverting to it.
        captured = {}

        class FakePage:
            def evaluate(self, script):
                captured["script"] = script
                return {
                    "ok": True,
                    "status": 200,
                    "body": {
                        "respCode": 0,
                        "respMsg": "SUCCESS",
                        "data": {
                            "mbrTypCd": "01",
                            "mbrGrCd": "V",
                            "mbrStCd": "AC",
                            "otbNum": "2140504550712284",
                            "expireSeconds": 1197,
                        },
                    },
                }

        result = bc.fetch_tworld_membership_data(FakePage())
        assert "/api/v6/common/my/tmembership" in captured["script"]
        assert "SessionUpdatedAt" in captured["script"]
        assert result["number"] == "2140504550712284"
        assert result["seconds_left"] == 1197

    def test_404_is_not_treated_as_success(self):
        class FakePage:
            def evaluate(self, script):
                return {"ok": False, "status": 404, "text": "<html>not found</html>"}

        result = bc.fetch_tworld_membership_data(FakePage())
        assert not result.get("number")


class TestWarmStatusMgetDedup:
    def test_legacy_key_fetched_once_per_account_not_per_target(self, monkeypatch):
        captured = {}

        def capture(command, timeout=4):
            if command[0] == "MGET":
                captured["keys"] = command[1:]
                return [None] * (len(command) - 1)
            return None

        monkeypatch.setattr(bc, "redis_command", capture)
        bc.mget_padded(
            [bc.warm_current_key()]
            + [bc.warm_state_key(t["id"], t["type"]) for t in bc.WARM_TARGETS]
            + [bc.cache_key(t["id"], t["type"]) for t in bc.WARM_TARGETS]
            + list(dict.fromkeys(f"barcode:{t['id']}" for t in bc.WARM_TARGETS))
        )
        assert len(captured["keys"]) == 16
        assert len(set(captured["keys"])) == 16
