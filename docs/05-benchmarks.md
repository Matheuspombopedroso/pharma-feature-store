# 5. Online serving and benchmarks

**Code:** [`src/pharma_fs/api.py`](../src/pharma_fs/api.py), [`scripts/benchmark.py`](../scripts/benchmark.py) · **Tests:** [`tests/test_api.py`](../tests/test_api.py) · **Raw results:** [benchmarks.md](benchmarks.md)

## Part A: the API

### What problem it solves

A model server needs the current feature values for an entity at inference time. The
FastAPI service is that read path. It wraps `get_online_features`, which reads Redis.

| endpoint | purpose |
|---|---|
| `GET /features/{pharmacy_id}/{ingredient_id}` | all 17 features for one entity; 404 if nothing is known |
| `POST /features/batch` | up to 1000 entities in **one** Feast call; results come back in request order, with nulls for unknown entities |
| `GET /features` | the served feature references (`view:feature`) |
| `GET /health` | liveness and the online store type |

### Key design points

```python
FEATURE_REFS = [f"{fv.name}:{field.name}" for fv in ALL_FEATURE_VIEWS for field in fv.features]
```

The feature list is derived from the same definitions Feast registered, so the API
cannot drift from the feature repo.

```python
def create_app(store_factory: Callable[[], FeatureStore] | None = None) -> FastAPI:
```

The store is injected. Production code uses `default_store()` (Redis, from YAML).
Tests pass a SQLite-backed store, so they run in CI without Redis. At startup the
lifespan sends one warm-up lookup, which loads the registry and opens the Redis
connection before the first real request.

Handlers are plain `def`, because the Feast online read is blocking. FastAPI runs them
in its thread pool, so a request never blocks the event loop.

### Run just this part

```bash
uvicorn pharma_fs.api:app --port 8000
curl http://localhost:8000/features/ph_001/ing_0001
pytest -q tests/test_api.py
```

## Part B: measured latency

### Method

`scripts/benchmark.py` measures two things:

* **sdk**: sequential in-process `get_online_features` against Redis, with no HTTP. This is the floor.
* **http**: 1, 10 and 50 concurrent async clients in a closed loop against the API.
  Scenarios are `single` (one entity) and `batch` (50 entities per request).

It runs 50 warm-up requests, then 1000 measured requests per scenario, and reports
p50/p95/p99 in ms plus throughput. Setup: one uvicorn worker on the host, and Redis in
Docker Desktop (2-vCPU / 1 GB VM). Full hardware details are in
[benchmarks.md](benchmarks.md).

### Results (run 2, 1 uvicorn worker)

| scenario | concurrency | p50 ms | p95 ms | p99 ms | req/s |
|---|---:|---:|---:|---:|---:|
| SDK, 1 entity | 1 | 1.90 | 3.59 | 7.27 | 455.9 |
| SDK, 50 entities | 1 | 16.39 | 34.58 | 59.54 | 48.8 |
| HTTP single | 1 | 4.09 | 5.49 | 7.90 | 225.8 |
| HTTP single | 10 | 20.03 | 118.59 | 261.59 | 284.7 |
| HTTP single | 50 | 234.18 | 903.48 | 1237.78 | 156.9 |
| HTTP batch (50) | 1 | 14.22 | 16.83 | 20.07 | 65.9 |
| HTTP batch (50) | 10 | 125.83 | 179.99 | 212.78 | 78.5 |
| HTTP batch (50) | 50 | 698.76 | 920.24 | 1684.03 | 68.9 |

### How to read it

* **One entity, no contention: p50 1.90 ms in the SDK, 4.09 ms over HTTP.** The
  2.19 ms difference is the whole HTTP path: the httpx client, uvicorn, FastAPI and
  pydantic.
* **Batching amortises the cost.** 50 entities had a p50 of 14.22 ms over HTTP
  (≈0.28 ms per entity), against 4.09 ms for one. A model scoring 50 ingredients at once
  should call `/features/batch`.
* **One process saturates early.** Single-entity throughput was 225.8 req/s at
  concurrency 1 and 284.7 req/s at concurrency 10. Above that, extra clients only queue:
  at concurrency 50, p50 is 234.18 ms and throughput fell to 156.9 req/s. *Hypothesis
  (not profiled):* most of the per-request time is Python work in the Feast SDK and the
  API, which threads cannot run in parallel under the GIL, so more processes behind a
  load balancer would be the usual fix. That could **not** be measured here, because
  uvicorn's multi-worker mode failed on Windows (see [benchmarks.md](benchmarks.md)).
* **Tails are noisy on this machine.** An earlier run had the same medians but
  multi-second p99 outliers. Treat the p99 column as indicative only.

### Run just this part

```bash
docker compose up -d redis
pharma-fs setup                      # generate + apply + materialize
uvicorn pharma_fs.api:app --port 8000 &
python scripts/benchmark.py --url http://localhost:8000 --requests 1000 --concurrency 1 10 50
```
