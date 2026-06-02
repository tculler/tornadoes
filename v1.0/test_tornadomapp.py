"""
Unit tests for tornadomapp.py helper functions.
Run with:  python -m pytest test_tornadomapp.py -v
"""
# ---------------------------------------------------------------------------
# test_tornadomapp.py
#
# Unit tests for tornadomapp helper functions.
#
# Coverage areas
# ──────────────
# - normalise_intensity          – EF/F scale normalisation
# - season_color                 – month → RGBA colour
# - parse_wkt_endpoints          – WKT geometry parsing
# - default_year_range           – default UI year slider values
# - grouped location helpers     – country headers + location resolution
# - pick_column / series_or_default – DataFrame column helpers
# - format_us_county_name        – county label formatting
# - resolve_us_county_name       – point-in-polygon county lookup
# - resolve_noaa_area_name       – area name priority chain
# - SQLite cache layer           – _init_db, _is_cache_fresh,
#                                  _set_fetch_log, round-trip
#                                  (_df_to_db_rows / _rows_to_df),
#                                  noaa & canada read/write helpers
# - fetch_daily_temperature      – DB hit, DB miss+API, API error path
# - celsius_to_fahrenheit /
#   format_temperature           – unit conversion helpers
# - _batch_query_temperature_cache – bulk SQLite temp lookup
# - _update_event_temps_in_db    – write temps back to event tables
# - fetch_remote_csv encoding    – UTF-8 / latin-1 fallback
# - visible_in_bounds_mask       – geographic bounding-box filter
# ---------------------------------------------------------------------------
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pandas as pd
import pytest
from shapely.geometry import Polygon
from shapely.strtree import STRtree

# ---------------------------------------------------------------------------
# Stub out streamlit so the module can be imported without a browser session
# ---------------------------------------------------------------------------
_st_stub = types.ModuleType("streamlit")
for _attr in (
    "cache_data", "set_page_config", "markdown", "title", "sidebar",
    "header", "radio", "toggle", "slider", "multiselect", "columns",
    "checkbox", "spinner", "warning", "info", "expander", "dataframe",
    "download_button", "metric", "caption",
):
    setattr(_st_stub, _attr, MagicMock())
_st_stub.cache_data = lambda **_kw: (lambda f: f)  # decorator passthrough
_st_stub.cache_resource = lambda **_kw: (lambda f: f)  # decorator passthrough
_st_stub.session_state = {}
sys.modules.setdefault("streamlit", _st_stub)

import tornadomapp as app  # noqa: E402  (must come after stubs)


# ---------------------------------------------------------------------------
# normalise_intensity
# ---------------------------------------------------------------------------
class TestNormaliseIntensity:
    def test_ef_scale_passthrough(self):
        for scale in ("EF0", "EF1", "EF2", "EF3", "EF4", "EF5"):
            assert app.normalise_intensity(scale) == scale

    def test_f_scale_maps_to_ef(self):
        mapping = {"F0": "EF0", "F1": "EF1", "F2": "EF2",
                   "F3": "EF3", "F4": "EF4", "F5": "EF5"}
        for f, ef in mapping.items():
            assert app.normalise_intensity(f) == ef

    def test_lowercase_input(self):
        assert app.normalise_intensity("ef3") == "EF3"
        assert app.normalise_intensity("f2") == "EF2"

    def test_unknown_values_return_unk(self):
        for val in ("Unknown", "N/A", "", "EFU", "nan", None, 999):
            assert app.normalise_intensity(val) == "Unk", f"Expected Unk for {val!r}"

    def test_whitespace_stripped(self):
        assert app.normalise_intensity("  EF2  ") == "EF2"


# ---------------------------------------------------------------------------
# season_color
# ---------------------------------------------------------------------------
class TestSeasonColor:
    SPRING = [34, 197, 94, 200]
    SUMMER = [249, 115, 22, 200]
    FALL   = [168, 85, 247, 200]
    WINTER = [59, 130, 246, 200]
    GREY   = [150, 150, 150, 180]

    def test_spring_months(self):
        for m in (3, 4, 5):
            assert app.season_color(m) == self.SPRING, f"Failed for month {m}"

    def test_summer_months(self):
        for m in (6, 7, 8):
            assert app.season_color(m) == self.SUMMER

    def test_fall_months(self):
        for m in (9, 10, 11):
            assert app.season_color(m) == self.FALL

    def test_winter_months(self):
        for m in (12, 1, 2):
            assert app.season_color(m) == self.WINTER

    def test_string_month_input(self):
        assert app.season_color("7") == self.SUMMER

    def test_invalid_month_returns_grey(self):
        assert app.season_color(None) == self.GREY
        assert app.season_color("abc") == self.GREY


# ---------------------------------------------------------------------------
# parse_wkt_endpoints
# ---------------------------------------------------------------------------
class TestParseWktEndpoints:
    def test_linestring_two_points(self):
        wkt = "LINESTRING (-83.5 42.3, -84.0 42.8)"
        slat, slon, elat, elon = app.parse_wkt_endpoints(wkt)
        assert slat == pytest.approx(42.3)
        assert slon == pytest.approx(-83.5)
        assert elat == pytest.approx(42.8)
        assert elon == pytest.approx(-84.0)

    def test_non_string_returns_nones(self):
        assert app.parse_wkt_endpoints(None) == (None, None, None, None)
        assert app.parse_wkt_endpoints(42) == (None, None, None, None)

    def test_no_coordinates_returns_nones(self):
        assert app.parse_wkt_endpoints("POINT EMPTY") == (None, None, None, None)

    def test_multipoint_uses_first_and_last(self):
        wkt = "LINESTRING (-80.0 40.0, -81.0 41.0, -82.0 42.0)"
        slat, slon, elat, elon = app.parse_wkt_endpoints(wkt)
        assert slat == pytest.approx(40.0)
        assert elat == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# default_year_range
# ---------------------------------------------------------------------------
class TestDefaultYearRange:
    def test_range_is_roughly_half_history(self):
        start, end = app.default_year_range()
        current = pd.Timestamp.now("UTC").year
        assert end == current
        span = end - start + 1
        full_span = current - app.OLDEST_DATASET_YEAR + 1
        assert abs(span - full_span // 2) <= 2

    def test_start_not_before_oldest(self):
        start, _ = app.default_year_range()
        assert start >= app.OLDEST_DATASET_YEAR


# ---------------------------------------------------------------------------
# grouped location helpers
# ---------------------------------------------------------------------------
class TestGroupedLocationOptions:
    def test_build_grouped_location_options_includes_headers_and_area_tokens(self):
        options, token_to_areas = app.build_grouped_location_options(
            {
                "United States": ["Michigan", "Ohio"],
                "Canada": ["Ontario"],
            },
            {
                "United States": {"Midwest": ["Michigan", "Ohio"]},
                "Canada": {"Central Canada": ["Ontario"]},
            },
        )
        assert "__HEADER__::United States" in options
        assert "__HEADER__::Canada" in options
        assert "__REGION__::United States::Midwest" in options
        assert "__REGION__::Canada::Central Canada" in options
        assert "__AREA__::United States::Michigan" in options
        assert "__AREA__::Canada::Ontario" in options
        assert token_to_areas["__AREA__::United States::Ohio"] == ["Ohio"]

    def test_format_location_option_formats_headers_and_plain_area_labels(self):
        assert app.format_location_option("__HEADER__::United States") == "--- United States ---"
        assert app.format_location_option("__REGION__::United States::Midwest") == "• Midwest"
        assert app.format_location_option("__AREA__::Canada::Ontario") == "Ontario"

    def test_resolve_selected_admin_areas_ignores_headers(self):
        token_to_areas = {
            "__AREA__::United States::Michigan": ["Michigan"],
            "__AREA__::Canada::Ontario": ["Ontario"],
        }
        selected = [
            "__HEADER__::United States",
            "__AREA__::United States::Michigan",
            "__AREA__::Canada::Ontario",
        ]
        result = app.resolve_selected_admin_areas(selected, token_to_areas)
        assert result == ["Michigan", "Ontario"]

    def test_resolve_selected_admin_areas_expands_region_members(self):
        token_to_areas = {
            "__REGION__::United States::Midwest": ["Michigan", "Ohio", "Wisconsin"],
            "__AREA__::Canada::Ontario": ["Ontario"],
        }
        selected = [
            "__REGION__::United States::Midwest",
            "__AREA__::Canada::Ontario",
        ]
        result = app.resolve_selected_admin_areas(selected, token_to_areas)
        assert result == ["Michigan", "Ohio", "Ontario", "Wisconsin"]

    def test_resolve_selected_admin_areas_deduplicates_and_sorts(self):
        token_to_areas = {
            "__AREA__::United States::Ohio": ["Ohio"],
            "__AREA__::United States::Michigan": ["Michigan"],
            "__REGION__::United States::Midwest": ["Michigan", "Ohio", "Wisconsin"],
            "__AREA__::Canada::Ontario": ["Ontario"],
        }
        selected = [
            "__AREA__::United States::Ohio",
            "__AREA__::United States::Michigan",
            "__REGION__::United States::Midwest",
            "__AREA__::Canada::Ontario",
        ]
        result = app.resolve_selected_admin_areas(selected, token_to_areas)
        assert result == ["Michigan", "Ohio", "Ontario", "Wisconsin"]


# ---------------------------------------------------------------------------
# pick_column / series_or_default
# ---------------------------------------------------------------------------
class TestPickColumn:
    def test_exact_match(self):
        df = pd.DataFrame({"State": [1], "Year": [2]})
        assert app.pick_column(df, ["state", "province"]) == "State"

    def test_no_match_returns_none(self):
        df = pd.DataFrame({"X": [1]})
        assert app.pick_column(df, ["year"]) is None


class TestSeriesOrDefault:
    def test_found_column(self):
        df = pd.DataFrame({"year": [2020, 2021]})
        s = app.series_or_default(df, ["year", "yr"])
        assert list(s) == [2020, 2021]

    def test_missing_column_uses_default(self):
        df = pd.DataFrame({"x": [1, 2]})
        s = app.series_or_default(df, ["year"], default=0)
        assert list(s) == [0, 0]


# ---------------------------------------------------------------------------
# county resolution helpers
# ---------------------------------------------------------------------------
class TestFormatUsCountyName:
    def test_appends_lsad(self):
        assert app.format_us_county_name({"NAME": "Autauga", "LSAD": "County"}) == "Autauga County"

    def test_preserves_existing_suffix(self):
        assert app.format_us_county_name({"NAME": "St. Louis city", "LSAD": "city"}) == "St. Louis city"


class TestResolveUsCountyName:
    def test_returns_matching_county_for_point(self, monkeypatch):
        geometries = [
            Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            Polygon([(2, 2), (3, 2), (3, 3), (2, 3)]),
        ]
        monkeypatch.setattr(
            app,
            "get_us_county_lookup",
            lambda: (geometries, STRtree(geometries), ["Alpha County", "Beta County"]),
        )
        assert app.resolve_us_county_name(0.5, 0.5) == "Alpha County"

    def test_returns_none_when_point_not_in_any_county(self, monkeypatch):
        geometries = [Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])]
        monkeypatch.setattr(
            app,
            "get_us_county_lookup",
            lambda: (geometries, STRtree(geometries), ["Alpha County"]),
        )
        assert app.resolve_us_county_name(5, 5) is None


class TestResolveNoaaAreaName:
    def test_prefers_coordinate_derived_county(self, monkeypatch):
        monkeypatch.setattr(app, "resolve_us_county_name", lambda *_args: "Autauga County")
        result = app.resolve_noaa_area_name(32.5, -86.5, "Near Prattville", "Montgomery")
        assert result == "Autauga County"

    def test_falls_back_to_begin_location(self, monkeypatch):
        monkeypatch.setattr(app, "resolve_us_county_name", lambda *_args: None)
        result = app.resolve_noaa_area_name(32.5, -86.5, "Near Prattville", "Montgomery")
        assert result == "Near Prattville"

    def test_falls_back_to_cz_name(self, monkeypatch):
        monkeypatch.setattr(app, "resolve_us_county_name", lambda *_args: None)
        result = app.resolve_noaa_area_name(32.5, -86.5, None, "Montgomery")
        assert result == "Montgomery"


class TestNormalizeCanadaAdminArea:
    def test_abbreviation_maps_to_full_name(self):
        assert app.normalize_canada_admin_area("ON") == "Ontario"

    def test_full_name_is_preserved(self):
        assert app.normalize_canada_admin_area("British Columbia") == "British Columbia"

    def test_unknown_values_fall_back_to_original_text(self):
        assert app.normalize_canada_admin_area("North of Somewhere") == "North of Somewhere"


# ---------------------------------------------------------------------------
# SQLite cache layer
# ---------------------------------------------------------------------------

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class TestIsCacheFresh:
    """_is_cache_fresh returns True only when the fetch_log entry exists and
    is younger than CACHE_TTL_DAYS (7 days)."""

    def test_missing_entry_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        assert app._is_cache_fresh("noaa", "2020") is False

    def test_fresh_entry_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        app._set_fetch_log("noaa", "2020")
        assert app._is_cache_fresh("noaa", "2020") is True

    def test_stale_entry_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        # Write a timestamp older than the TTL.
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(days=app.CACHE_TTL_DAYS + 1)
        ).isoformat()
        with sqlite3.connect(tmp_path / "t.db") as conn:
            conn.execute(
                "INSERT INTO fetch_log(source, cache_key, fetched_at) VALUES(?,?,?)",
                ("noaa", "2020", stale_ts),
            )
        assert app._is_cache_fresh("noaa", "2020") is False

    def test_different_key_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        app._set_fetch_log("noaa", "2020")
        assert app._is_cache_fresh("noaa", "2021") is False

    def test_different_source_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        app._set_fetch_log("canada", "all")
        assert app._is_cache_fresh("noaa", "all") is False


class TestEnsureFreshDataCheck:
    """_ensure_fresh_data_check marks current-year and Canada data for refresh if stale."""

    def test_marks_current_year_for_refresh_when_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(days=app.CACHE_TTL_DAYS + 1)
        ).isoformat()
        current_year_str = str(pd.Timestamp.now("UTC").year)
        with sqlite3.connect(tmp_path / "t.db") as conn:
            conn.execute(
                "INSERT INTO fetch_log VALUES(?,?,?)",
                ("noaa", current_year_str, stale_ts),
            )
        app._ensure_fresh_data_check()
        # Current year entry should be deleted for re-fetch
        assert app._is_cache_fresh("noaa", current_year_str) is False
        with sqlite3.connect(tmp_path / "t.db") as conn:
            row = conn.execute(
                f"SELECT 1 FROM fetch_log WHERE source='noaa' AND cache_key='{current_year_str}'"
            ).fetchone()
        assert row is None

    def test_preserves_old_years_when_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(days=app.CACHE_TTL_DAYS + 1)
        ).isoformat()
        with sqlite3.connect(tmp_path / "t.db") as conn:
            # Old year entry
            conn.execute(
                "INSERT INTO fetch_log VALUES(?,?,?)",
                ("noaa", "2015", stale_ts),
            )
        app._ensure_fresh_data_check()
        # Old years should not be deleted
        with sqlite3.connect(tmp_path / "t.db") as conn:
            row = conn.execute(
                "SELECT 1 FROM fetch_log WHERE source='noaa' AND cache_key='2015'"
            ).fetchone()
        assert row is not None

    def test_marks_canada_for_refresh_when_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(days=app.CACHE_TTL_DAYS + 1)
        ).isoformat()
        with sqlite3.connect(tmp_path / "t.db") as conn:
            conn.execute(
                "INSERT INTO fetch_log VALUES(?,?,?)",
                (app.CANADA_SOURCE_NAME, "all", stale_ts),
            )
        app._ensure_fresh_data_check()
        # Canada entry should be deleted for re-fetch
        with sqlite3.connect(tmp_path / "t.db") as conn:
            row = conn.execute(
                f"SELECT 1 FROM fetch_log WHERE source=? AND cache_key='all'",
                (app.CANADA_SOURCE_NAME,),
            ).fetchone()
        assert row is None

    def test_does_nothing_when_cache_is_fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        current_year_str = str(pd.Timestamp.now("UTC").year)
        app._set_fetch_log("noaa", current_year_str)
        app._ensure_fresh_data_check()
        # Entry should be preserved (still fresh)
        assert app._is_cache_fresh("noaa", current_year_str) is True


def _make_minimal_df() -> "pd.DataFrame":
    """Return a one-row normalised DataFrame for round-trip tests."""
    return pd.DataFrame(
        {
            "COUNTRY": ["United States"],
            "ADMIN_AREA": ["Michigan"],
            "AREA_NAME": ["Test County"],
            "YEAR": [2020],
            "MONTH_NUM": [6],
            "BEGIN_DATE_TIME": pd.to_datetime(["2020-06-01"]),
            "END_DATE_TIME": pd.to_datetime(["2020-06-01"]),
            "INTENSITY": ["EF1"],
            "TRACK_LENGTH": [1.5],
            "TRACK_WIDTH": [100.0],
            "BEGIN_LAT": [43.0],
            "BEGIN_LON": [-84.0],
            "END_LAT": [43.1],
            "END_LON": [-83.9],
            "EVENT_NARRATIVE": ["Test event"],
            "SOURCE_DB": ["NOAA Storm Events"],
            "TRACK_LENGTH_YARDS_CORRECTED": [False],
            "track": [[[-84.0, 43.0], [-83.9, 43.1]]],
            "TEMP_HIGH_C": [None],
            "TEMP_LOW_C": [None],
        }
    )


class TestNoaaCacheRoundTrip:
    """Data written to the noaa_cache table can be read back accurately."""

    def test_write_then_read_returns_matching_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df()
        app._write_noaa_to_db(2020, df)
        result = app._read_noaa_from_db(2020)
        assert len(result) == 1
        assert result.iloc[0]["ADMIN_AREA"] == "Michigan"
        assert result.iloc[0]["INTENSITY"] == "EF1"
        assert abs(result.iloc[0]["BEGIN_LAT"] - 43.0) < 1e-6

    def test_write_replaces_existing_year(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df()
        app._write_noaa_to_db(2020, df)
        # Write again for the same year — should replace, not append.
        app._write_noaa_to_db(2020, df)
        result = app._read_noaa_from_db(2020)
        assert len(result) == 1

    def test_read_missing_year_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        result = app._read_noaa_from_db(1900)
        assert result.empty

    def test_track_column_round_trips_as_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df()
        app._write_noaa_to_db(2020, df)
        result = app._read_noaa_from_db(2020)
        track = result.iloc[0]["track"]
        assert isinstance(track, list)
        assert track == [[-84.0, 43.0], [-83.9, 43.1]]

    def test_boolean_correction_flag_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df().copy()
        df["TRACK_LENGTH_YARDS_CORRECTED"] = [True]
        app._write_noaa_to_db(2020, df)
        result = app._read_noaa_from_db(2020)
        assert bool(result.iloc[0]["TRACK_LENGTH_YARDS_CORRECTED"]) is True


class TestCanadaCacheRoundTrip:
    """Data written to the canada_cache table can be read back accurately."""

    def test_write_then_read_returns_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df().copy()
        df["COUNTRY"] = ["Canada"]
        df["ADMIN_AREA"] = ["Ontario"]
        df["SOURCE_DB"] = ["Canadian National Tornado Database"]
        app._write_canada_to_db(df)
        result = app._read_canada_from_db()
        assert len(result) == 1
        assert result.iloc[0]["COUNTRY"] == "Canada"

    def test_write_replaces_all_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df().copy()
        df["COUNTRY"] = ["Canada"]
        df["ADMIN_AREA"] = ["Ontario"]
        df["SOURCE_DB"] = ["Canadian National Tornado Database"]
        app._write_canada_to_db(df)
        app._write_canada_to_db(df)
        result = app._read_canada_from_db()
        assert len(result) == 1  # Replace, not duplicate.

    def test_read_empty_db_returns_empty_frame(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        result = app._read_canada_from_db()
        assert result.empty


class TestFetchDailyTemperature:
    """fetch_daily_temperature checks SQLite first, then calls the API."""

    def test_returns_cached_values_without_api_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        with sqlite3.connect(tmp_path / "t.db") as conn:
            conn.execute(
                "INSERT INTO temperature_cache VALUES(?,?,?,?,?)",
                (43.0, -84.0, "2020-06-01", 28.5, 15.2),
            )
        with patch("tornadomapp.requests.get") as mock_get:
            result = app.fetch_daily_temperature(43.0, -84.0, "2020-06-01")
        mock_get.assert_not_called()
        assert result == (28.5, 15.2)

    def test_calls_api_on_cache_miss_and_stores_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        api_payload = {
            "daily": {
                "temperature_2m_max": [30.0],
                "temperature_2m_min": [18.0],
            }
        }
        mock_response = MagicMock()
        mock_response.json.return_value = api_payload
        mock_response.raise_for_status.return_value = None
        with patch("tornadomapp.requests.get", return_value=mock_response):
            result = app.fetch_daily_temperature(43.0, -84.0, "2020-06-02")
        assert result == (30.0, 18.0)
        # Value should now be persisted.
        with sqlite3.connect(tmp_path / "t.db") as conn:
            row = conn.execute(
                "SELECT high_celsius, low_celsius FROM temperature_cache "
                "WHERE lat=43.0 AND lon=-84.0 AND date_str='2020-06-02'"
            ).fetchone()
        assert row == (30.0, 18.0)

    def test_api_error_returns_none_tuple(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        with patch("tornadomapp.requests.get", side_effect=Exception("network error")):
            result = app.fetch_daily_temperature(99.0, 99.0, "2020-01-01")
        assert result == (None, None)


class TestVisibleInBoundsMask:
    """visible_in_bounds_mask filters events to the visible map viewport."""

    def _df(self) -> "pd.DataFrame":
        return pd.DataFrame(
            {
                "BEGIN_LAT": [40.0, 50.0, 45.0],
                "BEGIN_LON": [-90.0, -70.0, -80.0],
                "END_LAT": [41.0, 51.0, 46.0],
                "END_LON": [-89.0, -69.0, -79.0],
            }
        )

    def test_all_inside_bounds(self):
        df = self._df()
        bounds = (35.0, -95.0, 55.0, -65.0)
        mask = app.visible_in_bounds_mask(df, bounds)
        assert mask.all()

    def test_none_inside_narrow_bounds(self):
        df = self._df()
        bounds = (60.0, -95.0, 70.0, -65.0)
        mask = app.visible_in_bounds_mask(df, bounds)
        assert not mask.any()

    def test_partial_overlap(self):
        df = self._df()
        bounds = (39.0, -91.0, 42.0, -88.0)
        mask = app.visible_in_bounds_mask(df, bounds)
        assert bool(mask.iloc[0])
        assert not mask.iloc[1]
        assert not mask.iloc[2]


# ---------------------------------------------------------------------------
# celsius_to_fahrenheit / format_temperature
# ---------------------------------------------------------------------------
class TestCelsiusToFahrenheit:
    def test_freezing(self):
        assert app.celsius_to_fahrenheit(0.0) == pytest.approx(32.0)

    def test_boiling(self):
        assert app.celsius_to_fahrenheit(100.0) == pytest.approx(212.0)

    def test_body_temp(self):
        assert app.celsius_to_fahrenheit(37.0) == pytest.approx(98.6)


class TestFormatTemperature:
    def test_none_returns_dash(self):
        assert app.format_temperature(None, use_fahrenheit=True) == "\u2014"
        assert app.format_temperature(None, use_fahrenheit=False) == "\u2014"

    def test_fahrenheit_format(self):
        result = app.format_temperature(0.0, use_fahrenheit=True)
        assert "\u00b0F" in result
        assert "32.0" in result

    def test_celsius_format(self):
        result = app.format_temperature(20.0, use_fahrenheit=False)
        assert "\u00b0C" in result
        assert "20.0" in result


# ---------------------------------------------------------------------------
# _batch_query_temperature_cache
# ---------------------------------------------------------------------------
class TestBatchQueryTemperatureCache:
    def test_returns_stored_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        app._init_db()
        with sqlite3.connect(tmp_path / "t.db") as conn:
            conn.execute(
                "INSERT INTO temperature_cache VALUES(?,?,?,?,?)",
                (43.0, -84.0, "2020-06-01", 28.5, 15.2),
            )
        combos = pd.DataFrame(
            {"_lat_r": [43.0], "_lon_r": [-84.0], "_date_s": ["2020-06-01"]}
        )
        result = app._batch_query_temperature_cache(combos)
        assert (43.0, -84.0, "2020-06-01") in result
        assert result[(43.0, -84.0, "2020-06-01")] == (28.5, 15.2)

    def test_empty_input_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        combos = pd.DataFrame(columns=["_lat_r", "_lon_r", "_date_s"])
        result = app._batch_query_temperature_cache(combos)
        assert result == {}

    def test_missing_key_not_in_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        combos = pd.DataFrame(
            {"_lat_r": [99.0], "_lon_r": [99.0], "_date_s": ["2099-01-01"]}
        )
        result = app._batch_query_temperature_cache(combos)
        assert result == {}


# ---------------------------------------------------------------------------
# _update_event_temps_in_db
# ---------------------------------------------------------------------------
class TestUpdateEventTempsInDb:
    def test_updates_noaa_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df()
        app._write_noaa_to_db(2020, df)
        updates = [(25.0, 12.0, app.NOAA_SOURCE_NAME, "2020-06-01 00:00:00", 43.0, -84.0)]
        app._update_event_temps_in_db(updates)
        result = app._read_noaa_from_db(2020)
        assert abs(result.iloc[0]["TEMP_HIGH_C"] - 25.0) < 1e-6
        assert abs(result.iloc[0]["TEMP_LOW_C"] - 12.0) < 1e-6

    def test_updates_canada_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.db")
        df = _make_minimal_df().copy()
        df["COUNTRY"] = ["Canada"]
        df["SOURCE_DB"] = [app.CANADA_SOURCE_NAME]
        app._write_canada_to_db(df)
        updates = [(18.0, 5.0, app.CANADA_SOURCE_NAME, "2020-06-01 00:00:00", 43.0, -84.0)]
        app._update_event_temps_in_db(updates)
        result = app._read_canada_from_db()
        assert abs(result.iloc[0]["TEMP_HIGH_C"] - 18.0) < 1e-6


# ---------------------------------------------------------------------------
# fetch_remote_csv encoding fallback
# ---------------------------------------------------------------------------
class TestFetchRemoteCsvEncoding:
    def test_utf8_csv_parses_normally(self):
        """A UTF-8 encoded CSV is returned as a DataFrame."""
        csv_bytes = b"col1,col2\n1,2\n3,4"
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.content = csv_bytes
        mock_resp.text = csv_bytes.decode("utf-8")
        mock_resp.headers = {"content-type": "text/csv"}
        with patch("tornadomapp.requests.get", return_value=mock_resp):
            df = app.fetch_remote_csv("http://example.com/data.csv")
        assert list(df.columns) == ["col1", "col2"]
        assert len(df) == 2

    def test_latin1_fallback_on_decode_error(self):
        """A byte sequence invalid in UTF-8 (e.g. Windows-1252 0x92) is
        handled gracefully via the latin-1 fallback."""
        # Build a csv where one field contains the Windows curly-apostrophe byte.
        raw = b"name,value\ncaf\x92,42\n"
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.content = raw
        # Simulate requests decoding failure on .text property
        mock_resp.text = raw.decode("latin-1")
        mock_resp.headers = {"content-type": "text/csv"}
        with patch("tornadomapp.requests.get", return_value=mock_resp):
            df = app.fetch_remote_csv("http://example.com/data.csv")
        assert len(df) == 1
        assert df.iloc[0]["value"] == 42

    def test_html_response_raises_value_error(self):
        """An HTML page masquerading as CSV raises ValueError."""
        html = b"<!DOCTYPE html><html><body>Not CSV</body></html>"
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.content = html
        mock_resp.text = html.decode("utf-8")
        mock_resp.headers = {"content-type": "text/html"}
        with patch("tornadomapp.requests.get", return_value=mock_resp):
            with pytest.raises(ValueError, match="HTML"):
                app.fetch_remote_csv("http://example.com/data.csv")
