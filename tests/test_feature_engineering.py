"""Leakage tests for the feature engineering step (upstream of Feast).

Feast's point-in-time join is only as good as the timestamps it receives. These
tests prove that a feature row stamped D depends only on raw events before D:
we corrupt every raw event at or after a cutoff and check that no feature row
stamped at or before the cutoff changes.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pandas.testing as pdt

from pharma_fs.features import build_feature_tables


def _corrupt_future(raw, cutoff: pd.Timestamp):
    """Return a copy of ``raw`` where everything at/after ``cutoff`` is altered."""
    cons = raw.consumption_events.copy()
    cons.loc[cons["event_ts"] >= cutoff, "quantity_g"] *= 1000

    snaps = raw.stock_snapshots.copy()
    snaps.loc[snaps["snapshot_ts"] >= cutoff, "stock_g"] = 123456.0

    po = raw.purchase_orders.copy()
    late = po["received_ts"] >= cutoff
    po.loc[late, "received_ts"] = po.loc[late, "received_ts"] + pd.Timedelta(days=30)

    lots = raw.lots.copy()
    lots.loc[lots["received_ts"] >= cutoff, "expiration_ts"] = cutoff + pd.Timedelta(days=1)

    quotes = raw.quotes.copy()
    # Future conversions disappear; future-created quotes all convert instantly.
    quotes.loc[quotes["converted_ts"] >= cutoff, "converted_ts"] = pd.NaT
    fut = quotes["created_ts"] >= cutoff
    quotes.loc[fut, "converted_ts"] = quotes.loc[fut, "created_ts"]

    prod = raw.production_orders.copy()
    prod.loc[prod["started_ts"] >= cutoff, "started_ts"] = pd.NaT
    prod.loc[prod["finished_ts"] >= cutoff, "finished_ts"] = pd.NaT

    return replace(
        raw,
        consumption_events=cons,
        stock_snapshots=snaps,
        purchase_orders=po,
        lots=lots,
        quotes=quotes,
        production_orders=prod,
    )


def test_future_events_do_not_change_past_features(small_world):
    """Corrupting events at/after a cutoff leaves earlier feature rows unchanged."""
    # Day 45 of the 60-day small world: late enough that every table has rows.
    cutoff = pd.Timestamp("2026-02-15", tz="UTC")
    original = small_world.tables
    corrupted = build_feature_tables(_corrupt_future(small_world.raw, cutoff))
    for name, df in original.items():
        keys = [c for c in ("event_timestamp", "pharmacy_id", "ingredient_id") if c in df]
        before = df[df["event_timestamp"] <= cutoff].sort_values(keys).reset_index(drop=True)
        after = corrupted[name]
        after = after[after["event_timestamp"] <= cutoff].sort_values(keys).reset_index(drop=True)
        assert len(before) > 0, name
        pdt.assert_frame_equal(before, after, check_dtype=False, obj=name)
        # Sanity: the corruption really changed later rows (the test has teeth).
        later_orig = df[df["event_timestamp"] > cutoff].sort_values(keys).reset_index(drop=True)
        later_corr = corrupted[name][corrupted[name]["event_timestamp"] > cutoff]
        later_corr = later_corr.sort_values(keys).reset_index(drop=True)
        assert not later_orig.equals(later_corr), name


def test_consumption_window_matches_raw_events(small_world):
    """consumption_7d_g at D == sum of raw events in [D-7d, D)."""
    cons = small_world.raw.consumption_events
    feats = small_world.tables["ingredient_consumption"]
    sample = feats.sample(40, random_state=0)
    for row in sample.itertuples():
        d = row.event_timestamp
        mask = (
            (cons["pharmacy_id"] == row.pharmacy_id)
            & (cons["ingredient_id"] == row.ingredient_id)
            & (cons["event_ts"] >= d - pd.Timedelta(days=7))
            & (cons["event_ts"] < d)
        )
        assert np.isclose(row.consumption_7d_g, cons.loc[mask, "quantity_g"].sum(), atol=1e-2)
