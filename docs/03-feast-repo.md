# 3. The Feast repository

**Code:** [`src/pharma_fs/definitions.py`](../src/pharma_fs/definitions.py), [`feature_repo/feature_store.yaml`](../feature_repo/feature_store.yaml), [`feature_repo/definitions.py`](../feature_repo/definitions.py), [`src/pharma_fs/store.py`](../src/pharma_fs/store.py)

## What problem it solves

Feast is the contract between training and serving. The same feature definitions are
used for:

* **offline** point-in-time retrieval, which builds training sets (`get_historical_features`), and
* **online** low-latency lookup at inference time (`get_online_features`, served from Redis).

## Objects

**Entities**: `pharmacy` (join key `pharmacy_id`) and `ingredient` (join key `ingredient_id`).

**Feature views** (8). Each reads one Parquet `FileSource`:

| feature view | entities | features |
|---|---|---|
| `ingredient_consumption_7d` | pharmacy, ingredient | consumption_7d_g |
| `ingredient_consumption_30d` | pharmacy, ingredient | consumption_30d_g |
| `stock_coverage_days` | pharmacy, ingredient | stock_g, avg_daily_consumption_30d_g, stock_coverage_days |
| `supplier_lead_time` | pharmacy, ingredient | lead_time_avg_last5_days, lead_time_last_days, received_orders_count |
| `expiration_days` | pharmacy, ingredient | days_to_nearest_expiration, lots_expiring_30d, active_lots |
| `quote_conversion_rate` | pharmacy | quotes_created_30d, quotes_converted_30d, quote_conversion_rate_30d |
| `production_queue_size` | pharmacy | production_queue_size |
| `avg_production_time` | pharmacy | avg_production_time_30d_min, finished_orders_30d |

Two views can share one source. The 7d and 30d consumption views both read
`ingredient_consumption.parquet` and expose different columns.

## Settings worth understanding

**`ttl = 2 days`.** In historical retrieval, Feast only joins a row that is at most `ttl`
older than the training row. The tables are daily, so "yesterday's snapshot, never
something weeks old" is the right contract. If the ttl is too short you silently get
nulls. If it is very long, pipeline gaps are hidden behind stale values.

**Paths relative to the repo.** Sources use `path="../data/features/<table>.parquet"`.
Feast resolves this against the repo path (`feature_repo/`), so the same definitions
work on the host and in the Docker image (`/app/feature_repo`, `/app/data`).

**`${REDIS_CONNECTION_STRING}`.** Feast runs `os.path.expandvars` on
`feature_store.yaml` (this was checked in the feast 0.68 source). An unset variable
would be passed through literally, so `store.default_store()` sets a `localhost:6379`
default.

**One source of truth for definitions.** The objects live in the installable package
(`pharma_fs.definitions`). The API, the materialization job and the tests import those
same objects. `feature_repo/definitions.py` only re-exports them for the `feast` CLI.

## Two ways to build a store

```python
default_store()  # feature_store.yaml -> Redis online store
build_store(repo, registry, online)  # config in code -> tests use SQLite, no Redis
```

`apply_definitions(store)` is the programmatic `feast apply`.

## Gotcha found while building this

After `store.apply(...)`, Feast adds the entity join-key columns to each view's
`schema`. Code that lists features must therefore use `fv.features`, not `fv.schema`.
Otherwise it requests `ingredient_consumption_7d:ingredient_id` and fails.

## Run just this part

```bash
pharma-fs apply          # or: cd feature_repo && feast apply
pytest -q tests/test_definitions.py
```
