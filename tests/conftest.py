"""Shared pytest fixtures.

The tests never touch the developer's ``data/`` directory or Redis by default.
``small_world`` generates a reduced dataset (3 pharmacies x 6 ingredients x 60
days) into a temp directory laid out like the real repo::

    <tmp>/feature_repo/   (Feast repo path; sources resolve to ../data/...)
    <tmp>/data/features/  (Parquet feature tables)
    <tmp>/data/registry.db

and a Feast store over it with a SQLite online store.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest
from feast import FeatureStore

from pharma_fs import config
from pharma_fs.data_gen import RawData, generate_raw
from pharma_fs.features import build_feature_tables, write_feature_tables
from pharma_fs.store import apply_definitions, build_store


@dataclass
class World:
    """A generated dataset plus a Feast store over it."""

    root: Path
    raw: RawData
    tables: dict[str, pd.DataFrame]
    store: FeatureStore


@pytest.fixture(scope="session")
def small_world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    """Small dataset + applied Feast definitions (nothing materialized yet)."""
    with pytest.MonkeyPatch.context() as mp:
        # Shrink the simulation; the modules read these at call time.
        mp.setattr(config, "N_PHARMACIES", 3)
        mp.setattr(config, "N_INGREDIENTS", 6)
        mp.setattr(config, "SIM_DAYS", 60)
        root = tmp_path_factory.mktemp("world")
        (root / "feature_repo").mkdir()
        raw = generate_raw(seed=7)
        tables = build_feature_tables(raw)
        write_feature_tables(tables, root / "data" / "features")
        store = build_store(root / "feature_repo", root / "data" / "registry.db")
        apply_definitions(store)
        yield World(root, raw, tables, store)
