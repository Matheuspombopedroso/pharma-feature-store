"""Synthetic raw event generator for a network of compounding pharmacies.

This is step 1 of the pipeline. It plays the role of the operational systems
(ERP, stock control, quoting, production) and emits *raw event logs* as Parquet:

=========================  ===================================================
file                       one row per ...
=========================  ===================================================
consumption_events         ingredient withdrawal (grams) for a compounded order
stock_snapshots            end-of-day stock level per (pharmacy, ingredient)
purchase_orders            supplier order: placed -> received (lead time)
lots                       received lot with expiration date and depletion time
quotes                     customer quote: created -> converted (or never)
production_orders          compounding order: queued -> started -> finished
=========================  ===================================================

Everything is driven by one ``numpy`` generator seeded with ``config.SEED`` so
the dataset is reproducible. No real pharmacy, patient or supplier data is used.

The simulation is intentionally simple but *internally consistent*: stock goes
down with consumption, purchase orders are triggered by a reorder point, lots
arrive after a supplier-specific lead time and are consumed FIFO, and production
orders come from converted quotes. Consistency matters because the feature
engineering step derives features from several of these logs at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from pharma_fs import config


@dataclass
class RawData:
    """Container for all raw event tables produced by :func:`generate_raw`."""

    consumption_events: pd.DataFrame
    stock_snapshots: pd.DataFrame
    purchase_orders: pd.DataFrame
    lots: pd.DataFrame
    quotes: pd.DataFrame
    production_orders: pd.DataFrame

    def tables(self) -> dict[str, pd.DataFrame]:
        """Return the tables keyed by their Parquet file stem."""
        return {
            "consumption_events": self.consumption_events,
            "stock_snapshots": self.stock_snapshots,
            "purchase_orders": self.purchase_orders,
            "lots": self.lots,
            "quotes": self.quotes,
            "production_orders": self.production_orders,
        }


def _simulate_inventory(
    rng: np.random.Generator, pharmacy_id: str, ingredient_id: str
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Simulate one (pharmacy, ingredient) pair day by day.

    Returns four lists of row dicts: consumption events, stock snapshots,
    purchase orders and lots. Kept as a plain Python loop because the state
    (stock, open order, FIFO lots) is sequential and the volume is small
    (200 pairs x 180 days).
    """
    start = config.SIM_START
    end = start + timedelta(days=config.SIM_DAYS)

    # Each pair has its own demand level and supplier behaviour, so the
    # features differ meaningfully across entities.
    base_daily_g = float(rng.lognormal(mean=3.0, sigma=0.8))  # ~20 g/day median
    events_per_day = float(rng.uniform(1.0, 6.0))
    lead_time_mean_days = float(rng.uniform(2.0, 12.0))
    shelf_life_days = int(rng.integers(45, 240))
    reorder_point = base_daily_g * (lead_time_mean_days + 5)
    order_qty = base_daily_g * 30

    consumption: list[dict] = []
    snapshots: list[dict] = []
    orders: list[dict] = []
    lots: list[dict] = []

    # FIFO queue of open lots: [lot_index_in_lots_list, remaining_grams].
    fifo: list[list[float]] = []

    def receive_lot(ts: pd.Timestamp, qty: float) -> None:
        lot_idx = len(lots)
        lots.append(
            {
                "pharmacy_id": pharmacy_id,
                "ingredient_id": ingredient_id,
                "lot_id": f"{pharmacy_id}-{ingredient_id}-L{lot_idx:03d}",
                "received_ts": ts,
                "expiration_ts": ts + timedelta(days=shelf_life_days),
                "quantity_g": qty,
                "depleted_ts": pd.NaT,
            }
        )
        fifo.append([lot_idx, qty])

    # Opening stock arrives as one lot just before the simulation starts.
    receive_lot(pd.Timestamp(start - timedelta(days=1)), base_daily_g * 45)
    pending_arrival: tuple[pd.Timestamp, float] | None = None

    for day in range(config.SIM_DAYS):
        day_start = pd.Timestamp(start + timedelta(days=day))

        # 1) Deliveries scheduled for today arrive at their exact timestamp.
        if pending_arrival is not None and pending_arrival[0] < day_start + timedelta(days=1):
            receive_lot(*pending_arrival)
            pending_arrival = None

        # 2) Expired lots are written off at their expiration time.
        for lot in fifo:
            lot_row = lots[int(lot[0])]
            if lot_row["expiration_ts"] < day_start + timedelta(days=1) and lot[1] > 0:
                lot[1] = 0.0
                lot_row["depleted_ts"] = lot_row["expiration_ts"]
        fifo[:] = [lot for lot in fifo if lot[1] > 0]

        # 3) Consumption events: Poisson count, weekday seasonality (weekends ~40%).
        weekday_factor = 0.4 if day_start.weekday() >= 5 else 1.0
        n_events = rng.poisson(events_per_day * weekday_factor)
        if n_events:
            secs = np.sort(rng.uniform(8 * 3600, 20 * 3600, n_events))  # business hours
            qtys = rng.gamma(2.0, base_daily_g / events_per_day / 2.0, n_events)
            for sec, qty in zip(secs, qtys, strict=True):
                ts = day_start + timedelta(seconds=float(sec))
                remaining = float(qty)
                taken = 0.0
                # Consume FIFO; if stock runs out the unmet demand is simply lost.
                while remaining > 0 and fifo:
                    lot = fifo[0]
                    use = min(lot[1], remaining)
                    lot[1] -= use
                    remaining -= use
                    taken += use
                    if lot[1] <= 1e-9:
                        lots[int(lot[0])]["depleted_ts"] = ts
                        fifo.pop(0)
                if taken > 0:
                    consumption.append(
                        {
                            "event_ts": ts,
                            "pharmacy_id": pharmacy_id,
                            "ingredient_id": ingredient_id,
                            "quantity_g": round(taken, 3),
                        }
                    )

        # 4) End-of-day stock snapshot at 23:00 UTC.
        stock = float(sum(lot[1] for lot in fifo))
        snap_ts = day_start + timedelta(hours=23)
        snapshots.append(
            {
                "snapshot_ts": snap_ts,
                "pharmacy_id": pharmacy_id,
                "ingredient_id": ingredient_id,
                "stock_g": round(stock, 3),
            }
        )

        # 5) Reorder when below the reorder point and nothing is in transit.
        if stock < reorder_point and pending_arrival is None:
            lead_days = float(max(0.5, rng.gamma(4.0, lead_time_mean_days / 4.0)))
            received_ts = snap_ts + timedelta(days=lead_days)
            orders.append(
                {
                    "pharmacy_id": pharmacy_id,
                    "ingredient_id": ingredient_id,
                    "order_ts": snap_ts,
                    # A receipt after the simulation end is unknown (NaT):
                    # we must not "know" the lead time of an open order.
                    "received_ts": received_ts if received_ts < end else pd.NaT,
                    "quantity_g": round(order_qty, 3),
                }
            )
            pending_arrival = (received_ts, order_qty)

    return consumption, snapshots, orders, lots


def _simulate_quotes_and_production(
    rng: np.random.Generator, pharmacy_id: str
) -> tuple[list[dict], list[dict]]:
    """Simulate quotes and the production orders they generate for one tenant."""
    start = config.SIM_START
    end = start + timedelta(days=config.SIM_DAYS)
    quotes_per_day = float(rng.uniform(15, 60))
    conversion_p = float(rng.uniform(0.25, 0.65))
    mean_duration_min = float(rng.uniform(30, 90))
    # Lower capacity -> longer queues. Expressed as mean wait before start.
    mean_wait_hours = float(rng.uniform(2, 24))

    quotes: list[dict] = []
    production: list[dict] = []
    for day in range(config.SIM_DAYS):
        day_start = pd.Timestamp(start + timedelta(days=day))
        weekday_factor = 0.3 if day_start.weekday() >= 5 else 1.0
        n = rng.poisson(quotes_per_day * weekday_factor)
        secs = rng.uniform(7 * 3600, 21 * 3600, n)
        for sec in secs:
            created = day_start + timedelta(seconds=float(sec))
            converted = pd.NaT
            if rng.random() < conversion_p:
                c = created + timedelta(hours=float(rng.exponential(30)))
                converted = c if c < end else pd.NaT  # censored at sim end
            qid = f"{pharmacy_id}-Q{len(quotes):06d}"
            quotes.append(
                {
                    "quote_id": qid,
                    "pharmacy_id": pharmacy_id,
                    "created_ts": created,
                    "converted_ts": converted,
                }
            )
            if pd.notna(converted):
                started = converted + timedelta(hours=float(rng.exponential(mean_wait_hours)))
                finished = started + timedelta(
                    minutes=float(rng.lognormal(np.log(mean_duration_min), 0.35))
                )
                production.append(
                    {
                        "order_id": f"{qid}-P",
                        "pharmacy_id": pharmacy_id,
                        "queued_ts": converted,
                        "started_ts": started if started < end else pd.NaT,
                        "finished_ts": finished if finished < end else pd.NaT,
                    }
                )
    return quotes, production


def generate_raw(seed: int = config.SEED) -> RawData:
    """Generate every raw event table for all pharmacies and ingredients.

    Args:
        seed: RNG seed. The default makes the dataset reproducible.

    Returns:
        A :class:`RawData` with tz-aware UTC timestamps in every table.
    """
    rng = np.random.default_rng(seed)
    cons, snaps, orders, lots = [], [], [], []
    quotes, prod = [], []
    for ph in config.pharmacy_ids():
        for ing in config.ingredient_ids():
            c, s, o, lt = _simulate_inventory(rng, ph, ing)
            cons += c
            snaps += s
            orders += o
            lots += lt
        q, p = _simulate_quotes_and_production(rng, ph)
        quotes += q
        prod += p

    def frame(rows: list[dict], ts_cols: list[str]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        for col in ts_cols:
            # Normalise everything to tz-aware UTC microseconds; mixing naive
            # and aware timestamps is the classic way to break PIT joins.
            df[col] = pd.to_datetime(df[col], utc=True).astype("datetime64[us, UTC]")
        return df

    return RawData(
        consumption_events=frame(cons, ["event_ts"]),
        stock_snapshots=frame(snaps, ["snapshot_ts"]),
        purchase_orders=frame(orders, ["order_ts", "received_ts"]),
        lots=frame(lots, ["received_ts", "expiration_ts", "depleted_ts"]),
        quotes=frame(quotes, ["created_ts", "converted_ts"]),
        production_orders=frame(prod, ["queued_ts", "started_ts", "finished_ts"]),
    )


def write_raw(raw: RawData, out_dir: Path = config.RAW_DIR) -> None:
    """Write each raw table to ``<out_dir>/<name>.parquet``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in raw.tables().items():
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
