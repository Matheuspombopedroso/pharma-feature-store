"""Feature engineering: raw event logs -> daily feature tables for Feast.

This is step 2 of the pipeline. Feast does not compute rolling windows over raw
events for a file offline store, so we pre-aggregate here with pandas and hand
Feast one Parquet table per feature source. Each output row is a *snapshot* of
what was knowable at its ``event_timestamp``.

The single most important rule in this module
---------------------------------------------
A row stamped ``event_timestamp = D`` may only use raw events whose own
timestamp is **strictly before D**. Concretely, every row is stamped at the
moment its window *closes* (00:00 UTC of the next day), not when it opens.
If we stamped the 7-day consumption for days ``[D-7, D)`` at ``D-7`` instead,
Feast's point-in-time join would happily serve it to a training row at
``D-6``, and the model would see six days of future consumption. Feast cannot
detect that kind of leak: it trusts the timestamps it is given. The test
``tests/test_feature_engineering.py`` checks this rule by perturbing future
events and asserting past feature values do not change.

Output tables (all keyed by ``event_timestamp`` + entity ids):

* ``ingredient_consumption``  pharmacy_id, ingredient_id -> 7d / 30d grams
* ``stock_coverage``          pharmacy_id, ingredient_id -> stock, coverage days
* ``supplier_lead_time``      pharmacy_id, ingredient_id -> lead time stats
* ``expiration``              pharmacy_id, ingredient_id -> days to expiry
* ``quote_conversion``        pharmacy_id -> 30d quotes, conversions, rate
* ``production``              pharmacy_id -> queue size, avg production time
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from pharma_fs import config
from pharma_fs.data_gen import RawData

#: Coverage is capped so "no recent consumption" does not become infinity.
MAX_COVERAGE_DAYS = 365.0
#: Number of most recent received orders averaged for the lead-time feature.
LEAD_TIME_LAST_N = 5
#: Window for "expiring soon" lot counts.
EXPIRING_SOON_DAYS = 30


def _np_utc(values: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    """Return tz-naive ``datetime64[ns]`` (UTC wall clock) for fast numpy comparisons.

    ``Series.to_numpy()`` on a tz-aware column yields an object array of
    Timestamps, which is slow and does not compare with ``np.datetime64``.
    """
    if isinstance(values, pd.Series):
        values = pd.DatetimeIndex(values)
    return values.tz_convert(None).to_numpy().astype("datetime64[ns]")


def snapshot_times() -> pd.DatetimeIndex:
    """Return the daily snapshot instants D_1..D_N (00:00 UTC).

    D_k closes simulated day k-1, so D_1 = SIM_START + 1 day and the last one
    closes the final simulated day.
    """
    start = config.SIM_START + timedelta(days=1)
    return pd.date_range(start, periods=config.SIM_DAYS, freq="D", tz="UTC").astype(
        "datetime64[us, UTC]"
    )


def _pair_grid(raw: RawData) -> pd.DataFrame:
    """Cartesian product of (pharmacy, ingredient) pairs x snapshot times."""
    pairs = raw.stock_snapshots[["pharmacy_id", "ingredient_id"]].drop_duplicates()
    ts = pd.DataFrame({"event_timestamp": snapshot_times()})
    return pairs.merge(ts, how="cross")


def consumption_features(raw: RawData) -> pd.DataFrame:
    """7-day and 30-day ingredient consumption (grams) per pair.

    Daily totals are computed per calendar day d, a trailing rolling sum over
    days ``d-6..d`` is taken, and the result is stamped at ``d + 1 day``
    (window close), so it only contains events strictly before the stamp.
    """
    ev = raw.consumption_events
    daily = (
        ev.assign(day=ev["event_ts"].dt.floor("D"))
        .groupby(["pharmacy_id", "ingredient_id", "day"], as_index=False)["quantity_g"]
        .sum()
    )
    grid = _pair_grid(raw)
    # Build a dense daily grid so days without consumption count as 0, not "missing".
    grid["day"] = grid["event_timestamp"] - pd.Timedelta(days=1)
    dense = grid.merge(daily, on=["pharmacy_id", "ingredient_id", "day"], how="left")
    dense["quantity_g"] = dense["quantity_g"].fillna(0.0)
    dense = dense.sort_values(["pharmacy_id", "ingredient_id", "day"])
    g = dense.groupby(["pharmacy_id", "ingredient_id"])["quantity_g"]
    # min_periods=1: early days use a partial window instead of returning NaN.
    dense["consumption_7d_g"] = g.transform(lambda s: s.rolling(7, min_periods=1).sum())
    dense["consumption_30d_g"] = g.transform(lambda s: s.rolling(30, min_periods=1).sum())
    out = dense[
        ["event_timestamp", "pharmacy_id", "ingredient_id", "consumption_7d_g", "consumption_30d_g"]
    ]
    return out.round(3).reset_index(drop=True)


def stock_coverage_features(raw: RawData, consumption: pd.DataFrame) -> pd.DataFrame:
    """Latest stock level and days of coverage at the current 30d burn rate.

    ``stock_coverage_days = stock_g / (consumption_30d_g / 30)``, capped at
    :data:`MAX_COVERAGE_DAYS` when the burn rate is zero.
    """
    snaps = raw.stock_snapshots.sort_values("snapshot_ts")
    grid = consumption.sort_values("event_timestamp")
    # merge_asof(backward, allow_exact_matches=False) = "last snapshot strictly
    # before D". The 23:00 snapshot of day d is therefore visible at D = d+1.
    merged = pd.merge_asof(
        grid,
        snaps,
        left_on="event_timestamp",
        right_on="snapshot_ts",
        by=["pharmacy_id", "ingredient_id"],
        direction="backward",
        allow_exact_matches=False,
    )
    burn = merged["consumption_30d_g"] / 30.0
    coverage = np.where(burn > 0, merged["stock_g"] / burn.where(burn > 0, 1.0), MAX_COVERAGE_DAYS)
    merged["avg_daily_consumption_30d_g"] = burn
    merged["stock_coverage_days"] = np.minimum(coverage, MAX_COVERAGE_DAYS)
    cols = [
        "event_timestamp",
        "pharmacy_id",
        "ingredient_id",
        "stock_g",
        "avg_daily_consumption_30d_g",
        "stock_coverage_days",
    ]
    return merged[cols].round(3).sort_values(cols[:3]).reset_index(drop=True)


def supplier_lead_time_features(raw: RawData) -> pd.DataFrame:
    """Supplier lead-time statistics from *received* purchase orders.

    A lead time only becomes known when the goods arrive, so orders are keyed
    by ``received_ts`` (not ``order_ts``). Keying by order time would leak the
    future arrival date into the past.
    """
    po = raw.purchase_orders.dropna(subset=["received_ts"]).copy()
    po["lead_time_days"] = (po["received_ts"] - po["order_ts"]).dt.total_seconds() / 86400
    rows = []
    times = snapshot_times()
    times_np = _np_utc(times)
    for (ph, ing), g in po.groupby(["pharmacy_id", "ingredient_id"]):
        g = g.sort_values("received_ts")
        recv = _np_utc(g["received_ts"])
        lt = g["lead_time_days"].to_numpy()
        # Number of orders received strictly before each D.
        n_known = np.searchsorted(recv, times_np, side="left")
        for t, n in zip(times, n_known, strict=True):
            if n == 0:
                continue  # no history yet: leave the row out (Feast returns null)
            window = lt[max(0, n - LEAD_TIME_LAST_N) : n]
            rows.append(
                {
                    "event_timestamp": t,
                    "pharmacy_id": ph,
                    "ingredient_id": ing,
                    "lead_time_avg_last5_days": float(window.mean()),
                    "lead_time_last_days": float(lt[n - 1]),
                    "received_orders_count": int(n),
                }
            )
    cols = [
        "event_timestamp",
        "pharmacy_id",
        "ingredient_id",
        "lead_time_avg_last5_days",
        "lead_time_last_days",
        "received_orders_count",
    ]
    # Explicit columns so an empty result (no receipts yet) still has a schema.
    return pd.DataFrame(rows, columns=cols).round(3)


def expiration_features(raw: RawData) -> pd.DataFrame:
    """Days until the nearest expiration among lots still in stock at D.

    A lot is "in stock" at D if it was received before D and not yet depleted
    (or written off) before D.
    """
    lots = raw.lots
    rows = []
    for (ph, ing), g in lots.groupby(["pharmacy_id", "ingredient_id"]):
        recv = _np_utc(g["received_ts"])
        exp = _np_utc(g["expiration_ts"])
        dep = _np_utc(g["depleted_ts"])
        for t, t64 in zip(snapshot_times(), _np_utc(snapshot_times()), strict=True):
            active = (recv < t64) & (np.isnat(dep) | (dep >= t64)) & (exp > t64)
            if not active.any():
                days, soon = np.nan, 0
            else:
                left = (exp[active] - t64) / np.timedelta64(1, "D")
                days = float(left.min())
                soon = int((left <= EXPIRING_SOON_DAYS).sum())
            rows.append(
                {
                    "event_timestamp": t,
                    "pharmacy_id": ph,
                    "ingredient_id": ing,
                    "days_to_nearest_expiration": days,
                    "lots_expiring_30d": soon,
                    "active_lots": int(active.sum()),
                }
            )
    return pd.DataFrame(rows).round(3)


def quote_conversion_features(raw: RawData) -> pd.DataFrame:
    """30-day quote volume and conversion rate per pharmacy.

    Only conversions that happened *before* D are counted. Quotes created in
    the last day or two are therefore under-counted as converted (right
    censoring); that is the honest view at time D and what a live model sees.
    """
    q = raw.quotes
    rows = []
    for ph, g in q.groupby("pharmacy_id"):
        created = _np_utc(g["created_ts"])
        converted = _np_utc(g["converted_ts"])
        for t, t64 in zip(snapshot_times(), _np_utc(snapshot_times()), strict=True):
            in_win = (created >= t64 - np.timedelta64(30, "D")) & (created < t64)
            n_created = int(in_win.sum())
            n_conv = int((in_win & ~np.isnat(converted) & (converted < t64)).sum())
            rows.append(
                {
                    "event_timestamp": t,
                    "pharmacy_id": ph,
                    "quotes_created_30d": n_created,
                    "quotes_converted_30d": n_conv,
                    "quote_conversion_rate_30d": n_conv / n_created if n_created else 0.0,
                }
            )
    return pd.DataFrame(rows).round(4)


def production_features(raw: RawData) -> pd.DataFrame:
    """Production queue size at D and 30-day average production time.

    * queue size: orders queued before D that had not started by D.
    * avg production time: mean (finished - started) in minutes over orders
      that finished in ``[D-30d, D)``.
    """
    p = raw.production_orders
    rows = []
    for ph, g in p.groupby("pharmacy_id"):
        queued = _np_utc(g["queued_ts"])
        started = _np_utc(g["started_ts"])
        finished = _np_utc(g["finished_ts"])
        duration_min = (finished - started) / np.timedelta64(1, "m")
        for t, t64 in zip(snapshot_times(), _np_utc(snapshot_times()), strict=True):
            in_queue = (queued < t64) & (np.isnat(started) | (started >= t64))
            done = (
                ~np.isnat(finished) & (finished < t64) & (finished >= t64 - np.timedelta64(30, "D"))
            )
            rows.append(
                {
                    "event_timestamp": t,
                    "pharmacy_id": ph,
                    "production_queue_size": int(in_queue.sum()),
                    "avg_production_time_30d_min": (
                        float(duration_min[done].mean()) if done.any() else np.nan
                    ),
                    "finished_orders_30d": int(done.sum()),
                }
            )
    return pd.DataFrame(rows).round(3)


def build_feature_tables(raw: RawData) -> dict[str, pd.DataFrame]:
    """Compute every feature table from the raw logs.

    Returns:
        Mapping ``table name -> DataFrame``; names match the Parquet file stems
        referenced by ``feature_repo/definitions.py``.
    """
    consumption = consumption_features(raw)
    tables = {
        "ingredient_consumption": consumption,
        "stock_coverage": stock_coverage_features(raw, consumption),
        "supplier_lead_time": supplier_lead_time_features(raw),
        "expiration": expiration_features(raw),
        "quote_conversion": quote_conversion_features(raw),
        "production": production_features(raw),
    }
    for df in tables.values():
        # One consistent timestamp dtype for Feast's file offline store.
        df["event_timestamp"] = pd.to_datetime(df["event_timestamp"], utc=True).astype(
            "datetime64[us, UTC]"
        )
    return tables


def write_feature_tables(
    tables: dict[str, pd.DataFrame], out_dir: Path = config.FEATURES_DIR
) -> None:
    """Write each feature table to ``<out_dir>/<name>.parquet``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
