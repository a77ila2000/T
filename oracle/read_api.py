import json
import os
import re
import sys
import time
from functools import lru_cache

from flask import Flask, Response, request


API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from barcode_core import (  # noqa: E402
    RedisUnavailable,
    WARM_TARGETS,
    cache_key,
    mget_padded,
    normalize_barcode_type,
    warm_current_key,
    warm_state_key,
)


app = Flask(__name__)

CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]

EXPOSED_HEADERS = ", ".join([
    "X-Barcode-Number",
    "X-Barcode-Seconds-Left",
    "X-Barcode-Stale",
    "X-Barcode-Status",
    "X-Barcode-Stale-Seconds",
    "X-Membership-Grade",
])
KNOWN_ACCOUNTS = {target["id"] for target in WARM_TARGETS}


@app.after_request
def add_response_headers(response):
    # The Vercel-hosted static page calls this API directly. Exposing the custom
    # barcode headers is required before browser JavaScript can read them cross-origin.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Expose-Headers"] = EXPOSED_HEADERS
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def json_response(payload, status=200):
    return Response(json.dumps(payload, ensure_ascii=False), status=status, mimetype="application/json")


def _parse_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def read_cached_barcode(account_id, barcode_type, now=None):
    barcode_type = normalize_barcode_type(barcode_type)
    keys = [cache_key(account_id, barcode_type)]
    if barcode_type == "universe":
        keys.append(f"barcode:{account_id}")
    values = mget_padded(keys)
    value = _parse_json(next((raw for raw in values if raw), None))
    if not value or not value.get("number"):
        return None

    now = time.time() if now is None else float(now)
    expires_at = float(value.get("expires_at") or 0)
    value["seconds_left"] = max(0, int(expires_at - now))
    value["stale"] = expires_at <= now + 5
    value["stale_seconds"] = max(0, int(now - expires_at))
    return value


def _encode_code128_c(digits):
    if len(digits) % 2:
        codes = [105]
        codes.extend(int(digits[index:index + 2]) for index in range(0, len(digits) - 1, 2))
        codes.extend([100, ord(digits[-1]) - 32])
    else:
        codes = [105]
        codes.extend(int(digits[index:index + 2]) for index in range(0, len(digits), 2))
    checksum = codes[0] + sum(value * index for index, value in enumerate(codes[1:], 1))
    return [*codes, checksum % 103, 106]


@lru_cache(maxsize=32)
def render_barcode_svg(number):
    digits = re.sub(r"\D", "", str(number))
    if not digits:
        raise ValueError("barcode number is empty")

    module = 3
    quiet = 30
    bar_height = 150
    text_height = 46
    codes = _encode_code128_c(digits)
    width = quiet * 2 + sum(sum(int(part) for part in CODE128_PATTERNS[code]) for code in codes) * module
    x = quiet
    rectangles = []
    for code in codes:
        for index, part in enumerate(CODE128_PATTERNS[code]):
            bar_width = int(part) * module
            if index % 2 == 0:
                rectangles.append(f'<rect x="{x}" y="18" width="{bar_width}" height="{bar_height}"/>')
            x += bar_width

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{bar_height + text_height}" '
        f'viewBox="0 0 {width} {bar_height + text_height}" role="img" aria-label="Barcode {digits}">'
        '<rect width="100%" height="100%" fill="white"/>'
        f'<g fill="black">{"".join(rectangles)}</g>'
        f'<text x="50%" y="190" text-anchor="middle" font-family="Arial,sans-serif" font-size="22">{digits}</text>'
        '</svg>'
    )


def barcode_response(cached):
    number = re.sub(r"\D", "", str(cached.get("number") or ""))
    if not number:
        return json_response({"status": "invalid_cache"}, status=502)
    response = Response(render_barcode_svg(number), mimetype="image/svg+xml")
    response.headers["X-Barcode-Number"] = number
    response.headers["X-Barcode-Seconds-Left"] = str(max(0, int(cached.get("seconds_left") or 0)))
    response.headers["X-Barcode-Stale"] = "1" if cached.get("stale") else "0"
    response.headers["X-Barcode-Status"] = "stale" if cached.get("stale") else "valid"
    response.headers["X-Barcode-Stale-Seconds"] = str(max(0, int(cached.get("stale_seconds") or 0)))
    if cached.get("grade"):
        response.headers["X-Membership-Grade"] = str(cached["grade"])
    return response


@app.route("/healthz", methods=["GET"])
def healthz():
    return json_response({"status": "ok", "service": "tworld-read-api"})


@app.route("/api/get_barcode", methods=["GET"])
def get_barcode():
    account_id = request.args.get("id", "")
    if account_id not in KNOWN_ACCOUNTS:
        return json_response({"status": "unknown_account"}, status=404)
    barcode_type = normalize_barcode_type(request.args.get("type"))
    try:
        cached = read_cached_barcode(account_id, barcode_type)
    except RedisUnavailable:
        return json_response({"status": "redis_unavailable"}, status=503)
    if not cached:
        return json_response({"status": "cache_missing"}, status=404)
    return barcode_response(cached)


@app.route("/api/warm_status", methods=["GET"])
def warm_status():
    now = int(time.time())
    state_keys = [warm_state_key(target["id"], target["type"]) for target in WARM_TARGETS]
    cache_keys = [cache_key(target["id"], target["type"]) for target in WARM_TARGETS]
    account_ids = list(dict.fromkeys(target["id"] for target in WARM_TARGETS))
    legacy_keys = [f"barcode:{account_id}" for account_id in account_ids]
    all_keys = [warm_current_key(), *state_keys, *cache_keys, *legacy_keys]
    try:
        raw_values = mget_padded(all_keys)
    except RedisUnavailable:
        return json_response({"status": "redis_unavailable"}, status=503)

    target_count = len(WARM_TARGETS)
    current = _parse_json(raw_values[0])
    state_values = raw_values[1:1 + target_count]
    cache_values = raw_values[1 + target_count:1 + 2 * target_count]
    legacy_values = raw_values[1 + 2 * target_count:]
    legacy_by_id = dict(zip(account_ids, legacy_values))

    targets = []
    for index, target in enumerate(WARM_TARGETS):
        state = _parse_json(state_values[index]) or {}
        raw_cache = cache_values[index]
        if not raw_cache and target["type"] == "universe":
            raw_cache = legacy_by_id.get(target["id"])
        cached = _parse_json(raw_cache)
        seconds_left = 0
        stale_seconds = 0
        stale = False
        if cached:
            expires_at = float(cached.get("expires_at") or 0)
            seconds_left = max(0, int(expires_at - now))
            stale = expires_at <= now + 5
            stale_seconds = max(0, int(now - expires_at))

        targets.append({
            "id": target["id"],
            "type": target["type"],
            "name": target["name"],
            "next_refresh_at": int(state.get("next_refresh_at") or 0),
            "last_success_at": int(state.get("last_success_at") or 0),
            "last_failure_at": int(state.get("last_failure_at") or 0),
            "has_cache": bool(cached),
            "stale": stale,
            "seconds_left": seconds_left,
            "stale_seconds": stale_seconds,
        })
    return json_response({"status": "ok", "now": now, "current": current, "targets": targets})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
