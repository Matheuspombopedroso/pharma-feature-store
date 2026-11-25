"""Entry point for the ``feast`` CLI (``feast apply``, ``feast plan``...).

The ``feast`` CLI imports every ``.py`` file in this directory and registers the
Feast objects it finds at module level. The real definitions live in the
installable package (:mod:`pharma_fs.definitions`) so the API, the
materialization job and the tests import the *same* objects; this file just
re-exports them for the CLI.
"""

from pharma_fs.definitions import (  # noqa: F401  (re-exported for the feast CLI)
    avg_production_time,
    expiration_days,
    ingredient,
    ingredient_consumption_7d,
    ingredient_consumption_30d,
    pharmacy,
    production_queue_size,
    quote_conversion_rate,
    stock_coverage_days,
    supplier_lead_time,
)
