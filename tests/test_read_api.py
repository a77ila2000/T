import json
from types import SimpleNamespace

import pytest

from oracle import read_api as api


@pytest.fixture
def client():
    api.app.config.update(TESTING=True)
    return api.app.test_client()


def test_healthz_has_cors_headers(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json()["service"] == "tworld-read-api"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "X-Barcode-Number" in response.headers["Access-Control-Expose-Headers"]


def test_get_barcode_returns_cached_svg_and_metadata(monkeypatch, client):
    monkeypatch.setattr(api, "time", SimpleNamespace(time=lambda: 1_000.0))
    redis_calls = []

    def fake_mget(keys):
        redis_calls.append(list(keys))
        return [json.dumps({
            "number": "1234567890123456",
            "expires_at": 1_120.0,
            "grade": "VIP",
        }), None]

    monkeypatch.setattr(api, "mget_padded", fake_mget)
    response = client.get("/api/get_barcode?id=a77ila2000&type=universe")

    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert b"1234567890123456" in response.data
    assert b"<rect" in response.data
    assert response.headers["X-Barcode-Number"] == "1234567890123456"
    assert response.headers["X-Barcode-Seconds-Left"] == "120"
    assert response.headers["X-Barcode-Stale"] == "0"
    assert response.headers["X-Membership-Grade"] == "VIP"
    assert redis_calls == [["barcode:universe:a77ila2000", "barcode:a77ila2000"]]


def test_get_barcode_returns_stale_cache_instead_of_scraping(monkeypatch, client):
    monkeypatch.setattr(api, "time", SimpleNamespace(time=lambda: 1_000.0))
    monkeypatch.setattr(api, "mget_padded", lambda keys: [json.dumps({
        "number": "1234567890123456",
        "expires_at": 970.0,
    })])

    response = client.get("/api/get_barcode?id=a77ila2000&type=general")

    assert response.status_code == 200
    assert response.headers["X-Barcode-Seconds-Left"] == "0"
    assert response.headers["X-Barcode-Stale"] == "1"
    assert response.headers["X-Barcode-Stale-Seconds"] == "30"


def test_get_barcode_fails_closed_when_redis_is_unavailable(monkeypatch, client):
    def fail_mget(keys):
        raise api.RedisUnavailable("offline")

    monkeypatch.setattr(api, "mget_padded", fail_mget)
    response = client.get("/api/get_barcode?id=a77ila2000&type=general")

    assert response.status_code == 503
    assert response.get_json()["status"] == "redis_unavailable"


def test_warm_status_uses_one_mget_for_all_targets(monkeypatch, client):
    monkeypatch.setattr(api, "time", SimpleNamespace(time=lambda: 1_000.0))
    captured_keys = []

    def fake_mget(keys):
        captured_keys.extend(keys)
        target_count = len(api.WARM_TARGETS)
        states = [json.dumps({"next_refresh_at": 1_100, "last_success_at": 900})]
        states.extend([None] * (target_count - 1))
        caches = [json.dumps({"number": "1234567890123456", "expires_at": 1_120})]
        caches.extend([None] * (target_count - 1))
        legacy = [None] * len({target["id"] for target in api.WARM_TARGETS})
        return [None, *states, *caches, *legacy]

    monkeypatch.setattr(api, "mget_padded", fake_mget)
    response = client.get("/api/warm_status")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert len(payload["targets"]) == len(api.WARM_TARGETS)
    assert payload["targets"][0]["has_cache"] is True
    assert payload["targets"][0]["seconds_left"] == 120
    assert len(captured_keys) == 1 + len(api.WARM_TARGETS) * 2 + 3
