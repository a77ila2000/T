import os, sys, time, json

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))

from playwright.sync_api import sync_playwright
from barcode_core import (
    RedisUnavailable, redis_command, mget_padded, acquire_warm_lock, release_warm_lock,
    select_warm_target, acquire_browser_lock, release_browser_lock,
    decrypt_accounts, set_cached_barcode, record_warm_result, warm_current_key,
    warm_state_key,
    safe_url, get_body_text, goto_page, extract_barcode_number,
    extract_seconds_left, extract_membership_grade, fetch_barcode_data,
    fetch_tworld_membership_data, open_tid_from_my, wait_for_my_ready,
    submit_tid_credentials, wait_for_tid_login_form, wait_for_tid_result,
    wait_for_tworld_result, ensure_idpw_login_mode, open_barcode_view,
    poll_for_fresh_barcode, MY_PAGE_URL, TWORLD_MY_URL, TWORLD_LOGIN_URL,
    MOBILE_USER_AGENT, WARM_TARGETS, WARM_EARLY_LOGIN_LEAD_SECONDS,
)

# One invocation = one tick: pick the most-due target and, only when the other type for that
# same account is due in the same early-login window, process the pair through one Browserless
# connection/context. A systemd timer re-runs this every ~20s (see tworld-worker.timer) instead
# of an in-process `while True` loop - a hung cycle gets killed cleanly by the `timeout` wrapper
# in tworld-worker.service, and a fresh process each tick avoids long-running resource leaks.
# Kept comfortably under WARM_LOCK_TTL/browser lock TTL (130s in barcode_core.py) with margin
# for the systemd `timeout` backstop (110s, see tworld-worker.service) to still fire before
# the lock itself would expire - see the WARM_LOCK_TTL comment in barcode_core.py.
WORKER_SCRAPE_BUDGET_SECONDS = 90
PAIR_SCRAPE_BUDGET_SECONDS = 100
PAIR_BROWSERLESS_TIMEOUT_MS = 100000
BROWSERLESS_WS_URL = os.environ.get("BROWSERLESS_WS_URL", "ws://localhost:3000")
BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "")
HEARTBEAT_KEY = "barcode:worker-heartbeat"
HEARTBEAT_TTL = 120


class ScrapeTimeout(Exception):
    pass


def make_mark(started, budget_seconds=WORKER_SCRAPE_BUDGET_SECONDS):
    def mark(label):
        elapsed = time.monotonic() - started
        print(f"debug elapsed={elapsed:.1f}s stage={label}", flush=True)
        if elapsed > budget_seconds:
            raise ScrapeTimeout(f"scrape exceeded {budget_seconds}s budget at stage={label}")
    return mark


def select_pair_sibling(target, now=None):
    """Return the other barcode type when it is due in the same early-login window."""
    now = int(now or time.time())
    sibling = next(
        (
            dict(candidate)
            for candidate in WARM_TARGETS
            if candidate["id"] == target["id"] and candidate["type"] != target["type"]
        ),
        None,
    )
    if not sibling:
        return None, {}
    raw_state = mget_padded([warm_state_key(sibling["id"], sibling["type"])])[0]
    try:
        state = json.loads(raw_state) if raw_state else {}
    except Exception as exc:
        print(f"pair sibling state parse failed: {exc}", flush=True)
        state = {}
    due_at = int(state.get("next_refresh_at") or 0)
    effective_lead = 0 if int(state.get("consecutive_failures", 0)) > 0 else WARM_EARLY_LOGIN_LEAD_SECONDS
    if due_at > now + effective_lead:
        return None, state
    return sibling, state


def _scrape_target_in_context(context, target, creds, force_scrape, started, budget_seconds):
    # Each target gets a fresh page, while the shared BrowserContext keeps T-ID/SSO cookies
    # from the first type available to the second type. This avoids cross-page DOM state while
    # still eliminating a second Browserless connection and, when T-ID accepts the existing
    # session, a second full credential-login flow.
    account_id, barcode_type = target["id"], target["type"]
    if not creds:
        return {"success": False, "code": "account_not_found"}

    page = None
    stage = "start"
    mark = make_mark(started, budget_seconds)
    try:
        mark("target_start")
        page = context.new_page()
        page.set_default_timeout(6000)

        if barcode_type == "general":
            stage = "open_tworld_login"; mark(stage)
            goto_page(page, TWORLD_LOGIN_URL, timeout=12000)
            page.wait_for_timeout(900)
            mark("after_goto_tworld_login")
            if "m.tworld.co.kr" not in safe_url(page):
                wait_for_tid_login_form(page, 10000)
                stage = "type_tworld_tid_credentials"; mark(stage)
                submit_tid_credentials(page, creds, "tworld")
                mark("after_submit_tworld_tid_credentials")
                result = wait_for_tworld_result(page, 12000)
                if result == "timeout" and "auth.skt-id.co.kr" in safe_url(page) and (time.monotonic() - started) < (budget_seconds - 15):
                    stage = "retry_tworld_idpw_login"; mark(stage)
                    ensure_idpw_login_mode(page)
                    submit_tid_credentials(page, creds, "tworld-retry")
                    result = wait_for_tworld_result(page, 12000)
                    mark("after_retry_tworld_idpw_login")
            else:
                result = "callback"
            print(f"debug tworld login result={result} url={safe_url(page)}", flush=True)
            stage = "open_tworld_my"; mark(stage)
            goto_page(page, TWORLD_MY_URL, timeout=12000)
            wait_for_my_ready(page, 6000)
            stage = "fetch_tworld_membership_data"; mark(stage)
            barcode_api = fetch_tworld_membership_data(page)
            if force_scrape:
                barcode_api = poll_for_fresh_barcode(fetch_tworld_membership_data, page, mark, started, barcode_api, budget_seconds)
            print(f"debug tworld membership api={barcode_api}", flush=True)
            if barcode_api.get("number"):
                grade = barcode_api.get("grade") or extract_membership_grade(page)
                set_cached_barcode(account_id, barcode_api["number"], barcode_api.get("seconds_left", 20 * 60), barcode_type, grade)
                return {"success": True, "code": "200"}
            visible_number = extract_barcode_number(page)
            visible_seconds = extract_seconds_left(page)
            visible_grade = extract_membership_grade(page)
            if visible_number:
                set_cached_barcode(account_id, visible_number, visible_seconds, barcode_type, visible_grade)
                return {"success": True, "code": "200"}
            return {"success": False, "code": str(barcode_api.get("code") or "tworld_barcode_not_found")}

        stage = "open_tid_from_my"; mark(stage)
        open_tid_from_my(page, mark)
        mark("after_open_tid_from_my")
        wait_for_tid_login_form(page, 8000)
        stage = "type_tid_credentials"; mark(stage)
        submit_tid_credentials(page, creds)
        mark("after_submit_tid_credentials")
        result = wait_for_tid_result(page, 10000)
        if result == "timeout" and "auth.skt-id.co.kr" in safe_url(page) and (time.monotonic() - started) < (budget_seconds - 15):
            stage = "retry_tid_idpw_login"; mark(stage)
            ensure_idpw_login_mode(page)
            submit_tid_credentials(page, creds, "retry")
            result = wait_for_tid_result(page, 10000)
            mark("after_retry_tid_idpw_login")
        print(f"debug tid submit result={result} url={safe_url(page)}", flush=True)
        stage = "open_my_after_login"; mark(stage)
        goto_page(page, MY_PAGE_URL, timeout=9000)
        wait_for_my_ready(page, 5000)
        stage = "fetch_barcode_data"; mark(stage)
        barcode_api = fetch_barcode_data(page)
        if force_scrape:
            barcode_api = poll_for_fresh_barcode(fetch_barcode_data, page, mark, started, barcode_api, budget_seconds)
        print(f"debug barcode api={barcode_api}", flush=True)
        if barcode_api.get("number"):
            set_cached_barcode(account_id, barcode_api["number"], barcode_api.get("seconds_left", 20 * 60), barcode_type)
            return {"success": True, "code": "200"}
        if barcode_api.get("code") in ["MSG0115", "MSG0116", "MSG0118", "MSG0120", "MSG0998"]:
            return {"success": False, "code": barcode_api.get("code")}

        stage = "open_barcode_view"; mark(stage)
        barcode_result = open_barcode_view(page, started + budget_seconds - 8)
        barcode_number = ""
        for _ in range(10):
            barcode_number = extract_barcode_number(page)
            if barcode_number:
                break
            page.wait_for_timeout(300)
        seconds_left = extract_seconds_left(page)
        print(f"debug barcode open result={barcode_result} number={barcode_number} seconds={seconds_left} url={safe_url(page)}", flush=True)
        if barcode_number:
            set_cached_barcode(account_id, barcode_number, seconds_left)
            return {"success": True, "code": "200"}
        return {"success": False, "code": "barcode_not_found"}
    except ScrapeTimeout as exc:
        print(f"Scrape timed out for {account_id}/{barcode_type} at {stage}: {exc}", flush=True)
        return {"success": False, "code": "timeout"}
    except Exception as exc:
        print(f"Error processing {account_id}/{barcode_type} at {stage}: {type(exc).__name__}: {exc}", flush=True)
        return {"success": False, "code": "error"}
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def perform_scrape_batch(requests, on_target_start=None):
    """Scrape one or two same-account targets through one Browserless context."""
    requests = list(requests)
    if not requests:
        return []
    accounts = decrypt_accounts()
    started = time.monotonic()
    budget_seconds = PAIR_SCRAPE_BUDGET_SECONDS if len(requests) > 1 else WORKER_SCRAPE_BUDGET_SECONDS
    browser_timeout_ms = PAIR_BROWSERLESS_TIMEOUT_MS if len(requests) > 1 else 60000
    browser = None
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"{BROWSERLESS_WS_URL}?token={BROWSERLESS_TOKEN}&stealth=true&timeout={browser_timeout_ms}",
                timeout=20000,
            )
            context = browser.new_context(
                viewport={"width": 412, "height": 915},
                user_agent=MOBILE_USER_AGENT,
                is_mobile=True,
                has_touch=True,
            )
            for target, force_scrape in requests:
                if on_target_start:
                    on_target_start(target)
                creds = next((account for account in accounts if account["id"] == target["id"]), None)
                result = _scrape_target_in_context(
                    context,
                    target,
                    creds,
                    force_scrape,
                    started,
                    budget_seconds,
                )
                results.append((target, result))
                print(
                    f"batch target complete name={target['name']} success={result['success']} "
                    f"code={result.get('code')} elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
    except Exception as exc:
        print(f"worker batch failed: {type(exc).__name__}: {exc}", flush=True)
        for target, _force_scrape in requests[len(results):]:
            results.append((target, {"success": False, "code": "batch_error"}))
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return results


def perform_scrape(target, force_scrape=False):
    # Compatibility wrapper retained for focused tests and one-off diagnostics.
    results = perform_scrape_batch([(target, force_scrape)])
    return results[0][1] if results else {"success": False, "code": "batch_error"}


def publish_current_target(target, lock_token):
    redis_command(["SET", warm_current_key(), json.dumps({
        "token": lock_token,
        "started_at": int(time.time()),
        "id": target["id"],
        "type": target["type"],
        "name": target["name"],
    }), "EX", 130])


def main():
    now = int(time.time())
    # Peek for a due target BEFORE acquiring the warm lock - this runs every ~20s regardless
    # of whether anything's actually due, and the vast majority of ticks find nothing to do
    # (each target is only due once per ~20min cycle). Acquiring+releasing the warm lock on
    # every idle tick costs 4 extra Redis commands on top of this MGET (SET NX + GET + DEL +
    # DEL) for zero benefit, and at a 20s cadence that alone was on pace to blow past Upstash's
    # free-tier monthly command quota. An idle tick now costs exactly this one MGET.
    try:
        target, state = select_warm_target(now)
    except RedisUnavailable:
        print("Redis unavailable - skipping this tick rather than guessing a target", flush=True)
        return

    if not target:
        print(f"no_due now={now}", flush=True)
        return

    # Explicit 130s TTL (barcode_core's default, but spelled out here since Vercel's own call
    # site in get_barcode.py deliberately uses a shorter 75s - see that file's comment) so this
    # lock can't expire while this worker's own up-to-110s run is still legitimately in flight.
    lock_token = acquire_warm_lock(ttl=130)
    if not lock_token:
        print("warm lock busy - another tick (Vercel or this worker) is running, skipping", flush=True)
        return

    # Re-check after acquiring the lock: the peek above ran lock-free, so another process
    # (Vercel, or this same worker's own next tick under a race) could have already claimed
    # and finished this exact target in the gap between the peek and actually getting the
    # lock. Re-selecting guarantees we act on current state, not a stale read.
    try:
        target, state = select_warm_target(now)
    except RedisUnavailable:
        release_warm_lock(lock_token)
        return
    if not target:
        release_warm_lock(lock_token)
        return

    selection_now = int(time.time())
    requests = [(target, int(state.get("next_refresh_at") or 0) > selection_now)]
    try:
        sibling, sibling_state = select_pair_sibling(target, selection_now)
    except RedisUnavailable:
        # Primary selection was already made from a successful MGET. If the additional
        # sibling read fails, keep the known-safe single-target path rather than guessing.
        print("Redis unavailable while checking pair sibling - running primary only", flush=True)
        sibling, sibling_state = None, {}
    if sibling:
        requests.append((sibling, int(sibling_state.get("next_refresh_at") or 0) > selection_now))
        print(f"paired refresh selected primary={target['name']} sibling={sibling['name']}", flush=True)

    browser_lock_token = acquire_browser_lock(target["id"], ttl=130)
    if not browser_lock_token:
        print(f"browser lock busy - skipping {target['name']} this tick", flush=True)
        release_warm_lock(lock_token)
        return

    results = []
    try:
        results = perform_scrape_batch(
            requests,
            on_target_start=lambda active_target: publish_current_target(active_target, lock_token),
        )
    except Exception as exc:
        print(f"worker_tick batch failed: {type(exc).__name__}: {exc}", flush=True)
        results = [
            (requested_target, {"success": False, "code": "error"})
            for requested_target, _force_scrape in requests
        ]
    finally:
        release_browser_lock(browser_lock_token)

    try:
        for index, (completed_target, result) in enumerate(results, start=1):
            completed_state = record_warm_result(
                completed_target,
                lock_token,
                result["success"],
                result.get("code", ""),
                release_lock=False,
            )
            print(
                f"done id={completed_target['id']} type={completed_target['type']} "
                f"name={completed_target['name']} success={result['success']} "
                f"code={result.get('code')} next_refresh_at={completed_state['next_refresh_at']} "
                f"batch={index}/{len(results)}",
                flush=True,
            )
    finally:
        # Keep the global warm lock for the whole pair, then release it exactly once after
        # both target states have been committed. Browser concurrency remains one.
        release_warm_lock(lock_token)

    last_target = results[-1][0]["name"] if results else target["name"]
    all_success = bool(results) and all(result["success"] for _item, result in results)
    redis_command(["SET", HEARTBEAT_KEY, json.dumps({
        "at": int(time.time()),
        "last_target": last_target,
        "last_success": all_success,
        "batch_size": len(results),
    }), "EX", HEARTBEAT_TTL])


if __name__ == "__main__":
    main()
