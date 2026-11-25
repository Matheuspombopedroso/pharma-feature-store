"""Feature definition tests: the registry matches what the feature tables contain."""

from __future__ import annotations

from datetime import timedelta

from pharma_fs.definitions import ALL_FEATURE_VIEWS
from pharma_fs.features import MAX_COVERAGE_DAYS

EXPECTED_VIEWS = {
    "ingredient_consumption_7d": {"pharmacy", "ingredient"},
    "ingredient_consumption_30d": {"pharmacy", "ingredient"},
    "stock_coverage_days": {"pharmacy", "ingredient"},
    "supplier_lead_time": {"pharmacy", "ingredient"},
    "expiration_days": {"pharmacy", "ingredient"},
    "quote_conversion_rate": {"pharmacy"},
    "production_queue_size": {"pharmacy"},
    "avg_production_time": {"pharmacy"},
}

SOURCE_TABLE = {
    "ingredient_consumption_7d": "ingredient_consumption",
    "ingredient_consumption_30d": "ingredient_consumption",
    "stock_coverage_days": "stock_coverage",
    "supplier_lead_time": "supplier_lead_time",
    "expiration_days": "expiration",
    "quote_conversion_rate": "quote_conversion",
    "production_queue_size": "production",
    "avg_production_time": "production",
}


def test_all_expected_views_with_entities():
    got = {fv.name: set(fv.entities) for fv in ALL_FEATURE_VIEWS}
    assert got == EXPECTED_VIEWS


def test_views_are_online_with_daily_ttl():
    for fv in ALL_FEATURE_VIEWS:
        assert fv.online, fv.name
        assert fv.ttl == timedelta(days=2), fv.name


def test_feature_names_are_unique_across_views():
    # The API returns features by bare name, so collisions would overwrite values.
    names = [f.name for fv in ALL_FEATURE_VIEWS for f in fv.features]
    assert len(names) == len(set(names))


def test_registry_round_trip(small_world):
    registered = {fv.name for fv in small_world.store.list_feature_views()}
    assert registered == set(EXPECTED_VIEWS)
    assert {e.name for e in small_world.store.list_entities()} == {"pharmacy", "ingredient"}


def test_schema_columns_exist_in_source_tables(small_world):
    for fv in ALL_FEATURE_VIEWS:
        df = small_world.tables[SOURCE_TABLE[fv.name]]
        join_keys = {"pharmacy": "pharmacy_id", "ingredient": "ingredient_id"}
        for ent in fv.entities:
            assert join_keys[ent] in df.columns
        for field in fv.features:
            assert field.name in df.columns, (fv.name, field.name)
        assert str(df["event_timestamp"].dtype) == "datetime64[us, UTC]"


def test_feature_value_ranges(small_world):
    t = small_world.tables
    assert (t["ingredient_consumption"]["consumption_7d_g"] >= 0).all()
    assert (
        t["ingredient_consumption"]["consumption_30d_g"]
        >= t["ingredient_consumption"]["consumption_7d_g"] - 1e-6
    ).all()
    cov = t["stock_coverage"]["stock_coverage_days"]
    assert cov.between(0, MAX_COVERAGE_DAYS).all()
    rate = t["quote_conversion"]["quote_conversion_rate_30d"]
    assert rate.between(0, 1).all()
    assert (t["production"]["production_queue_size"] >= 0).all()
