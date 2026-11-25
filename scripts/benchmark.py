"""Online read latency benchmark (step 6 of the pipeline).

Two measurements, both against the Redis online store:

* ``sdk``  - in-process ``FeatureStore.get_online_features`` calls, one at a
  time. This is the floor: Feast SDK + Redis round trip, no HTTP.
* ``http`` - the FastAPI service, hit by N concurrent async clients (closed
  loop: each client sends its next request as soon as the previous answer
  arrives). Measures what a model server calling the service would see.

For each scenario we report p50/p95/p99 latency (ms) and throughput. Warm-up
requests are sent first and excluded from the statistics.

Usage::

    python scripts/benchmark.py --url http://localhost:8000 \
        --requests 2000 --concurrency 1 10 50 --batch-size 50 --out docs/bench_raw.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from typing import Any

import httpx

from pharma_fs import config


def _pct(values: list[float], q: float) -> float:
    """Percentile with linear interpolation (q in [0, 100])."""
    s = sorted(values)
    k = (len(s) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _summary(lat_s: list[float], wall_s: float, label: dict[str, Any]) -> dict[str, Any]:
    ms = [x * 1000 for x in lat_s]
    return {
        **label,
        "n": len(ms),
        "p50_ms": round(_pct(ms, 50), 2),
        "p95_ms": round(_pct(ms, 95), 2),
        "p99_ms": round(_pct(ms, 99), 2),
        "mean_ms": round(statistics.fmean(ms), 2),
        "throughput_rps": round(len(ms) / wall_s, 1),
    }


def _random_key(rng: random.Random) -> dict[str, str]:
    return {
        "pharmacy_id": rng.choice(config.pharmacy_ids()),
        "ingredient_id": rng.choice(config.ingredient_ids()),
    }


async def _http_scenario(
    url: str, mode: str, concurrency: int, n_requests: int, batch_size: int, warmup: int
) -> dict[str, Any]:
    """Run one (mode, concurrency) HTTP scenario and summarise latencies."""
    rng = random.Random(0)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, limits=limits, timeout=30) as client:

        async def one() -> float:
            if mode == "single":
                k = _random_key(rng)
                t0 = time.perf_counter()
                r = await client.get(f"/features/{k['pharmacy_id']}/{k['ingredient_id']}")
            else:
                body = {"entities": [_random_key(rng) for _ in range(batch_size)]}
                t0 = time.perf_counter()
                r = await client.post("/features/batch", json=body)
            dt = time.perf_counter() - t0
            r.raise_for_status()
            return dt

        for _ in range(warmup):  # excluded from stats
            await one()

        latencies: list[float] = []
        remaining = n_requests

        async def worker() -> None:
            nonlocal remaining
            while remaining > 0:
                remaining -= 1
                latencies.append(await one())

        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        wall = time.perf_counter() - t0

    label = {"target": "http", "mode": mode, "concurrency": concurrency}
    if mode == "batch":
        label["batch_size"] = batch_size
    return _summary(latencies, wall, label)


def _sdk_scenario(mode: str, n_requests: int, batch_size: int, warmup: int) -> dict[str, Any]:
    """Sequential in-process Feast SDK reads (no HTTP) against Redis."""
    from pharma_fs.api import FEATURE_REFS
    from pharma_fs.store import default_store

    store = default_store()
    rng = random.Random(0)
    size = 1 if mode == "single" else batch_size

    def one() -> float:
        rows = [_random_key(rng) for _ in range(size)]
        t0 = time.perf_counter()
        store.get_online_features(features=FEATURE_REFS, entity_rows=rows).to_dict()
        return time.perf_counter() - t0

    for _ in range(warmup):
        one()
    t0 = time.perf_counter()
    lat = [one() for _ in range(n_requests)]
    wall = time.perf_counter() - t0
    label = {"target": "sdk", "mode": mode, "concurrency": 1}
    if mode == "batch":
        label["batch_size"] = batch_size
    return _summary(lat, wall, label)


def _print_table(results: list[dict[str, Any]]) -> None:
    hdr = f"{'target':6} {'mode':7} {'bs':>4} {'conc':>4} {'n':>6} {'p50':>8} {'p95':>8} "
    hdr += f"{'p99':>8} {'rps':>8}"
    print(hdr)
    for r in results:
        print(
            f"{r['target']:6} {r['mode']:7} {r.get('batch_size', 1):>4} {r['concurrency']:>4} "
            f"{r['n']:>6} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['p99_ms']:>8} "
            f"{r['throughput_rps']:>8}"
        )


def main() -> None:
    """Parse args, run SDK + HTTP scenarios, print a table and optionally save JSON."""
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--requests", type=int, default=2000, help="measured requests per scenario")
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--concurrency", type=int, nargs="+", default=[1, 10, 50])
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--skip-sdk", action="store_true")
    p.add_argument("--out", help="write raw results as JSON")
    args = p.parse_args()

    results: list[dict[str, Any]] = []
    if not args.skip_sdk:
        for mode in ("single", "batch"):
            results.append(_sdk_scenario(mode, args.requests, args.batch_size, args.warmup))
    for mode in ("single", "batch"):
        for c in args.concurrency:
            results.append(
                asyncio.run(
                    _http_scenario(args.url, mode, c, args.requests, args.batch_size, args.warmup)
                )
            )
    _print_table(results)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
