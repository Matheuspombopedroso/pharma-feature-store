# 2. Feature engineering (point-in-time safe)

**Code:** [`src/pharma_fs/features.py`](../src/pharma_fs/features.py) · **Tests:** [`tests/test_feature_engineering.py`](../tests/test_feature_engineering.py)

## What problem it solves

With a file offline store, Feast does not compute rolling windows over raw events. It
joins precomputed rows by timestamp. This step turns raw logs into **daily feature
snapshots**: one row per entity per day, saying what was known at that instant.

## The one rule

> A row stamped `event_timestamp = D` may only use raw events with timestamp **< D**.

Each row is stamped at the moment its window **closes** (00:00 UTC of the following
day). Suppose the 7-day consumption for days `[D-7, D)` were stamped at `D-7` instead.
Feast's point-in-time join would then serve it to a training row at `D-6`, and the
model would see six days of future demand. **Feast cannot detect this.** It trusts the
timestamps you give it. Leakage is usually introduced here, upstream of the feature
store.

## Output tables

| table | entities | columns |
|---|---|---|
| `ingredient_consumption` | pharmacy, ingredient | `consumption_7d_g`, `consumption_30d_g` |
| `stock_coverage` | pharmacy, ingredient | `stock_g`, `avg_daily_consumption_30d_g`, `stock_coverage_days` |
| `supplier_lead_time` | pharmacy, ingredient | `lead_time_avg_last5_days`, `lead_time_last_days`, `received_orders_count` |
| `expiration` | pharmacy, ingredient | `days_to_nearest_expiration`, `lots_expiring_30d`, `active_lots` |
| `quote_conversion` | pharmacy | `quotes_created_30d`, `quotes_converted_30d`, `quote_conversion_rate_30d` |
| `production` | pharmacy | `production_queue_size`, `avg_production_time_30d_min`, `finished_orders_30d` |

## Key excerpts

Rolling consumption builds a **dense** daily grid first, so days without consumption
count as 0 instead of being missing. The window over days `d-6..d` is then stamped at
`d + 1 day`:

```python
grid["day"] = grid["event_timestamp"] - pd.Timedelta(days=1)
dense = grid.merge(daily, on=["pharmacy_id", "ingredient_id", "day"], how="left")
dense["consumption_7d_g"] = g.transform(lambda s: s.rolling(7, min_periods=1).sum())
```

The stock snapshot is matched with "last snapshot **strictly** before D":

```python
pd.merge_asof(..., direction="backward", allow_exact_matches=False)
```

Supplier lead time is keyed by **`received_ts`**, not `order_ts`. A lead time is only
known once the goods arrive. Keying it by order date would push the future arrival date
into the past.

Quote conversion counts only conversions that happened before D. Quotes created in the
last day or two therefore look under-converted (right censoring). That is the honest
view at time D, and it is what a live model would see.

## How it is tested

`test_future_events_do_not_change_past_features` corrupts every raw event at or after a
cutoff: it multiplies consumption by 1000, sets stock to 123456, delays receipts, flips
conversions and drops production starts. It then recomputes all tables and asserts that
**every row stamped ≤ cutoff is unchanged**. As a guard against a test that cannot fail,
it also asserts that rows after the cutoff *did* change.

`test_consumption_window_matches_raw_events` recomputes `consumption_7d_g` from raw
events with an independent filter, `[D-7d, D)`.

## Run just this part

```bash
pharma-fs generate                                  # raw + features
pytest -q tests/test_feature_engineering.py
```
