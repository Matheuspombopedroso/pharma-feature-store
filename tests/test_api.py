"""API tests: FastAPI app served from a SQLite-backed store (no Redis needed)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pharma_fs.api import FEATURE_REFS, MAX_BATCH, create_app
from pharma_fs.materialize import run_incremental
from pharma_fs.store import apply_definitions, build_store


@pytest.fixture(scope="module")
def client(small_world, tmp_path_factory):
    """TestClient over a SQLite-backed, fully materialized store."""
    tmp = tmp_path_factory.mktemp("api")
    store = build_store(small_world.root / "feature_repo", tmp / "registry.db")
    apply_definitions(store)
    run_incremental(store)
    with TestClient(create_app(lambda: store)) as c:
        yield c


def _latest(world, table: str, **keys):
    """Latest offline row of ``table`` matching ``keys`` (what online should serve)."""
    t = world.tables[table]
    for k, v in keys.items():
        t = t[t[k] == v]
    return t.sort_values("event_timestamp").iloc[-1]


def test_health(client):
    """Health reports ok and the configured online store type."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "online_store": "sqlite"}


def test_list_features(client):
    """The catalogue endpoint returns exactly the served feature refs."""
    assert client.get("/features").json()["features"] == FEATURE_REFS


def test_single_entity_matches_latest_offline_row(client, small_world):
    """Online values equal the latest offline snapshot for that entity."""
    r = client.get("/features/ph_002/ing_0003")
    assert r.status_code == 200
    f = r.json()["features"]
    assert len(f) == len(FEATURE_REFS)
    cons = _latest(
        small_world, "ingredient_consumption", pharmacy_id="ph_002", ingredient_id="ing_0003"
    )
    assert f["consumption_30d_g"] == pytest.approx(cons["consumption_30d_g"])
    quotes = _latest(small_world, "quote_conversion", pharmacy_id="ph_002")
    assert f["quote_conversion_rate_30d"] == pytest.approx(quotes["quote_conversion_rate_30d"])
    assert isinstance(f["production_queue_size"], int)


def test_unknown_entity_is_404(client):
    """An entity with no features at all yields 404, not a row of nulls."""
    assert client.get("/features/ph_404/ing_9999").status_code == 404


def test_batch_preserves_order_and_nulls_unknown(client):
    """Batch results follow request order; unknown entities get nulls."""
    body = {
        "entities": [
            {"pharmacy_id": "ph_003", "ingredient_id": "ing_0006"},
            {"pharmacy_id": "ph_404", "ingredient_id": "ing_0001"},
            {"pharmacy_id": "ph_001", "ingredient_id": "ing_0002"},
        ]
    }
    r = client.post("/features/batch", json=body)
    assert r.status_code == 200
    res = r.json()["results"]
    assert [(x["pharmacy_id"], x["ingredient_id"]) for x in res] == [
        (e["pharmacy_id"], e["ingredient_id"]) for e in body["entities"]
    ]
    assert res[0]["features"]["consumption_7d_g"] is not None
    assert all(v is None for v in res[1]["features"].values())


def test_batch_validation(client):
    """Empty and oversized batches are rejected with 422."""
    assert client.post("/features/batch", json={"entities": []}).status_code == 422
    too_many = [{"pharmacy_id": "ph_001", "ingredient_id": "ing_0001"}] * (MAX_BATCH + 1)
    assert client.post("/features/batch", json={"entities": too_many}).status_code == 422
