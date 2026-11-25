"""Construction of the Feast ``FeatureStore`` object.

Two ways to get a store:

* :func:`default_store` reads ``feature_repo/feature_store.yaml`` (Redis online
  store). Used by the materialization job, the API and the benchmark.
* :func:`build_store` builds the config in code. Tests use it with a SQLite
  online store in a temp directory, so they run without Redis.

:func:`apply_definitions` registers the objects from
:mod:`pharma_fs.definitions` - the programmatic equivalent of ``feast apply``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from feast import FeatureStore, RepoConfig

from pharma_fs import config
from pharma_fs.definitions import ALL_ENTITIES, ALL_FEATURE_VIEWS

DEFAULT_REDIS = "localhost:6379"


def default_store(repo_path: Path = config.FEATURE_REPO_DIR) -> FeatureStore:
    """Load the store described by ``feature_store.yaml`` (Redis online store).

    ``REDIS_CONNECTION_STRING`` defaults to ``localhost:6379``: Feast expands
    ``${VAR}`` in the YAML, and an unset variable would otherwise be passed
    through literally as the connection string.
    """
    os.environ.setdefault("REDIS_CONNECTION_STRING", DEFAULT_REDIS)
    return FeatureStore(repo_path=str(repo_path))


def build_store(
    repo_path: Path,
    registry_path: Path,
    online_store: dict[str, Any] | None = None,
) -> FeatureStore:
    """Build a store from code instead of YAML.

    Args:
        repo_path: directory that relative source paths resolve against.
        registry_path: where the registry protobuf file lives.
        online_store: Feast online store config dict; defaults to SQLite at
            ``<registry dir>/online.db``.
    """
    online = online_store or {"type": "sqlite", "path": str(registry_path.parent / "online.db")}
    repo_config = RepoConfig(
        project="pharma_fs",
        registry=str(registry_path),
        provider="local",
        offline_store={"type": "file"},
        online_store=online,
        entity_key_serialization_version=3,
        repo_path=repo_path,
    )
    return FeatureStore(config=repo_config)


def apply_definitions(store: FeatureStore) -> None:
    """Register all entities and feature views (idempotent, like ``feast apply``)."""
    store.apply([*ALL_ENTITIES, *ALL_FEATURE_VIEWS])
