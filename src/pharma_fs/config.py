"""Shared configuration: paths and simulation constants.

Every other module imports paths from here instead of hard-coding them, so the
same code works on a laptop (repo checkout) and inside the Docker image
(``/app``). Values that differ per environment (data directory, Redis address)
come from environment variables with sensible local defaults.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

#: Repository root (two levels above ``src/pharma_fs``).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Where generated Parquet files live. Overridable so Docker can mount a volume.
DATA_DIR = Path(os.environ.get("PHARMA_FS_DATA_DIR", PROJECT_ROOT / "data"))

#: Raw event logs (what an operational system would emit).
RAW_DIR = DATA_DIR / "raw"

#: Pre-aggregated feature tables that Feast reads as its offline store.
FEATURES_DIR = DATA_DIR / "features"

#: Feast repository (feature_store.yaml + definitions.py).
FEATURE_REPO_DIR = Path(os.environ.get("PHARMA_FS_REPO_DIR", PROJECT_ROOT / "feature_repo"))

#: Fixed seed: the whole dataset is reproducible byte-for-byte.
SEED = 42

#: Simulation window. Features are computed once per day at 00:00 UTC.
SIM_START = datetime(2026, 1, 1, tzinfo=UTC)
SIM_DAYS = 180

#: Tenants and catalogue size. Small on purpose: the goal is to exercise the
#: feature-store mechanics, not to stress the generator.
N_PHARMACIES = 5
N_INGREDIENTS = 40


def pharmacy_ids() -> list[str]:
    """Return the synthetic tenant ids, e.g. ``["ph_001", ...]``."""
    return [f"ph_{i:03d}" for i in range(1, N_PHARMACIES + 1)]


def ingredient_ids() -> list[str]:
    """Return the synthetic ingredient ids, e.g. ``["ing_0001", ...]``."""
    return [f"ing_{i:04d}" for i in range(1, N_INGREDIENTS + 1)]
