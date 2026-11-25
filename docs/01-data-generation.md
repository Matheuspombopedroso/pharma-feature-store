# 1. Synthetic data generation

**Code:** [`src/pharma_fs/data_gen.py`](../src/pharma_fs/data_gen.py), [`src/pharma_fs/config.py`](../src/pharma_fs/config.py)

## What problem it solves

A feature store needs event data to aggregate. Real pharmacy data is private, so this
step stands in for the operational systems of a compounding pharmacy (ERP, stock
control, quoting, production). It writes **raw event logs**, not features. All data is
synthetic and generated with a fixed seed (`config.SEED = 42`).

## How it works

The simulation covers 180 days (2026-01-01 → 2026-06-30) for 5 pharmacies (tenants)
× 40 ingredients.

| raw table | one row per | key timestamps |
|---|---|---|
| `consumption_events` | ingredient withdrawal for an order (grams) | `event_ts` |
| `stock_snapshots` | end-of-day stock per pharmacy × ingredient | `snapshot_ts` (23:00 UTC) |
| `purchase_orders` | supplier order | `order_ts`, `received_ts` |
| `lots` | received lot | `received_ts`, `expiration_ts`, `depleted_ts` |
| `quotes` | customer quote | `created_ts`, `converted_ts` (null = not converted) |
| `production_orders` | compounding order from a converted quote | `queued_ts`, `started_ts`, `finished_ts` |

Each `(pharmacy, ingredient)` pair runs a small day-by-day inventory loop
(`_simulate_inventory`):

1. Deliveries due today arrive as new **lots**.
2. Expired lots are written off.
3. Consumption events (Poisson count, weekends at 40%) take stock **FIFO** from the oldest lot.
4. A stock snapshot is taken at 23:00.
5. If stock is below the reorder point and nothing is in transit, a **purchase order** is
   placed. Its lead time follows a gamma distribution around a pair-specific mean.

Quotes and production orders are simulated per pharmacy
(`_simulate_quotes_and_production`). Each pharmacy has its own conversion probability,
queue wait and production duration.

### Details that matter later

```python
# A receipt after the simulation end is unknown (NaT):
# we must not "know" the lead time of an open order.
"received_ts": received_ts if received_ts < end else pd.NaT,
```

Events that would happen after the simulated "now" are **censored**, not filled in.
Without this, the dataset itself would carry knowledge of the future.

```python
df[col] = pd.to_datetime(df[col], utc=True).astype("datetime64[us, UTC]")
```

Every timestamp is tz-aware UTC with microsecond precision. Mixing naive and aware
timestamps, or different precisions, is a common cause of broken point-in-time joins.

## Run just this part

```bash
pharma-fs generate     # writes data/raw/*.parquet and data/features/*.parquet
```

## What to look at

The command prints row counts. With the default seed: 101,136 consumption events,
36,000 stock snapshots (5 × 40 × 180), 867 purchase orders, 1,027 lots, 32,457 quotes
and 14,917 production orders.
You can inspect any table with pandas:

```python
import pandas as pd

pd.read_parquet("data/raw/purchase_orders.parquet").head()
```
