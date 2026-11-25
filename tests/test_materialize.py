"""Incremental materialization tests (SQLite online store, no Redis needed)."""

from __future__ import annotations

import pandas as pd

from pharma_fs.materialize import run_incremental, simulation_end
from pharma_fs.store import apply_definitions, build_store


def _online_7d(store, ph: str, ing: str) -> float:
    resp = store.get_online_features(
        features=["ingredient_consumption_7d:consumption_7d_g"],
        entity_rows=[{"pharmacy_id": ph, "ingredient_id": ing}],
    ).to_dict()
    return resp["consumption_7d_g"][0]


def _offline_7d(world, ph: str, ing: str, at: pd.Timestamp) -> float:
    t = world.tables["ingredient_consumption"]
    row = t[(t.pharmacy_id == ph) & (t.ingredient_id == ing) & (t.event_timestamp <= at)]
    return float(row.sort_values("event_timestamp").iloc[-1]["consumption_7d_g"])


def test_incremental_runs_cover_consecutive_intervals(small_world, tmp_path):
    # A fresh registry + online DB over the shared small dataset.
    store = build_store(small_world.root / "feature_repo", tmp_path / "registry.db")
    apply_definitions(store)

    mid = pd.Timestamp("2026-02-01", tz="UTC")
    first = run_incremental(store, mid.to_pydatetime())
    # Online value == latest offline row at or before the first end date.
    assert _online_7d(store, "ph_001", "ing_0001") == _offline_7d(
        small_world, "ph_001", "ing_0001", mid
    )

    end = simulation_end()
    second = run_incremental(store, end)
    for name, (start, stop) in second.intervals.items():
        # The second run starts exactly where the first one stopped.
        assert start == first.intervals[name][1], name
        assert stop == end, name
    assert _online_7d(store, "ph_001", "ing_0001") == _offline_7d(
        small_world, "ph_001", "ing_0001", pd.Timestamp(end)
    )


def test_unknown_entity_returns_none(small_world, tmp_path):
    store = build_store(small_world.root / "feature_repo", tmp_path / "registry.db")
    apply_definitions(store)
    run_incremental(store)
    assert _online_7d(store, "ph_404", "ing_0001") is None
