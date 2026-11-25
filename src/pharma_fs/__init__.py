"""pharma_fs: a Feast + Redis feature store demo for compounding pharmacies.

Pipeline (one module per step):

1. :mod:`pharma_fs.data_gen`     synthetic raw event logs (Parquet)
2. :mod:`pharma_fs.features`     raw events -> point-in-time-safe feature tables
3. ``feature_repo/``             Feast entities, sources and feature views
4. :mod:`pharma_fs.materialize`  incremental offline -> Redis materialization
5. :mod:`pharma_fs.api`          FastAPI online feature retrieval
6. ``scripts/benchmark.py``      latency benchmark under concurrency

All data is synthetic. This is a demo, not a production medical system.
"""
