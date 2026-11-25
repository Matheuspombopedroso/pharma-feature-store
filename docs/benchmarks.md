# Benchmarks: raw results

Every number in the README comes from this file. The raw outputs are in
[`docs/raw/`](raw/) (`bench_w1.txt` is the console output and `bench_w1.json` the
machine-readable results).

## Hardware and software

| | |
|---|---|
| CPU | 11th Gen Intel Core i7-1185G7 @ 3.00GHz, 4 cores / 8 threads |
| RAM | 39.7 GB |
| OS | Windows 11 Pro 10.0.26200 |
| Redis | 7.4.12 (`redis:7.4-alpine`) in Docker Desktop 28.1.1. The Docker Linux VM has **2 vCPUs and ~1 GB RAM**, shared with about 20 unrelated containers that were running at the same time |
| API | `uvicorn pharma_fs.api:app --workers 1` running **on the host** (not in Docker) |
| Client | `scripts/benchmark.py` on the same host (httpx async) |
| Python / libs | Python 3.13.1, feast 0.68.0, redis-py 7.4.1, fastapi 0.165.0, uvicorn 0.54.0 |
| Data | Default dataset: 5 pharmacies × 40 ingredients. Redis holds 205 entity keys. Every request asks for all 17 features from the 8 feature views |

## Method

* `sdk` rows: sequential in-process `FeatureStore.get_online_features` calls against
  Redis, with no HTTP. This is the floor for a Python client.
* `http` rows: N concurrent async clients in a **closed loop**: each client sends its
  next request as soon as the previous response arrives. `single` = `GET
  /features/{ph}/{ing}`. `batch` = `POST /features/batch` with 50 random entities.
* For each scenario, 50 warm-up requests are excluded, then 1000 requests are measured.
  Entities are random (seed 0).
* Latencies are client-side wall time per request, in milliseconds. `rps` = measured
  requests / scenario wall time.

Command (same for both runs):

```bash
docker compose up -d redis
pharma-fs generate && pharma-fs apply && pharma-fs materialize
uvicorn pharma_fs.api:app --port 8077 --workers 1
python scripts/benchmark.py --url http://localhost:8077 --requests 1000 --warmup 50 \
    --concurrency 1 10 50 --batch-size 50 --out docs/raw/repN/bench_w1.json
```

## Run 2 (2026-11-25T22:03:46Z), the cleanest run, used in the README

```
target mode      bs conc      n      p50      p95      p99      rps
sdk    single     1    1   1000      1.9     3.59     7.27    455.9
sdk    batch     50    1   1000    16.39    34.58    59.54     48.8
http   single     1    1   1000     4.09     5.49      7.9    225.8
http   single     1   10   1000    20.03   118.59   261.59    284.7
http   single     1   50   1000   234.18   903.48  1237.78    156.9
http   batch     50    1   1000    14.22    16.83    20.07     65.9
http   batch     50   10   1000   125.83   179.99   212.78     78.5
http   batch     50   50   1000   698.76   920.24  1684.03     68.9
```

## Run 1 (2026-11-25T21:52:39Z), same setup, a few minutes earlier

```
target mode      bs conc      n      p50      p95      p99      rps
sdk    single     1    1   1000     1.84    15.65   1655.1     11.1
sdk    batch     50    1   1000    16.08   139.17  3125.46      7.5
http   single     1    1   1000     4.17    13.63    19.05    180.2
http   single     1   10   1000    48.93   522.39  1003.11     81.1
http   single     1   50   1000   320.54  1243.64  1854.81    113.2
http   batch     50    1   1000     15.2     22.1    26.74     59.1
http   batch     50   10   1000   134.85   193.57   221.91     72.3
http   batch     50   50   1000   663.68    770.4  1902.37     72.8
```

The medians agree between the runs, but the tails do not. In run 1 the SDK p99 is
1.6 s (single) and 3.1 s (batch), against 7 ms and 60 ms in run 2. Rare multi-second
stalls like these suggest the Redis side, the small shared Docker VM, rather than the
Python client. That was not proven. **Tail latencies on this setup are not stable**, and
the README quotes run 2 only with this caveat.

## What was attempted and discarded

* **4 uvicorn workers.** On Windows, uvicorn's multi-process mode failed: worker
  processes logged `OSError: [WinError 10022]` and requests hung until the client's 120 s
  timeout. No multi-worker numbers are reported. Earlier attempts that seemed to produce
  4-worker numbers were invalid, because the new server had failed to bind the port and
  the client was talking to a leftover single-worker process. Those outputs were deleted.
* **Async Feast reads** (`get_online_features_async`). Against Redis in Docker Desktop
  this either failed to connect (`localhost`) or hung (`127.0.0.1`), so it was not
  benchmarked.
* **API inside Docker.** The image build could not finish on this machine. BuildKit
  returned `DeadlineExceeded`, and the legacy builder could not resolve
  `registry-1.docker.io` from inside the Docker VM. So the full `docker compose up`
  (materialize job + containerised API) was **not run** here. Only the `redis` service
  from the compose file ran.
