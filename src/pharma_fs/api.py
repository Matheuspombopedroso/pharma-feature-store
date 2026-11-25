"""FastAPI service for online feature retrieval.

This is step 5 of the pipeline: the read path a model server would call at
inference time. It wraps ``FeatureStore.get_online_features``, which reads the
latest materialized values from Redis.

Endpoints
---------
* ``GET  /health``                                         liveness + store type
* ``GET  /features``                                       list of served features
* ``GET  /features/{pharmacy_id}/{ingredient_id}``         one entity
* ``POST /features/batch``                                 many entities, one call

Design notes
------------
* The store is created once at startup (``lifespan``) and injected through
  ``app.state``; :func:`create_app` accepts a ready-made store so tests can pass
  a SQLite-backed one and never touch Redis.
* Handlers are plain ``def`` (not ``async def``): the Feast SDK call is
  blocking I/O, so FastAPI runs it in its worker thread pool instead of
  blocking the event loop.
* Every request asks for all served features. One Feast call covers both
  pharmacy-level and pharmacy+ingredient views: Feast picks the join keys each
  view needs from the entity row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from feast import FeatureStore
from pydantic import BaseModel, Field

from pharma_fs.definitions import ALL_FEATURE_VIEWS

#: "view:feature" references for every feature, e.g. "stock_coverage_days:stock_g".
FEATURE_REFS: list[str] = [
    f"{fv.name}:{field.name}" for fv in ALL_FEATURE_VIEWS for field in fv.features
]
#: Hard cap on batch size: keeps one request from monopolising a worker.
MAX_BATCH = 1000


class EntityKey(BaseModel):
    """One (pharmacy, ingredient) pair to look up."""

    pharmacy_id: str = Field(examples=["ph_001"])
    ingredient_id: str = Field(examples=["ing_0001"])


class BatchRequest(BaseModel):
    """Body of ``POST /features/batch``."""

    entities: list[EntityKey] = Field(min_length=1, max_length=MAX_BATCH)


class EntityFeatures(BaseModel):
    """Feature values for one entity. Missing values are ``null``."""

    pharmacy_id: str
    ingredient_id: str
    features: dict[str, Any]


class BatchResponse(BaseModel):
    """Body returned by ``POST /features/batch`` (same order as the request)."""

    results: list[EntityFeatures]


def _lookup(store: FeatureStore, keys: list[EntityKey]) -> list[EntityFeatures]:
    """Fetch every feature for ``keys`` in a single online-store call."""
    rows = [k.model_dump() for k in keys]
    data = store.get_online_features(features=FEATURE_REFS, entity_rows=rows).to_dict()
    names = [ref.split(":", 1)[1] for ref in FEATURE_REFS]
    return [
        EntityFeatures(
            pharmacy_id=k.pharmacy_id,
            ingredient_id=k.ingredient_id,
            features={n: data[n][i] for n in names},
        )
        for i, k in enumerate(keys)
    ]


def create_app(store_factory: Callable[[], FeatureStore] | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        store_factory: returns the ``FeatureStore`` to serve from. Defaults to
            :func:`pharma_fs.store.default_store` (Redis, from YAML).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create the store once at startup and warm it up before serving."""
        if store_factory is None:
            from pharma_fs.store import default_store  # deferred: reads env at startup

            app.state.store = default_store()
        else:
            app.state.store = store_factory()
        # Warm-up call: the first lookup loads the registry and opens the Redis
        # connection; doing it here keeps that cost out of the first request.
        _lookup(app.state.store, [EntityKey(pharmacy_id="warmup", ingredient_id="warmup")])
        yield

    app = FastAPI(title="pharma-feature-store", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health(request: Request) -> dict[str, str]:
        """Liveness probe; also reports which online store is configured."""
        store: FeatureStore = request.app.state.store
        return {"status": "ok", "online_store": store.config.online_store.type}

    @app.get("/features")
    def list_features() -> dict[str, list[str]]:
        """List the feature references the service returns."""
        return {"features": FEATURE_REFS}

    @app.get("/features/{pharmacy_id}/{ingredient_id}", response_model=EntityFeatures)
    def get_one(pharmacy_id: str, ingredient_id: str, request: Request) -> EntityFeatures:
        """Online features for one (pharmacy, ingredient). 404 if nothing is known."""
        key = EntityKey(pharmacy_id=pharmacy_id, ingredient_id=ingredient_id)
        result = _lookup(request.app.state.store, [key])[0]
        if all(v is None for v in result.features.values()):
            raise HTTPException(status_code=404, detail="no features for this entity")
        return result

    @app.post("/features/batch", response_model=BatchResponse)
    def get_batch(body: BatchRequest, request: Request) -> BatchResponse:
        """Online features for up to ``MAX_BATCH`` entities in one store call."""
        return BatchResponse(results=_lookup(request.app.state.store, body.entities))

    return app


#: Module-level app for ``uvicorn pharma_fs.api:app``.
app = create_app()
