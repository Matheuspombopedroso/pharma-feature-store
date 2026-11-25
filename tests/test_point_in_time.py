"""Point-in-time correctness of ``get_historical_features``.

Two complementary proofs that no future data leaks into training rows:

1. **Sentinel test** (hand-made data): a feature row with an absurd value is
   placed one second *after* a training row's timestamp. If the join ever looks
   forward, the sentinel shows up in the result.
2. **Oracle test** (generated data): every value Feast returns is compared with
   an independent ``pandas.merge_asof(direction="backward")`` join that applies
   the same ttl. Agreement on hundreds of random timestamps (including exact
   boundaries) shows the join picks "latest row at or before t, within ttl".
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from feast import Entity, FeatureView, Field, FileSource
from feast.types import Float64
from feast.value_type import ValueType

from pharma_fs.definitions import ALL_FEATURE_VIEWS
from pharma_fs.store import build_store

SENTINEL = 999_999.0
UTC = "UTC"


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=UTC)


@pytest.fixture()
def sentinel_store(tmp_path):
    """A one-view store over a tiny table that contains a future sentinel."""
    rows = pd.DataFrame(
        {
            "pharmacy_id": ["ph_a", "ph_a", "ph_a", "ph_b"],
            "event_timestamp": [
                _ts("2026-03-01 00:00:00"),
                _ts("2026-03-02 00:00:00"),
                # One second after the training row at 2026-03-03 12:00:00.
                _ts("2026-03-03 12:00:01"),
                _ts("2026-03-01 00:00:00"),
            ],
            "metric": [1.0, 2.0, SENTINEL, 10.0],
        }
    )
    rows["event_timestamp"] = rows["event_timestamp"].astype("datetime64[us, UTC]")
    (tmp_path / "repo").mkdir()
    (tmp_path / "data").mkdir()
    rows.to_parquet(tmp_path / "data" / "metric.parquet", index=False)

    ph = Entity(name="pharmacy", join_keys=["pharmacy_id"], value_type=ValueType.STRING)
    fv = FeatureView(
        name="metric_view",
        entities=[ph],
        ttl=timedelta(days=2),
        schema=[Field(name="metric", dtype=Float64)],
        source=FileSource(
            name="metric_src", path="../data/metric.parquet", timestamp_field="event_timestamp"
        ),
    )
    store = build_store(tmp_path / "repo", tmp_path / "data" / "registry.db")
    store.apply([ph, fv])
    return store


def test_sentinel_from_the_future_never_leaks(sentinel_store):
    entity_df = pd.DataFrame(
        {
            "pharmacy_id": ["ph_a", "ph_a", "ph_a", "ph_a", "ph_b", "ph_a"],
            "event_timestamp": [
                _ts("2026-02-28 23:59:59"),  # before any data      -> null
                _ts("2026-03-01 00:00:00"),  # exact match           -> 1.0 (inclusive)
                _ts("2026-03-02 08:00:00"),  # latest is 03-02       -> 2.0
                _ts("2026-03-03 12:00:00"),  # sentinel is 1s later  -> 2.0, NOT sentinel
                _ts("2026-03-04 00:00:00"),  # ph_b row is 3 days old -> null (ttl 2d)
                _ts("2026-03-03 12:00:01"),  # exactly at sentinel    -> sentinel (now past)
            ],
            "row_id": range(6),
        }
    )
    out = (
        sentinel_store.get_historical_features(entity_df=entity_df, features=["metric_view:metric"])
        .to_df()
        .sort_values("row_id")
    )
    got = out["metric"].tolist()
    assert np.isnan(got[0])
    assert got[1] == 1.0
    assert got[2] == 2.0
    assert got[3] == 2.0, "future sentinel leaked into a training row"
    assert np.isnan(got[4])
    assert got[5] == SENTINEL
    # The sentinel appears only for the training row at/after its timestamp.
    too_early = out["event_timestamp"] < _ts("2026-03-03 12:00:01")
    leaked = out[(out["metric"] == SENTINEL) & too_early]
    assert leaked.empty


SOURCE_TABLE = {
    "ingredient_consumption_7d": "ingredient_consumption",
    "ingredient_consumption_30d": "ingredient_consumption",
    "stock_coverage_days": "stock_coverage",
    "supplier_lead_time": "supplier_lead_time",
    "expiration_days": "expiration",
    "quote_conversion_rate": "quote_conversion",
    "production_queue_size": "production",
    "avg_production_time": "production",
}


def _random_entity_df(world, n: int = 300) -> pd.DataFrame:
    """Random training rows, including exact midnights and out-of-range times."""
    rng = np.random.default_rng(0)
    pairs = world.tables["ingredient_consumption"][["pharmacy_id", "ingredient_id"]]
    pairs = pairs.drop_duplicates().reset_index(drop=True)
    pick = pairs.sample(n, replace=True, random_state=1).reset_index(drop=True)
    times = world.tables["ingredient_consumption"]["event_timestamp"]
    lo, hi = times.min() - pd.Timedelta(days=3), times.max() + pd.Timedelta(days=3)
    secs = rng.uniform(0, (hi - lo).total_seconds(), n)
    ts = lo + pd.to_timedelta(secs, unit="s")
    # A quarter of the rows sit exactly on a snapshot instant (boundary case).
    on_boundary = rng.random(n) < 0.25
    ts = ts.where(~on_boundary, ts.floor("D"))
    pick["event_timestamp"] = pd.Series(ts).dt.floor("us").astype("datetime64[us, UTC]")
    # Feast's file offline store de-duplicates entity rows with identical
    # (join keys, timestamp) - observed with feast 0.68 - so keep them unique
    # to be able to compare row counts one-to-one.
    pick = pick.drop_duplicates(["pharmacy_id", "ingredient_id", "event_timestamp"])
    pick["row_id"] = range(len(pick))
    return pick.reset_index(drop=True)


def _oracle(entity_df: pd.DataFrame, table: pd.DataFrame, keys: list[str], cols: list[str], ttl):
    """Independent PIT join: latest row with ts <= t and ts >= t - ttl."""
    left = entity_df.sort_values("event_timestamp")
    right = table[["event_timestamp", *keys, *cols]].rename(columns={"event_timestamp": "feat_ts"})
    right = right.sort_values("feat_ts")
    merged = pd.merge_asof(
        left,
        right,
        left_on="event_timestamp",
        right_on="feat_ts",
        by=keys,
        direction="backward",
        allow_exact_matches=True,
        tolerance=pd.Timedelta(ttl),
    )
    return merged.sort_values("row_id").reset_index(drop=True)


def test_historical_features_match_backward_asof_oracle(small_world):
    entity_df = _random_entity_df(small_world)
    refs = [f"{fv.name}:{f.name}" for fv in ALL_FEATURE_VIEWS for f in fv.features]
    got = (
        small_world.store.get_historical_features(entity_df=entity_df, features=refs)
        .to_df()
        .sort_values("row_id")
        .reset_index(drop=True)
    )
    assert len(got) == len(entity_df)
    join_key = {"pharmacy": "pharmacy_id", "ingredient": "ingredient_id"}
    checked = 0
    for fv in ALL_FEATURE_VIEWS:
        keys = [join_key[e] for e in fv.entities]
        cols = [f.name for f in fv.features]
        expected = _oracle(entity_df, small_world.tables[SOURCE_TABLE[fv.name]], keys, cols, fv.ttl)
        # Extra proof: the oracle never used a feature row from after the training row.
        used_ts = expected["feat_ts"]
        assert (used_ts.isna() | (used_ts <= expected["event_timestamp"])).all()
        for col in cols:
            a = pd.to_numeric(got[col], errors="coerce").to_numpy(dtype=float)
            b = pd.to_numeric(expected[col], errors="coerce").to_numpy(dtype=float)
            np.testing.assert_allclose(a, b, rtol=0, atol=1e-9, equal_nan=True, err_msg=col)
            checked += int((~np.isnan(b)).sum())
    assert checked > 1000  # the comparison really covered non-null values
