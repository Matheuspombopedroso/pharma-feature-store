"""Integration test against a real Redis (skipped when none is reachable).

Uses logical database 15 so it never overwrites the demo data in db 0.
CI starts a Redis service container, so this test runs there too.
"""

from __future__ import annotations

import os

import pytest
import redis

from pharma_fs.materialize import run_incremental
from pharma_fs.store import apply_definitions, build_store

HOST_PORT = os.environ.get("REDIS_CONNECTION_STRING", "localhost:6379").split(",")[0]


def _redis_up() -> bool:
    host, port = HOST_PORT.split(":")
    try:
        return bool(redis.Redis(host=host, port=int(port), socket_connect_timeout=0.5).ping())
    except redis.exceptions.RedisError:
        return False


@pytest.mark.redis
@pytest.mark.skipif(not _redis_up(), reason=f"no Redis at {HOST_PORT}")
def test_materialize_and_read_back_from_redis(small_world, tmp_path):
    host, port = HOST_PORT.split(":")
    redis.Redis(host=host, port=int(port), db=15).flushdb()
    store = build_store(
        small_world.root / "feature_repo",
        tmp_path / "registry.db",
        online_store={"type": "redis", "connection_string": f"{HOST_PORT},db=15"},
    )
    apply_definitions(store)
    run_incremental(store)
    resp = store.get_online_features(
        features=[
            "stock_coverage_days:stock_coverage_days",
            "production_queue_size:production_queue_size",
        ],
        entity_rows=[{"pharmacy_id": "ph_001", "ingredient_id": "ing_0001"}],
    ).to_dict()
    t = small_world.tables["stock_coverage"]
    last = t[(t.pharmacy_id == "ph_001") & (t.ingredient_id == "ing_0001")].iloc[-1]
    assert resp["stock_coverage_days"][0] == pytest.approx(last["stock_coverage_days"])
    assert resp["production_queue_size"][0] is not None
    # One hash per entity key: 3 pharmacies x 6 ingredients + 3 pharmacy-only keys.
    assert redis.Redis(host=host, port=int(port), db=15).dbsize() == 3 * 6 + 3
