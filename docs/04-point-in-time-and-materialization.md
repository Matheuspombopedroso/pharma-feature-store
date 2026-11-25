# 4. Point-in-time retrieval and incremental materialization

**Code:** [`src/pharma_fs/materialize.py`](../src/pharma_fs/materialize.py) · **Tests:** [`tests/test_point_in_time.py`](../tests/test_point_in_time.py), [`tests/test_materialize.py`](../tests/test_materialize.py)

## Part A: historical retrieval without leakage

### Problem

A training set is a list of `(entity, timestamp)` rows, for example "pharmacy ph_001,
ingredient ing_0003, at 2026-03-14 10:22". Each row must get the feature values **as
they were at that moment**. A plain join on the latest value would leak future
information into training, and the model would look better offline than it is in
production.

### How Feast does it

`store.get_historical_features(entity_df, features)` does an as-of join for each view.
It takes the latest feature row with `feature_ts <= entity_ts` and
`feature_ts >= entity_ts - ttl`.

### How we prove it

**Sentinel test** (`test_sentinel_from_the_future_never_leaks`). A tiny hand-made table
holds a value of `999999` stamped **one second after** a training row. The test asserts:

| training row time | expected | why |
|---|---|---|
| before any data | null | nothing known yet |
| exactly on a feature row | that row | the join is inclusive (`<=`) |
| 1 s before the sentinel | the previous value, **not** 999999 | no look-ahead |
| 3 days after the last row | null | outside the 2-day ttl |
| exactly at the sentinel | 999999 | at that moment it is the past |

**Oracle test** (`test_historical_features_match_backward_asof_oracle`). This test takes
about 300 random training rows over the generated data. They include exact-midnight
boundaries and times before and after the data range. It retrieves all 8 views and
compares every value with an independent `pandas.merge_asof(direction="backward",
tolerance=ttl)`. More than 1000 non-null values must match exactly. The test also asserts
that the oracle never used a feature row stamped after its training row.

Combined with the upstream test in [part 2](02-feature-engineering.md), this covers both
places leakage can come from. The features themselves are built only from the past, and
the join only looks backward.

### Gotcha

The file offline store **de-duplicates** entity rows that share the same join keys and
timestamp (observed with feast 0.68: 300 rows in, 299 out). If you build a training set
with duplicates on purpose, add a distinguishing timestamp or de-duplicate first.

## Part B: incremental materialization to Redis

### Problem

The online store must hold the latest value per entity. Rescanning the whole history on
every run wastes work.

### How it works

`store.materialize_incremental(end)` reads, for each view, only rows in
`(previous end, end]`. It writes the latest row per entity to Redis and records the new
interval in the registry.

```python
fresh = [n for n, (_, stop) in materialized_intervals(store).items() if stop is None]
if fresh:
    store.materialize(start_date=first_snapshot - 1s, end_date=end, feature_views=fresh)
already = [...]
if already:
    store.materialize_incremental(end_date=end, feature_views=already)
```

**Why the bootstrap.** On a view's first run, Feast starts at *wall-clock* `now - ttl`.
The synthetic data sits in a fixed 2026 period, so that window would be empty: nothing
was written the first time this was tried. The first run therefore uses an explicit
start. Every later run is a real incremental run. With live data this workaround is not
needed.

**Simulated clock.** "Now" defaults to the last snapshot (2026-06-30). `--end` lets you
replay a daily job:

```bash
pharma-fs materialize --end 2026-04-01   # bootstrap: 2026-01-01 23:59:59 -> 2026-04-01
pharma-fs materialize                    # incremental: 2026-04-01 -> 2026-06-30
```

Captured output from this repo, with the second run starting where the first stopped:

```
materialize: up to 2026-04-01T00:00:00+00:00 in 10.51s
  ingredient_consumption_7d    2026-01-01 23:59:59+00:00 -> 2026-04-01 00:00:00+00:00
materialize: up to 2026-06-30T00:00:00+00:00 in 1.65s
  ingredient_consumption_7d    2026-04-01 00:00:00+00:00 -> 2026-06-30 00:00:00+00:00
```

After this, Redis holds 205 keys: 200 pharmacy×ingredient pairs and 5 pharmacy-only keys.
Feast stores one Redis hash per entity key, and that hash holds the fields of every view
keyed by that entity.

`test_incremental_runs_cover_consecutive_intervals` asserts that the second interval
starts exactly at the first one's end, and that the online value equals the latest
offline row after each run.

## Run just this part

```bash
docker compose up -d redis
pharma-fs generate && pharma-fs apply
pharma-fs materialize --end 2026-04-01 && pharma-fs materialize
pytest -q tests/test_point_in_time.py tests/test_materialize.py
```
