"""Incremental materialization: offline Parquet -> Redis online store.

This is step 4 of the pipeline. ``FeatureStore.materialize_incremental(end)``
loads, for every feature view, the *latest* row per entity whose timestamp
falls in ``(last materialized end, end]`` and writes it to the online store.
Feast stores the materialized interval in the registry, so the next run only
scans the new slice instead of the whole history.

First run: when a view has never been materialized, Feast's
``materialize_incremental`` starts at *wall-clock* ``now - ttl``. Our synthetic
data lives in a fixed simulated period, so that window would be empty. The
first run therefore bootstraps with an explicit ``materialize(start, end)``
from the first snapshot; every later run is a true incremental run that starts
at the previous run's end, recorded in the registry. With real, current data
this bootstrap is unnecessary.

Simulated clock: the synthetic data covers a fixed period in 2026, so "now" is
the end of the simulation (:func:`simulation_end`) unless ``--end`` is given.
Passing an earlier ``--end`` and then a later one replays how a daily cron job
would behave.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from feast import FeatureStore

from pharma_fs.features import snapshot_times


@dataclass
class MaterializationResult:
    """What a materialization run did, for logging and tests."""

    end_date: datetime
    seconds: float
    intervals: dict[str, tuple[datetime | None, datetime | None]]


def simulation_end() -> datetime:
    """The last daily snapshot instant: the simulated "now"."""
    return snapshot_times()[-1].to_pydatetime()


def materialized_intervals(
    store: FeatureStore,
) -> dict[str, tuple[datetime | None, datetime | None]]:
    """Return ``{view name: (start, end)}`` of the most recent materialized interval."""
    out: dict[str, tuple[datetime | None, datetime | None]] = {}
    for fv in store.list_feature_views():
        if fv.materialization_intervals:
            start, end = fv.materialization_intervals[-1]
            out[fv.name] = (start, end)
        else:
            out[fv.name] = (None, None)
    return out


def run_incremental(store: FeatureStore, end_date: datetime | None = None) -> MaterializationResult:
    """Materialize every feature view incrementally up to ``end_date``.

    Args:
        store: a store whose definitions were already applied.
        end_date: tz-aware upper bound; defaults to :func:`simulation_end`.
    """
    end = end_date or simulation_end()
    fresh = [n for n, (_, stop) in materialized_intervals(store).items() if stop is None]
    t0 = time.perf_counter()
    if fresh:
        # Bootstrap (see module docstring): explicit start at the first snapshot,
        # minus one second so the first row's timestamp is inside the window.
        start = snapshot_times()[0].to_pydatetime() - timedelta(seconds=1)
        store.materialize(start_date=start, end_date=end, feature_views=fresh)
    already = [fv.name for fv in store.list_feature_views() if fv.name not in fresh]
    if already:
        store.materialize_incremental(end_date=end, feature_views=already)
    elapsed = time.perf_counter() - t0
    return MaterializationResult(end, elapsed, materialized_intervals(store))
