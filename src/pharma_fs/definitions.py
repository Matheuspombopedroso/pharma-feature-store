"""Feast object definitions: entities, data sources and feature views.

This is step 3 of the pipeline. It tells Feast *what* exists:

* two entities - ``pharmacy`` (the tenant) and ``ingredient``;
* one ``FileSource`` per Parquet feature table written by ``pharma_fs.features``;
* eight feature views, one per business indicator a model might consume.

Feature views keyed by both entities describe a pharmacy's stock of a given
ingredient (consumption, coverage, lead time, expiration). Feature views keyed
only by ``pharmacy`` describe the tenant's commercial / production state
(quote conversion, production queue, production time).

Why ``ttl``: during historical retrieval Feast only joins a feature row if it is
at most ``ttl`` older than the entity row's timestamp. Our tables are daily
snapshots, so a 2-day ttl means "use yesterday's snapshot, but never something
stale from weeks ago". A too-short ttl would silently produce nulls; a very
long one would hide gaps in the pipeline.

Source paths are relative to the Feast repo directory (``feature_repo/``);
Feast resolves them against the repo path at runtime, so the same objects work
on the host and in Docker as long as the ``feature_repo/`` + ``data/`` layout
is preserved. ``feature_repo/definitions.py`` re-exports these objects for the
``feast`` CLI.
"""

from datetime import timedelta

from feast import Entity, FeatureView, Field, FileSource
from feast.types import Float64, Int64
from feast.value_type import ValueType

# ---------------------------------------------------------------------------
# Entities: the join keys. Both are strings ("ph_001", "ing_0001").
# ---------------------------------------------------------------------------
pharmacy = Entity(
    name="pharmacy",
    join_keys=["pharmacy_id"],
    value_type=ValueType.STRING,
    description="A compounding pharmacy (tenant).",
)
ingredient = Entity(
    name="ingredient",
    join_keys=["ingredient_id"],
    value_type=ValueType.STRING,
    description="An active ingredient or excipient held in stock.",
)

#: Daily snapshot tables -> accept yesterday's row, nothing older.
DAILY_TTL = timedelta(days=2)


def _source(table: str) -> FileSource:
    """FileSource for ``data/features/<table>.parquet`` (path relative to repo)."""
    return FileSource(
        name=f"{table}_source",
        path=f"../data/features/{table}.parquet",
        # The column Feast uses for point-in-time joins. Each row's timestamp is
        # the instant its aggregation window closed (see pharma_fs.features).
        timestamp_field="event_timestamp",
    )


consumption_source = _source("ingredient_consumption")
stock_coverage_source = _source("stock_coverage")
supplier_lead_time_source = _source("supplier_lead_time")
expiration_source = _source("expiration")
quote_conversion_source = _source("quote_conversion")
production_source = _source("production")

# ---------------------------------------------------------------------------
# Pharmacy x ingredient feature views
# ---------------------------------------------------------------------------
ingredient_consumption_7d = FeatureView(
    name="ingredient_consumption_7d",
    entities=[pharmacy, ingredient],
    ttl=DAILY_TTL,
    schema=[Field(name="consumption_7d_g", dtype=Float64)],
    # Two views can read the same source and expose different columns.
    source=consumption_source,
    online=True,
    description="Grams consumed in the 7 days before the snapshot.",
)

ingredient_consumption_30d = FeatureView(
    name="ingredient_consumption_30d",
    entities=[pharmacy, ingredient],
    ttl=DAILY_TTL,
    schema=[Field(name="consumption_30d_g", dtype=Float64)],
    source=consumption_source,
    online=True,
    description="Grams consumed in the 30 days before the snapshot.",
)

stock_coverage_days = FeatureView(
    name="stock_coverage_days",
    entities=[pharmacy, ingredient],
    ttl=DAILY_TTL,
    schema=[
        Field(name="stock_g", dtype=Float64),
        Field(name="avg_daily_consumption_30d_g", dtype=Float64),
        Field(name="stock_coverage_days", dtype=Float64),
    ],
    source=stock_coverage_source,
    online=True,
    description="Last stock level and days it lasts at the 30d burn rate (capped at 365).",
)

supplier_lead_time = FeatureView(
    name="supplier_lead_time",
    entities=[pharmacy, ingredient],
    # Lead-time rows only exist once at least one order was received; the
    # table is still daily, so the same ttl applies.
    ttl=DAILY_TTL,
    schema=[
        Field(name="lead_time_avg_last5_days", dtype=Float64),
        Field(name="lead_time_last_days", dtype=Float64),
        Field(name="received_orders_count", dtype=Int64),
    ],
    source=supplier_lead_time_source,
    online=True,
    description="Supplier lead time (order -> receipt) over the last 5 received orders.",
)

expiration_days = FeatureView(
    name="expiration_days",
    entities=[pharmacy, ingredient],
    ttl=DAILY_TTL,
    schema=[
        Field(name="days_to_nearest_expiration", dtype=Float64),
        Field(name="lots_expiring_30d", dtype=Int64),
        Field(name="active_lots", dtype=Int64),
    ],
    source=expiration_source,
    online=True,
    description="Days until the earliest-expiring lot still in stock expires.",
)

# ---------------------------------------------------------------------------
# Pharmacy-level feature views
# ---------------------------------------------------------------------------
quote_conversion_rate = FeatureView(
    name="quote_conversion_rate",
    entities=[pharmacy],
    ttl=DAILY_TTL,
    schema=[
        Field(name="quotes_created_30d", dtype=Int64),
        Field(name="quotes_converted_30d", dtype=Int64),
        Field(name="quote_conversion_rate_30d", dtype=Float64),
    ],
    source=quote_conversion_source,
    online=True,
    description="Share of quotes created in the last 30 days already converted.",
)

production_queue_size = FeatureView(
    name="production_queue_size",
    entities=[pharmacy],
    ttl=DAILY_TTL,
    schema=[Field(name="production_queue_size", dtype=Int64)],
    source=production_source,
    online=True,
    description="Production orders queued but not yet started at snapshot time.",
)

avg_production_time = FeatureView(
    name="avg_production_time",
    entities=[pharmacy],
    ttl=DAILY_TTL,
    schema=[
        Field(name="avg_production_time_30d_min", dtype=Float64),
        Field(name="finished_orders_30d", dtype=Int64),
    ],
    source=production_source,
    online=True,
    description="Mean start->finish minutes of orders finished in the last 30 days.",
)

#: Everything ``pharma_fs.store.apply_definitions`` registers, in one list.
ALL_ENTITIES = [pharmacy, ingredient]
ALL_FEATURE_VIEWS = [
    ingredient_consumption_7d,
    ingredient_consumption_30d,
    stock_coverage_days,
    supplier_lead_time,
    expiration_days,
    quote_conversion_rate,
    production_queue_size,
    avg_production_time,
]
