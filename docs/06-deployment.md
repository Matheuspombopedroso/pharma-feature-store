# 6. Docker, compose and CI

**Files:** [`Dockerfile`](../Dockerfile), [`docker-compose.yml`](../docker-compose.yml), [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)

## docker compose

```bash
docker compose up --build
```

| service | role |
|---|---|
| `redis` | Feast online store. Persistence is off (`--save "" --appendonly no`) because the online store is a cache that materialization can always rebuild. |
| `materialize` | One-shot job running `pharma-fs setup`: generate data → apply definitions → incremental materialization. Exits when done. |
| `api` | `uvicorn pharma_fs.api:app` on port 8000. Starts only after `materialize` has completed successfully (`service_completed_successfully`). |

`materialize` and `api` share the named volume `fsdata`, mounted at `/app/data`. It holds
the Parquet tables and the Feast registry. Both containers use the same `/app` layout,
so the registry's relative paths resolve identically.

Inside the compose network, Redis is reached as `redis:6379` through
`REDIS_CONNECTION_STRING`. On the host it is `localhost:6379`.

> **Verification status:** this compose stack was not run end to end on the
> development machine. The Docker Desktop VM (1 GB RAM, shared with other projects)
> could not build the image: BuildKit returned `DeadlineExceeded`, and the legacy
> builder could not resolve Docker Hub. The `redis` service from this file was used for
> all local development and benchmarks.

## Dockerfile choices

* `python:3.11-slim` with `PIP_NO_CACHE_DIR=1` keeps the image small. Feast pulls in
  pyarrow, dask and protobuf.
* `pyproject.toml` and `src/` are copied and installed before `feature_repo/`, so
  editing the Feast config does not re-run the slow dependency layer.
* One uvicorn process by default. The benchmark ([part 5](05-benchmarks.md)) shows a
  single process saturating at roughly 230–285 single-entity req/s, with most of the
  time spent in Python. More processes are the usual way to scale this. That was not
  measured here, because uvicorn's multi-worker mode failed on the Windows dev machine.

## CI

On every push and pull request, GitHub Actions:

1. starts a `redis:7.4-alpine` service container, so the Redis integration test runs
   instead of being skipped;
2. installs the package with `.[dev]` on Python 3.11;
3. runs `ruff check` and `ruff format --check`;
4. runs `pytest -q`. The tests generate their own small dataset (3 pharmacies ×
   6 ingredients × 60 days) in a temp directory and use a SQLite online store, except
   for the Redis test, which uses logical DB 15.
