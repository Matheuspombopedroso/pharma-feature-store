# Image used by both the one-shot "materialize" job and the "api" service.
# Slim base + --no-cache-dir keeps the image small; Feast pulls in a fair
# number of dependencies (pyarrow, dask, protobuf...).
FROM python:3.11-slim

# No .pyc files in the image, unbuffered logs (visible immediately in compose).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy only what pip needs first, so code edits do not invalidate the
# (slow) dependency layer.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# Feast repo config. Same layout as the git checkout (/app/feature_repo and
# /app/data), so the relative paths in feature_store.yaml resolve identically.
COPY feature_repo ./feature_repo
ENV PHARMA_FS_DATA_DIR=/app/data \
    PHARMA_FS_REPO_DIR=/app/feature_repo

EXPOSE 8000

# One uvicorn process by default. The Feast online read is synchronous and
# spends most of its time in Python, so one process saturates early (see
# docs/05-benchmarks.md). Adding processes (--workers N, or more replicas) is the
# usual way to scale; that was not benchmarked in this repo.
CMD ["uvicorn", "pharma_fs.api:app", "--host", "0.0.0.0", "--port", "8000"]
