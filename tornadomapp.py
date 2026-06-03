from __future__ import annotations

# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------
# v1.0  – Initial release: NOAA Storm Events + Canadian National Tornado
#          Database, interactive Folium map with season-coloured dots,
#          track polylines, hover popups, temperature overlay (Open-Meteo),
#          local SQLite cache, CSV/XLSX export, map row cap, advanced
#          settings sidebar.
# v1.1  – Added semi-transparent density heatmap overlay (folium HeatMap
#          plugin) groupable by Season, Intensity, Country, or
#          State/Province; controlled via sidebar checkbox + dropdown.
# v1.2  – Moved temperature toggle and debounce slider into Advanced sidebar
#          section; moved Export CSV/XLSX buttons to sidebar; added Google
#          Search link column to data table (date + area + admin area).
# v1.3  – Added Apply-based sidebar filter forms to reduce reruns during
#          editing, cached multi-year/Canada loads, conditional loading
#          spinner display, and viewport-signature gating for temperature
#          refresh work while panning/zooming.
# ---------------------------------------------------------------------------
__version__ = "1.3"
# ---------------------------------------------------------------------------

import calendar
import json
import math
import re
import sqlite3
import time
from contextlib import nullcontext
from urllib.parse import urlencode
from collections.abc import Iterable
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from functools import lru_cache
from io import BytesIO
from numbers import Integral
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import streamlit as st

try:
    folium = __import__("folium")
except Exception:
    folium = None

try:
    _folium_plugins = __import__("folium.plugins", fromlist=["MeasureControl"])
    MeasureControl = getattr(_folium_plugins, "MeasureControl", None)
except Exception:
    MeasureControl = None

try:
    _streamlit_folium = __import__("streamlit_folium", fromlist=["st_folium"])
    st_folium = getattr(_streamlit_folium, "st_folium", None)
except Exception:
    st_folium = None

try:
    _streamlit_autorefresh = __import__("streamlit_autorefresh", fromlist=["st_autorefresh"])
    st_autorefresh = getattr(_streamlit_autorefresh, "st_autorefresh", None)
except Exception:
    st_autorefresh = None

try:
    from shapely.geometry import Point
    from shapely.geometry import shape
    from shapely.strtree import STRtree
except ImportError:
    Point = None
    STRtree = None
    shape = None


NOAA_DIRECTORY_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"
US_COUNTY_GEOJSON_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
# The ECCC data portal (data-donnees.ec.gc.ca) became a JavaScript SPA and
# returns HTML for all paths.  The actual files are served by the Azure-backed
# API at data-donnees.az.ec.gc.ca/api/file?path=<url-encoded-blob-path>.
CANADA_EVENTS_CSV_URL = (
    "https://data-donnees.az.ec.gc.ca/api/file?path="
    "%2Fweather%2Fproducts%2Fcanadian-national-tornado-database-verified-events-1980-2009-public"
    "%2Fcanadian-national-tornado-database-verified-events-1980-2009-public-gis-en"
    "%2FGIS_CAN_VerifiedTornadoes_1980-2009.csv"
)
CANADA_TRACKS_CSV_URL = (
    "https://data-donnees.az.ec.gc.ca/api/file?path="
    "%2Fweather%2Fproducts%2Fcanadian-national-tornado-database-verified-events-1980-2009-public"
    "%2Fcanadian-national-tornado-database-verified-tracks-1980-2009-public-gis-en"
    "%2FGIS_CAN_VerifiedTracks_1980-2009.csv"
)
CANADA_SOURCE_NAME = "Canadian National Tornado Database"
NOAA_SOURCE_NAME = "NOAA Storm Events"
OLDEST_DATASET_YEAR = 1950
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TEMPERATURE_FETCH_LIMIT = 500  # max unique location+date combos fetched per refresh
MAX_MAP_ROWS = 2000  # cap on tornado events rendered in Folium to avoid browser/GPU driver crash
DEFAULT_MAP_HEIGHT = 780
DB_PATH = Path(__file__).parent / "tornado_cache.db"
CACHE_TTL_DAYS = 7
NORTHERN_US_STATES = {
    "Connecticut",
    "Idaho",
    "Iowa",
    "Maine",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Montana",
    "Nebraska",
    "New Hampshire",
    "New York",
    "North Dakota",
    "Ohio",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Dakota",
    "Vermont",
    "Washington",
    "Wisconsin",
    "Wyoming",
}

# Mapping of US state/territory abbreviations to full names
STATE_ABBR_TO_NAME = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

CANADA_PROV_ABBR_TO_NAME = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}


def normalize_canada_admin_area(value: object) -> str:
    text = clean_text(value)
    if not text:
        return "Unknown"
    key = text.upper()
    return CANADA_PROV_ABBR_TO_NAME.get(key, text)

US_REGIONS: dict[str, list[str]] = {
    "Great Lakes": [
        "Michigan",
        "Wisconsin",
        "Minnesota",
        "Illinois",
        "Indiana",
        "Ohio",
        "Pennsylvania",
        "New York",
    ],
    "Midwest": [
        "Illinois",
        "Indiana",
        "Michigan",
        "Ohio",
        "Wisconsin",
        "Iowa",
        "Kansas",
        "Minnesota",
        "Missouri",
        "Nebraska",
        "North Dakota",
        "South Dakota",
    ],
    "Northeast": [
        "Connecticut",
        "Maine",
        "Massachusetts",
        "New Hampshire",
        "Rhode Island",
        "Vermont",
        "New Jersey",
        "New York",
        "Pennsylvania",
    ],
    "South": [
        "Delaware",
        "District of Columbia",
        "Florida",
        "Georgia",
        "Maryland",
        "North Carolina",
        "South Carolina",
        "Virginia",
        "West Virginia",
        "Alabama",
        "Kentucky",
        "Mississippi",
        "Tennessee",
        "Arkansas",
        "Louisiana",
        "Oklahoma",
        "Texas",
    ],
    "West": [
        "Arizona",
        "Colorado",
        "Idaho",
        "Montana",
        "Nevada",
        "New Mexico",
        "Utah",
        "Wyoming",
        "Alaska",
        "California",
        "Hawaii",
        "Oregon",
        "Washington",
    ],
}
CANADA_REGIONS: dict[str, list[str]] = {
    "Atlantic Canada": [
        "New Brunswick",
        "Newfoundland and Labrador",
        "Nova Scotia",
        "Prince Edward Island",
    ],
    "Central Canada": ["Ontario", "Quebec"],
    "Prairies": ["Alberta", "Manitoba", "Saskatchewan"],
    "Pacific": ["British Columbia"],
    "Northern Canada": ["Northwest Territories", "Nunavut", "Yukon"],
}
ALL_REGIONS: dict[str, list[str]] = {**US_REGIONS, **CANADA_REGIONS}
MONTH_NAMES = {index: name for index, name in enumerate(calendar.month_name) if index}
MONTH_ABBR = {index: abbr for index, abbr in enumerate(calendar.month_abbr) if index}
SEASON_MONTHS: dict[str, list[int]] = {
    "Spring": [3, 4, 5],
    "Summer": [6, 7, 8],
    "Fall": [9, 10, 11],
    "Winter": [12, 1, 2],
}
SEASON_ICONS: dict[str, str] = {
    "Spring": "🟢",
    "Summer": "🟠",
    "Fall": "🟣",
    "Winter": "🔵",
}
NOAA_COLUMNS = [
    "STATE",
    "YEAR",
    "MONTH_NAME",
    "BEGIN_DATE_TIME",
    "END_DATE_TIME",
    "EVENT_TYPE",
    "TOR_F_SCALE",
    "TOR_LENGTH",
    "TOR_WIDTH",
    "CZ_NAME",
    "BEGIN_LOCATION",
    "BEGIN_LAT",
    "BEGIN_LON",
    "END_LAT",
    "END_LON",
    "EPISODE_NARRATIVE",
    "EVENT_NARRATIVE",
]
NORMALIZED_COLUMNS = [
    "COUNTRY",
    "ADMIN_AREA",
    "AREA_NAME",
    "YEAR",
    "MONTH_NUM",
    "BEGIN_DATE_TIME",
    "END_DATE_TIME",
    "INTENSITY",
    "TRACK_LENGTH",
    "TRACK_WIDTH",
    "BEGIN_LAT",
    "BEGIN_LON",
    "END_LAT",
    "END_LON",
    "EVENT_NARRATIVE",
    "SOURCE_DB",
    "TRACK_LENGTH_YARDS_CORRECTED",
    "track",
    "TEMP_HIGH_C",
    "TEMP_LOW_C",
]


CANADIAN_PROVINCES_AND_TERRITORIES = sorted(
    {area for areas in CANADA_REGIONS.values() for area in areas}
)
US_STATES = sorted(
    {area for region_name, areas in US_REGIONS.items() if region_name != "Great Lakes" for area in areas}
)
ALL_ADMIN_AREAS = sorted(set(US_STATES + CANADIAN_PROVINCES_AND_TERRITORIES))
COUNTRY_ADMIN_AREAS: dict[str, list[str]] = {
    "United States": US_STATES,
    "Canada": CANADIAN_PROVINCES_AND_TERRITORIES,
}
COUNTRY_REGIONS: dict[str, dict[str, list[str]]] = {
    "United States": US_REGIONS,
    "Canada": CANADA_REGIONS,
}


def empty_normalized_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=NORMALIZED_COLUMNS)


def default_year_range() -> tuple[int, int]:
    current_year = pd.Timestamp.now("UTC").year
    past_years = max(1, (current_year - OLDEST_DATASET_YEAR + 1) // 2)
    return max(OLDEST_DATASET_YEAR, current_year - past_years + 1), current_year


def pick_column(dataframe: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    lookup = {column.lower(): column for column in dataframe.columns}
    for candidate in candidates:
        column = lookup.get(candidate.lower())
        if column:
            return column
    return None


def series_or_default(dataframe: pd.DataFrame, candidates: Iterable[str], default: object = pd.NA) -> pd.Series:
    column = pick_column(dataframe, candidates)
    if column is None:
        return pd.Series([default] * len(dataframe), index=dataframe.index)
    return dataframe[column]


def parse_wkt_endpoints(value: object) -> tuple[float | None, float | None, float | None, float | None]:
    if not isinstance(value, str):
        return None, None, None, None

    matches = re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", value)
    if not matches:
        return None, None, None, None

    start_lon, start_lat = (float(item) for item in matches[0])
    end_lon, end_lat = (float(item) for item in matches[-1])
    return start_lat, start_lon, end_lat, end_lon


def clean_text(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None

    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def coerce_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def format_us_county_name(properties: dict[str, object]) -> str:
    name = clean_text(properties.get("NAME")) or "Unknown"
    lsad = clean_text(properties.get("LSAD"))
    if lsad and not name.endswith(lsad):
        return f"{name} {lsad}"
    return name


# ---------------------------------------------------------------------------
# SQLite persistence layer
# ---------------------------------------------------------------------------

_DB_COLS = [
    "COUNTRY", "ADMIN_AREA", "AREA_NAME", "year_num", "month_num",
    "BEGIN_DATE_TIME", "END_DATE_TIME", "INTENSITY", "TRACK_LENGTH", "TRACK_WIDTH",
    "BEGIN_LAT", "BEGIN_LON", "END_LAT", "END_LON", "EVENT_NARRATIVE", "SOURCE_DB",
    "TRACK_LENGTH_YARDS_CORRECTED", "track",
    "temp_high_celsius", "temp_low_celsius",
]


def _init_db() -> None:
    """Create SQLite tables if they don't already exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS noaa_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                country TEXT, admin_area TEXT, area_name TEXT,
                year_num REAL, month_num REAL,
                begin_datetime TEXT, end_datetime TEXT,
                intensity TEXT, track_length REAL, track_width REAL,
                begin_lat REAL, begin_lon REAL, end_lat REAL, end_lon REAL,
                event_narrative TEXT, source_db TEXT,
                track_length_yards_corrected INTEGER, track TEXT,
                temp_high_celsius REAL, temp_low_celsius REAL
            );
            CREATE INDEX IF NOT EXISTS idx_noaa_year ON noaa_cache(year);
            CREATE TABLE IF NOT EXISTS canada_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country TEXT, admin_area TEXT, area_name TEXT,
                year_num REAL, month_num REAL,
                begin_datetime TEXT, end_datetime TEXT,
                intensity TEXT, track_length REAL, track_width REAL,
                begin_lat REAL, begin_lon REAL, end_lat REAL, end_lon REAL,
                event_narrative TEXT, source_db TEXT,
                track_length_yards_corrected INTEGER, track TEXT,
                temp_high_celsius REAL, temp_low_celsius REAL
            );
            CREATE TABLE IF NOT EXISTS fetch_log (
                source TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (source, cache_key)
            );
            CREATE TABLE IF NOT EXISTS temperature_cache (
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                date_str TEXT NOT NULL,
                high_celsius REAL,
                low_celsius REAL,
                PRIMARY KEY (lat, lon, date_str)
            );
        """)
        # Safe migration: add temperature columns to event tables for existing DBs.
        for _tbl in ("noaa_cache", "canada_cache"):
            for _col in ("temp_high_celsius REAL", "temp_low_celsius REAL"):
                try:
                    conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col}")
                except Exception:
                    pass


def _is_cache_fresh(source: str, key: str) -> bool:
    """Return True when the cached entry is younger than CACHE_TTL_DAYS."""
    try:
        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT fetched_at FROM fetch_log WHERE source=? AND cache_key=?",
                (source, key),
            ).fetchone()
        if row is None:
            return False
        fetched_at = datetime.fromisoformat(row[0])
        age = datetime.now(timezone.utc) - fetched_at
        return age < timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return False


def _set_fetch_log(source: str, key: str) -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(source, cache_key, fetched_at) VALUES(?,?,?)",
                (source, key, datetime.now(timezone.utc).isoformat()),
            )
    except Exception:
        pass


def _df_to_db_rows(df: pd.DataFrame) -> list[tuple]:
    rows = []
    for _, row in df.iterrows():
        rows.append((
            row.get("COUNTRY"), row.get("ADMIN_AREA"), row.get("AREA_NAME"),
            float(row["YEAR"]) if pd.notna(row.get("YEAR")) else None,
            float(row["MONTH_NUM"]) if pd.notna(row.get("MONTH_NUM")) else None,
            str(row["BEGIN_DATE_TIME"]) if pd.notna(row.get("BEGIN_DATE_TIME")) else None,
            str(row["END_DATE_TIME"]) if pd.notna(row.get("END_DATE_TIME")) else None,
            row.get("INTENSITY"),
            float(row["TRACK_LENGTH"]) if pd.notna(row.get("TRACK_LENGTH")) else None,
            float(row["TRACK_WIDTH"]) if pd.notna(row.get("TRACK_WIDTH")) else None,
            float(row["BEGIN_LAT"]) if pd.notna(row.get("BEGIN_LAT")) else None,
            float(row["BEGIN_LON"]) if pd.notna(row.get("BEGIN_LON")) else None,
            float(row["END_LAT"]) if pd.notna(row.get("END_LAT")) else None,
            float(row["END_LON"]) if pd.notna(row.get("END_LON")) else None,
            row.get("EVENT_NARRATIVE"), row.get("SOURCE_DB"),
            int(bool(row.get("TRACK_LENGTH_YARDS_CORRECTED", False))),
            json.dumps(row.get("track")) if row.get("track") is not None else None,
            float(row["TEMP_HIGH_C"]) if pd.notna(row.get("TEMP_HIGH_C")) else None,
            float(row["TEMP_LOW_C"]) if pd.notna(row.get("TEMP_LOW_C")) else None,
        ))
    return rows


def _rows_to_df(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=_DB_COLS)
    df = df.rename(columns={"year_num": "YEAR", "month_num": "MONTH_NUM",
                             "begin_datetime": "BEGIN_DATE_TIME", "end_datetime": "END_DATE_TIME",
                             "begin_lat": "BEGIN_LAT", "begin_lon": "BEGIN_LON",
                             "end_lat": "END_LAT", "end_lon": "END_LON",
                             "intensity": "INTENSITY", "track_length": "TRACK_LENGTH",
                             "track_width": "TRACK_WIDTH", "event_narrative": "EVENT_NARRATIVE",
                             "source_db": "SOURCE_DB",
                             "track_length_yards_corrected": "TRACK_LENGTH_YARDS_CORRECTED",
                             "country": "COUNTRY", "admin_area": "ADMIN_AREA",
                             "area_name": "AREA_NAME",
                             "temp_high_celsius": "TEMP_HIGH_C",
                             "temp_low_celsius": "TEMP_LOW_C"})
    for col in ("BEGIN_DATE_TIME", "END_DATE_TIME"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    if "track" in df.columns:
        df["track"] = df["track"].apply(lambda v: json.loads(v) if isinstance(v, str) else v)
    if "TRACK_LENGTH_YARDS_CORRECTED" in df.columns:
        df["TRACK_LENGTH_YARDS_CORRECTED"] = df["TRACK_LENGTH_YARDS_CORRECTED"].astype(bool)
    return df[NORMALIZED_COLUMNS]


def _read_noaa_from_db(year: int) -> pd.DataFrame:
    try:
        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT country, admin_area, area_name, year_num, month_num, "
                "begin_datetime, end_datetime, intensity, track_length, track_width, "
                "begin_lat, begin_lon, end_lat, end_lon, event_narrative, source_db, "
                "track_length_yards_corrected, track, temp_high_celsius, temp_low_celsius "
                "FROM noaa_cache WHERE year=?",
                (year,),
            ).fetchall()
        return _rows_to_df(rows) if rows else empty_normalized_frame()
    except Exception:
        return empty_normalized_frame()


def _write_noaa_to_db(year: int, df: pd.DataFrame) -> None:
    try:
        _init_db()
        rows = _df_to_db_rows(df)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM noaa_cache WHERE year=?", (year,))
            conn.executemany(
                "INSERT INTO noaa_cache(year, country, admin_area, area_name, year_num, month_num, "
                "begin_datetime, end_datetime, intensity, track_length, track_width, "
                "begin_lat, begin_lon, end_lat, end_lon, event_narrative, source_db, "
                "track_length_yards_corrected, track, temp_high_celsius, temp_low_celsius) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(year, *r) for r in rows],
            )
    except Exception:
        pass


def _read_canada_from_db() -> pd.DataFrame:
    try:
        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT country, admin_area, area_name, year_num, month_num, "
                "begin_datetime, end_datetime, intensity, track_length, track_width, "
                "begin_lat, begin_lon, end_lat, end_lon, event_narrative, source_db, "
                "track_length_yards_corrected, track, temp_high_celsius, temp_low_celsius "
                "FROM canada_cache",
            ).fetchall()
        return _rows_to_df(rows) if rows else empty_normalized_frame()
    except Exception:
        return empty_normalized_frame()


def _write_canada_to_db(df: pd.DataFrame) -> None:
    try:
        _init_db()
        rows = _df_to_db_rows(df)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM canada_cache")
            conn.executemany(
                "INSERT INTO canada_cache(country, admin_area, area_name, year_num, month_num, "
                "begin_datetime, end_datetime, intensity, track_length, track_width, "
                "begin_lat, begin_lon, end_lat, end_lon, event_narrative, source_db, "
                "track_length_yards_corrected, track, temp_high_celsius, temp_low_celsius) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------


def _batch_query_temperature_cache(
    combos: pd.DataFrame,
) -> dict[tuple[float, float, str], tuple[float | None, float | None]]:
    """Single-query lookup of temperature_cache for all (lat_r, lon_r, date_s) combos."""
    if combos.empty:
        return {}
    result: dict[tuple[float, float, str], tuple[float | None, float | None]] = {}
    try:
        _init_db()
        placeholders = ",".join(["(?,?,?)"] * len(combos))
        params: list[object] = []
        for _, row in combos.iterrows():
            params.extend([float(row["_lat_r"]), float(row["_lon_r"]), str(row["_date_s"])])
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                f"SELECT lat, lon, date_str, high_celsius, low_celsius "
                f"FROM temperature_cache WHERE (lat, lon, date_str) IN ({placeholders})",
                params,
            ).fetchall()
        for lat, lon, date_str, high, low in rows:
            result[(float(lat), float(lon), str(date_str))] = (
                float(high) if high is not None else None,
                float(low) if low is not None else None,
            )
    except Exception:
        pass
    return result


def _update_event_temps_in_db(updates: list[tuple]) -> None:
    """Write freshly fetched temperatures back to the event cache tables.

    updates: list of (high_celsius, low_celsius, source_db, begin_datetime_str, begin_lat, begin_lon)
    """
    noaa_rows = [
        (h, lo, dt, la, ln)
        for h, lo, src, dt, la, ln in updates
        if src != CANADA_SOURCE_NAME
    ]
    canada_rows = [
        (h, lo, dt, la, ln)
        for h, lo, src, dt, la, ln in updates
        if src == CANADA_SOURCE_NAME
    ]
    try:
        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            if noaa_rows:
                conn.executemany(
                    "UPDATE noaa_cache SET temp_high_celsius=?, temp_low_celsius=? "
                    "WHERE begin_datetime=? AND begin_lat=? AND begin_lon=?",
                    noaa_rows,
                )
            if canada_rows:
                conn.executemany(
                    "UPDATE canada_cache SET temp_high_celsius=?, temp_low_celsius=? "
                    "WHERE begin_datetime=? AND begin_lat=? AND begin_lon=?",
                    canada_rows,
                )
    except Exception:
        pass


def _ensure_fresh_data_check() -> None:
    """If the database hasn't been checked in CACHE_TTL_DAYS, mark current data for refresh.

    Called once at server startup via _run_startup_maintenance(). If no data source
    has been fetched for more than CACHE_TTL_DAYS, we delete only the current/recent
    year and Canada entries so they'll be re-fetched, checking for new events. Old
    cached years remain untouched to avoid unnecessary re-downloads.
    """
    try:
        _init_db()
        current_year = pd.Timestamp.now("UTC").year
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=CACHE_TTL_DAYS)

        with sqlite3.connect(DB_PATH) as conn:
            # Check if ANY entry was fetched recently
            row = conn.execute(
                "SELECT MAX(fetched_at) FROM fetch_log"
            ).fetchone()
            if row and row[0]:
                last_fetch = datetime.fromisoformat(row[0])
                if last_fetch >= cutoff_time:
                    # Cache was touched recently, all good
                    return
            # No recent fetches — mark current year and Canada for re-check
            conn.execute("DELETE FROM fetch_log WHERE cache_key = ?", (str(current_year),))
            conn.execute("DELETE FROM fetch_log WHERE cache_key = 'all' AND source = ?", (CANADA_SOURCE_NAME,))
    except Exception:
        pass


@st.cache_resource(show_spinner=False)
def _run_startup_maintenance() -> None:
    """Execute once per Streamlit server session (not on every rerun).

    If the database hasn't been checked in CACHE_TTL_DAYS (7 days), marks
    current-year NOAA and Canada data for re-fetch so new events are picked up
    automatically. Old cached years are preserved to avoid re-downloading them.
    """
    _ensure_fresh_data_check()


# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def load_us_county_geojson() -> dict[str, object]:
    response = requests.get(US_COUNTY_GEOJSON_URL, timeout=120)
    response.raise_for_status()
    return response.json()


@lru_cache(maxsize=1)
def get_us_county_lookup() -> tuple[list[Any], Any, list[str]]:
    if Point is None or STRtree is None or shape is None:
        raise ImportError("shapely is not installed")

    geojson = load_us_county_geojson()
    features = geojson.get("features", [])
    if not isinstance(features, list):
        return [], STRtree([]), []

    geometries: list[Any] = []
    county_names: list[str] = []
    for feature in features:
        if not isinstance(feature, dict) or "geometry" not in feature:
            continue
        geometry = shape(feature["geometry"])
        geometries.append(geometry)
        county_names.append(format_us_county_name(feature.get("properties", {})))

    return geometries, STRtree(geometries), county_names


def resolve_us_county_name(begin_lat: object, begin_lon: object) -> str | None:
    latitude = coerce_float(begin_lat)
    longitude = coerce_float(begin_lon)
    if latitude is None or longitude is None or Point is None:
        return None

    try:
        geometries, tree, county_names = get_us_county_lookup()
    except Exception:
        return None

    point = Point(longitude, latitude)
    for candidate in tree.query(point):
        candidate_index = int(candidate) if isinstance(candidate, Integral) else geometries.index(candidate)
        geometry = geometries[candidate_index]
        if geometry.covers(point):
            return county_names[candidate_index]
    return None


def resolve_noaa_area_name(begin_lat: object, begin_lon: object, begin_location: object, cz_name: object) -> str:
    county_name = resolve_us_county_name(begin_lat, begin_lon)
    if county_name:
        return county_name

    start_location = clean_text(begin_location)
    if start_location:
        return start_location

    county_zone_name = clean_text(cz_name)
    if county_zone_name:
        return county_zone_name

    return "Unknown area"


@st.cache_data(show_spinner=False)
def get_year_download_url(year: int) -> str:
    response = requests.get(NOAA_DIRECTORY_URL, timeout=120)
    response.raise_for_status()

    pattern = re.compile(rf"StormEvents_details-ftp_v1\.0_d{year}_c\d{{8}}\.csv\.gz")
    matches = pattern.findall(response.text)
    if not matches:
        raise ValueError(f"No NOAA Storm Events details file was found for year {year}.")

    return NOAA_DIRECTORY_URL + sorted(set(matches))[-1]


def fetch_daily_temperature(lat: float, lon: float, date_str: str) -> tuple[float | None, float | None]:
    """Return (high_celsius, low_celsius) from the SQLite cache or Open-Meteo archive.

    Returns (None, None) on any error so callers can degrade gracefully.
    lat/lon should already be rounded to 2 decimal places for cache efficiency.
    """
    try:
        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT high_celsius, low_celsius FROM temperature_cache WHERE lat=? AND lon=? AND date_str=?",
                (lat, lon, date_str),
            ).fetchone()
        if row is not None:
            return (row[0], row[1])
    except Exception:
        pass
    # Not in cache — fetch from API.
    try:
        response = requests.get(
            OPEN_METEO_ARCHIVE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": date_str,
                "end_date": date_str,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "UTC",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        daily = data.get("daily", {})
        highs = daily.get("temperature_2m_max", [None])
        lows = daily.get("temperature_2m_min", [None])
        high = highs[0] if highs else None
        low = lows[0] if lows else None
        result = (float(high) if high is not None else None, float(low) if low is not None else None)
    except Exception:
        result = (None, None)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO temperature_cache(lat, lon, date_str, high_celsius, low_celsius)"
                " VALUES(?,?,?,?,?)",
                (lat, lon, date_str, result[0], result[1]),
            )
    except Exception:
        pass
    return result


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def format_temperature(celsius: float | None, use_fahrenheit: bool) -> str:
    if celsius is None:
        return "—"
    if use_fahrenheit:
        return f"{celsius_to_fahrenheit(celsius):.1f} °F"
    return f"{celsius:.1f} °C"


def load_noaa_tornado_data(year: int) -> pd.DataFrame:
    """Load NOAA tornado data for a single year, using a local SQLite cache.

    Data is re-fetched from NOAA only when the cached copy is older than
    CACHE_TTL_DAYS (7 days).  The first time a year is requested its full
    state/event dataset is downloaded and stored, so subsequent selections of
    any state within that year are served instantly from disk.
    """
    _init_db()
    if _is_cache_fresh("noaa", str(year)):
        cached = _read_noaa_from_db(year)
        if not cached.empty:
            return cached
    # Not cached or stale — download from NOAA.
    url = get_year_download_url(year)
    response = requests.get(url, timeout=120)
    response.raise_for_status()

    dataframe = pd.read_csv(
        BytesIO(response.content),
        compression="gzip",
        low_memory=False,
        usecols=NOAA_COLUMNS,
    )
    dataframe = dataframe.loc[dataframe["EVENT_TYPE"] == "Tornado"].copy()
    dataframe["BEGIN_DATE_TIME"] = pd.to_datetime(
        dataframe["BEGIN_DATE_TIME"],
        format="%d-%b-%y %H:%M:%S",
        errors="coerce",
    )
    dataframe["END_DATE_TIME"] = pd.to_datetime(
        dataframe["END_DATE_TIME"],
        format="%d-%b-%y %H:%M:%S",
        errors="coerce",
    )
    # %y parsing maps 00-68 → 2000-2068, 69-99 → 1969-1999.
    # Historical records dated in the future are misparse artifacts; subtract 100 years.
    _now = pd.Timestamp.now("UTC").tz_localize(None)
    for _col in ("BEGIN_DATE_TIME", "END_DATE_TIME"):
        _future = dataframe[_col] > _now
        dataframe.loc[_future, _col] -= pd.DateOffset(years=100)
    dataframe["MONTH_NUM"] = dataframe["MONTH_NAME"].map({name: number for number, name in MONTH_NAMES.items()})

    for column in ["BEGIN_LAT", "BEGIN_LON", "END_LAT", "END_LON", "TOR_LENGTH", "TOR_WIDTH"]:
        dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce")

    # Normalize STATE column: convert abbreviations to full names
    def convert_state_abbr(val: str) -> str:
        s = str(val).strip().upper()
        return STATE_ABBR_TO_NAME.get(s, s.title())
    
    dataframe["STATE"] = dataframe["STATE"].apply(convert_state_abbr)
    
    # Data quality rule: in northern states, extreme TOR_LENGTH values are
    # assumed to be yards and converted to miles.
    northern_yards_mask = dataframe["STATE"].isin(NORTHERN_US_STATES) & (dataframe["TOR_LENGTH"] > 99)
    dataframe.loc[northern_yards_mask, "TOR_LENGTH"] = dataframe.loc[northern_yards_mask, "TOR_LENGTH"] / 1760.0

    dataframe["END_LAT"] = dataframe["END_LAT"].fillna(dataframe["BEGIN_LAT"])
    dataframe["END_LON"] = dataframe["END_LON"].fillna(dataframe["BEGIN_LON"])
    dataframe = dataframe.dropna(subset=["BEGIN_LAT", "BEGIN_LON", "END_LAT", "END_LON"])
    area_names = dataframe.apply(
        lambda row: resolve_noaa_area_name(
            row["BEGIN_LAT"],
            row["BEGIN_LON"],
            row["BEGIN_LOCATION"],
            row["CZ_NAME"],
        ),
        axis=1,
    )

    normalized = pd.DataFrame(
        {
            "COUNTRY": "United States",
            "ADMIN_AREA": dataframe["STATE"],
            "AREA_NAME": area_names,
            "YEAR": dataframe["YEAR"],
            "MONTH_NUM": dataframe["MONTH_NUM"],
            "BEGIN_DATE_TIME": dataframe["BEGIN_DATE_TIME"],
            "END_DATE_TIME": dataframe["END_DATE_TIME"],
            "INTENSITY": dataframe["TOR_F_SCALE"].fillna("Unknown"),
            "TRACK_LENGTH": dataframe["TOR_LENGTH"],
            "TRACK_WIDTH": dataframe["TOR_WIDTH"],
            "BEGIN_LAT": dataframe["BEGIN_LAT"],
            "BEGIN_LON": dataframe["BEGIN_LON"],
            "END_LAT": dataframe["END_LAT"],
            "END_LON": dataframe["END_LON"],
            "EVENT_NARRATIVE": dataframe["EVENT_NARRATIVE"].fillna(dataframe["EPISODE_NARRATIVE"]),
            "SOURCE_DB": NOAA_SOURCE_NAME,
            "TRACK_LENGTH_YARDS_CORRECTED": northern_yards_mask,
        }
    )
    normalized["track"] = normalized.apply(
        lambda row: [[row["BEGIN_LON"], row["BEGIN_LAT"]], [row["END_LON"], row["END_LAT"]]],
        axis=1,
    )
    normalized["TEMP_HIGH_C"] = None
    normalized["TEMP_LOW_C"] = None
    result = normalized[NORMALIZED_COLUMNS]
    _write_noaa_to_db(year, result)
    _set_fetch_log("noaa", str(year))
    return result


def load_noaa_years(years: Iterable[int]) -> pd.DataFrame:
    frames = [load_noaa_tornado_data(year) for year in years]
    if not frames:
        return empty_normalized_frame()
    return pd.concat(frames, ignore_index=True)


@st.cache_data(show_spinner=False)
def load_noaa_years_cached(years_key: tuple[int, ...], data_revision: int) -> pd.DataFrame:
    """Memoize concatenated NOAA years to avoid rebuilds on non-filter reruns."""
    _ = data_revision  # explicit key input to invalidate when refresh is requested
    return load_noaa_years(years_key)


@st.cache_data(show_spinner=False)
def load_canadian_tornado_data_cached(data_revision: int) -> pd.DataFrame:
    """Memoize Canadian dataset load while preserving manual refresh invalidation."""
    _ = data_revision  # explicit key input to invalidate when refresh is requested
    return load_canadian_tornado_data()


def fetch_remote_csv(url: str) -> pd.DataFrame:
    response = requests.get(url, timeout=120)
    response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").lower()
    leading_text = response.text[:256].lower() if "text" in content_type or response.text.startswith("<") else ""
    if "html" in content_type or "<!doctype html" in leading_text or "<html" in leading_text:
        raise ValueError("source returned an HTML catalogue page instead of raw CSV")

    try:
        return pd.read_csv(BytesIO(response.content), low_memory=False, encoding="utf-8")
    except UnicodeDecodeError:
        return pd.read_csv(BytesIO(response.content), low_memory=False, encoding="latin-1")


def load_canadian_tornado_data() -> pd.DataFrame:
    """Load Canadian tornado data, using a local SQLite cache with 7-day TTL."""
    _init_db()
    if _is_cache_fresh("canada", "all"):
        cached = _read_canada_from_db()
        if not cached.empty:
            month_source = cached["MONTH_NUM"] if "MONTH_NUM" in cached.columns else pd.Series(dtype="float64")
            month_values = pd.to_numeric(month_source, errors="coerce")
            cache_shape_ok = list(cached.columns) == NORMALIZED_COLUMNS
            cache_month_ok = month_values.dropna().between(1, 12).all()
            if "TRACK_LENGTH" in cached.columns:
                cached_lengths = pd.to_numeric(cached["TRACK_LENGTH"], errors="coerce")
                # Legacy bug path: Canadian LENGTH_M values were persisted as km-scale lengths.
                # If magnitudes look meter-like, convert to km on read and persist corrected cache.
                if cached_lengths.notna().any() and float(cached_lengths.max()) > 500.0:
                    cached["TRACK_LENGTH"] = cached_lengths / 1000.0
                    _write_canada_to_db(cached[NORMALIZED_COLUMNS])
            if cache_shape_ok and cache_month_ok:
                return cached
    tracks = fetch_remote_csv(CANADA_TRACKS_CSV_URL)
    events = fetch_remote_csv(CANADA_EVENTS_CSV_URL)

    province_series = series_or_default(
        tracks,
        ["province", "prov_terr", "prov", "prname", "admin_area"],
    ).fillna(series_or_default(events, ["province", "prov_terr", "prov", "prname", "admin_area"]))
    province_series = province_series.apply(normalize_canada_admin_area)

    year_series = pd.to_numeric(
        series_or_default(tracks, ["year", "yr", "yyyy_local", "yyyy_solar"], pd.NA).fillna(
            series_or_default(events, ["year", "yr", "yyyy_local", "yyyy_solar"], pd.NA)
        ),
        errors="coerce",
    )
    month_series = pd.to_numeric(
        series_or_default(tracks, ["month", "mo", "mm_local", "mm_solar"], pd.NA).fillna(
            series_or_default(events, ["month", "mo", "mm_local", "mm_solar"], pd.NA)
        ),
        errors="coerce",
    )
    day_series = pd.to_numeric(
        series_or_default(tracks, ["day", "dy", "dd_local", "dd_solar"], pd.NA).fillna(
            series_or_default(events, ["day", "dy", "dd_local", "dd_solar"], pd.NA)
        ),
        errors="coerce",
    )
    date_series = pd.to_datetime(
        series_or_default(tracks, ["date", "event_date", "tornado_date", "yyyymmddhh"], pd.NA).fillna(
            series_or_default(events, ["date", "event_date", "tornado_date", "yyyymmddhh"], pd.NA)
        ),
        errors="coerce",
    )

    if date_series.isna().all():
        yyyymmddhh = series_or_default(tracks, ["yyyymmddhh"], pd.NA).fillna(
            series_or_default(events, ["yyyymmddhh"], pd.NA)
        )
        yyyymmddhh_str = (
            yyyymmddhh.astype("string")
            .str.replace(r"\.0$", "", regex=True)
            .str.strip()
            .str.zfill(10)
        )
        date_series = pd.to_datetime(yyyymmddhh_str, format="%Y%m%d%H", errors="coerce")

    if date_series.isna().all():
        date_series = pd.to_datetime(
            pd.DataFrame({"year": year_series, "month": month_series, "day": day_series.fillna(1)}),
            errors="coerce",
        )

    start_lat = pd.to_numeric(
        series_or_default(tracks, ["start_lat", "begin_lat", "lat_start", "start_y", "start_lat_"]),
        errors="coerce",
    )
    start_lon = pd.to_numeric(
        series_or_default(tracks, ["start_lon", "begin_lon", "lon_start", "start_x", "start_lon_"]),
        errors="coerce",
    )
    end_lat = pd.to_numeric(
        series_or_default(tracks, ["end_lat", "lat_end", "end_y", "end_lat__1", "end_lat_n"]),
        errors="coerce",
    )
    end_lon = pd.to_numeric(
        series_or_default(tracks, ["end_lon", "lon_end", "end_x", "end_lon_w"]),
        errors="coerce",
    )

    geometry_column = pick_column(tracks, ["wkt", "geometry", "geom"])
    if geometry_column and start_lat.isna().all() and start_lon.isna().all():
        endpoints = tracks[geometry_column].apply(parse_wkt_endpoints)
        endpoint_frame = pd.DataFrame(
            endpoints.tolist(),
            columns=["BEGIN_LAT", "BEGIN_LON", "END_LAT", "END_LON"],
            index=tracks.index,
        )
        start_lat = endpoint_frame["BEGIN_LAT"]
        start_lon = endpoint_frame["BEGIN_LON"]
        end_lat = endpoint_frame["END_LAT"]
        end_lon = endpoint_frame["END_LON"]

    if start_lat.isna().all() or start_lon.isna().all():
        event_lat = pd.to_numeric(series_or_default(events, ["latitude", "lat", "y", "begin_lat"]), errors="coerce")
        event_lon = pd.to_numeric(series_or_default(events, ["longitude", "lon", "x", "begin_lon"]), errors="coerce")
        start_lat = event_lat
        start_lon = event_lon
        end_lat = event_lat
        end_lon = event_lon

    length_column = pick_column(tracks, ["length", "track_length", "length_km", "path_length", "length_m"])
    track_length_series = pd.to_numeric(
        series_or_default(tracks, ["length", "track_length", "length_km", "path_length", "length_m"]),
        errors="coerce",
    )
    if length_column and length_column.lower() == "length_m":
        # Canadian source LENGTH_M is meters; normalize to kilometers for table/unit consistency.
        track_length_series = track_length_series / 1000.0

    normalized = pd.DataFrame(
        {
            "COUNTRY": "Canada",
            "ADMIN_AREA": province_series,
            "AREA_NAME": series_or_default(
                tracks,
                ["location", "municipality", "place", "area_name", "near_cmmty"],
            ).fillna(
                series_or_default(events, ["location", "municipality", "place", "area_name", "near_cmmty"])
            ),
            "YEAR": year_series.fillna(date_series.dt.year),
            "MONTH_NUM": month_series.fillna(date_series.dt.month),
            "BEGIN_DATE_TIME": date_series,
            "END_DATE_TIME": date_series,
            "INTENSITY": series_or_default(tracks, ["f_scale", "fscale", "fujita", "scale"], "Unknown").fillna(
                series_or_default(events, ["f_scale", "fscale", "fujita", "scale"], "Unknown")
            ),
            "TRACK_LENGTH": track_length_series,
            "TRACK_WIDTH": pd.to_numeric(
                series_or_default(tracks, ["width", "track_width", "width_m", "path_width", "width_max_"]),
                errors="coerce",
            ),
            "BEGIN_LAT": start_lat,
            "BEGIN_LON": start_lon,
            "END_LAT": end_lat.fillna(start_lat),
            "END_LON": end_lon.fillna(start_lon),
            "EVENT_NARRATIVE": series_or_default(tracks, ["comments", "narrative", "description", "notes"]).fillna(
                series_or_default(events, ["comments", "narrative", "description", "notes"])
            ),
            "SOURCE_DB": CANADA_SOURCE_NAME,
            "TRACK_LENGTH_YARDS_CORRECTED": False,
        }
    )
    normalized["YEAR"] = pd.to_numeric(normalized["YEAR"], errors="coerce")
    normalized["MONTH_NUM"] = pd.to_numeric(normalized["MONTH_NUM"], errors="coerce")
    normalized["INTENSITY"] = (
        normalized["INTENSITY"]
        .astype("string")
        .str.strip()
        .str.replace(r"^([0-5])(?:\.0+)?$", r"F\1", regex=True)
    )
    normalized = normalized.loc[normalized["MONTH_NUM"].between(1, 12, inclusive="both")]
    normalized = normalized.dropna(subset=["BEGIN_LAT", "BEGIN_LON", "END_LAT", "END_LON", "YEAR", "MONTH_NUM"])
    normalized["track"] = normalized.apply(
        lambda row: [[row["BEGIN_LON"], row["BEGIN_LAT"]], [row["END_LON"], row["END_LAT"]]],
        axis=1,
    )
    normalized["TEMP_HIGH_C"] = None
    normalized["TEMP_LOW_C"] = None
    result = normalized[NORMALIZED_COLUMNS]
    _write_canada_to_db(result)
    _set_fetch_log("canada", "all")
    return result


def build_grouped_location_options(
    country_admin_areas: dict[str, list[str]],
    country_regions: dict[str, dict[str, list[str]]],
) -> tuple[list[str], dict[str, list[str]]]:
    options: list[str] = []
    token_to_areas: dict[str, list[str]] = {}
    for country in country_admin_areas:
        header_token = f"__HEADER__::{country}"
        options.append(header_token)

        for region_name in sorted(country_regions.get(country, {})):
            region_token = f"__REGION__::{country}::{region_name}"
            options.append(region_token)
            token_to_areas[region_token] = sorted(set(country_regions[country][region_name]))

        for area in sorted(set(country_admin_areas[country])):
            token = f"__AREA__::{country}::{area}"
            options.append(token)
            token_to_areas[token] = [area]
    return options, token_to_areas


def format_location_option(option_token: str) -> str:
    if option_token.startswith("__HEADER__::"):
        country = option_token.split("::", 1)[1]
        return f"--- {country} ---"
    if option_token.startswith("__REGION__::"):
        return f"• {option_token.split('::', 2)[2]}"
    if option_token.startswith("__AREA__::"):
        return option_token.split("::", 2)[2]
    return option_token


def resolve_selected_admin_areas(selected_location_tokens: list[str], token_to_areas: dict[str, list[str]]) -> list[str]:
    resolved: set[str] = set()
    for token in selected_location_tokens:
        resolved.update(token_to_areas.get(token, []))
    return sorted(resolved)



def normalise_intensity(val: object) -> str:
    """Normalise an F-scale or EF-scale string to EF0-EF5 or 'Unk'."""
    s = str(val).strip().upper()
    if s in ("EF0", "EF1", "EF2", "EF3", "EF4", "EF5"):
        return s
    # Canadian files can encode Fujita scale as plain digits (0-5).
    if re.fullmatch(r"[0-5](?:\.0+)?", s):
        return f"EF{int(float(s))}"
    if s in ("F0",):
        return "EF0"
    if s in ("F1",):
        return "EF1"
    if s in ("F2",):
        return "EF2"
    if s in ("F3",):
        return "EF3"
    if s in ("F4",):
        return "EF4"
    if s in ("F5",):
        return "EF5"
    return "Unk"


def season_color(month: object) -> list[int]:
    """Return an RGBA colour list for a given month number (season-coded)."""
    try:
        m = int(month)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [150, 150, 150, 180]
    if m in (3, 4, 5):
        return [34, 197, 94, 200]   # spring – green
    if m in (6, 7, 8):
        return [249, 115, 22, 200]  # summer – orange
    if m in (9, 10, 11):
        return [168, 85, 247, 200]  # fall – purple
    return [59, 130, 246, 200]      # winter – blue


def format_event_date(value: object) -> str:
    if value is None or value is pd.NA:
        return "Unknown"

    timestamp = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(timestamp):
        return str(value)
    if timestamp.hour == 0 and timestamp.minute == 0 and timestamp.second == 0:
        return timestamp.strftime("%Y-%m-%d")
    return timestamp.strftime("%Y-%m-%d %H:%M")


def format_measurement(value: object, unit: str) -> str:
    if value is None or value is pd.NA:
        return f"Unknown {unit}"

    number = pd.to_numeric(str(value), errors="coerce")
    if pd.isna(number):
        return f"{value} {unit}"
    return f"{number:g} {unit}"


def sync_seasons_from_months() -> None:
    active_months = set(st.session_state.get("selected_months", []))
    for season, months in SEASON_MONTHS.items():
        st.session_state[f"season_{season}"] = any(month in active_months for month in months)


def sync_months_from_seasons() -> None:
    selected_months = sorted(
        {
            month
            for season, months in SEASON_MONTHS.items()
            if st.session_state.get(f"season_{season}", False)
            for month in months
        }
    )
    st.session_state["selected_months"] = selected_months
    for month in MONTH_NAMES:
        key = f"month_{month}"
        if key in st.session_state:
            st.session_state[key] = month in selected_months


def sync_months_from_month_checks() -> None:
    selected_months = sorted(
        month for month in MONTH_NAMES if st.session_state.get(f"month_{month}", False)
    )
    st.session_state["selected_months"] = selected_months
    sync_seasons_from_months()


def get_map_selected_rows(selection_container: object) -> set[int]:
    selected_rows: set[int] = set()

    if not isinstance(selection_container, dict):
        return selected_rows

    last_clicked = selection_container.get("last_object_clicked")
    if not isinstance(last_clicked, dict):
        return selected_rows

    geometry = last_clicked.get("geometry", {})
    coordinates = geometry.get("coordinates", None)
    if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
        # GeoJSON Feature format: coordinates are [lon, lat]
        lon, lat = float(coordinates[0]), float(coordinates[1])
    else:
        # Direct latlng format from folium.Marker / DivIcon: {"lat": lat, "lng": lng}
        lat_val = last_clicked.get("lat")
        lng_val = last_clicked.get("lng")
        if lat_val is not None and lng_val is not None:
            lat, lon = float(lat_val), float(lng_val)
        else:
            return selected_rows

    coord_tuple = (round(lat, 3), round(lon, 3))
    coord_to_idx = st.session_state.get("_map_coord_to_idx", {})
    if coord_tuple in coord_to_idx:
        selected_rows.add(coord_to_idx[coord_tuple])
    else:
        # Try proximity matching if exact match not found
        for stored_coord, idx in coord_to_idx.items():
            if abs(stored_coord[0] - lat) < 0.01 and abs(stored_coord[1] - lon) < 0.01:
                selected_rows.add(idx)
                break
    return selected_rows


def get_table_selected_rows(selection_container: object, displayed_row_ids: Sequence[int]) -> set[int]:
    selected_rows: set[int] = set()

    if isinstance(selection_container, dict):
        selection_state = selection_container.get("selection", {})
        raw_rows = selection_state.get("rows", [])
    else:
        selection_state = getattr(selection_container, "selection", None)
        raw_rows = getattr(selection_state, "rows", None)

    if raw_rows is None:
        return selected_rows

    for row_position in raw_rows:
        row_index = int(row_position)
        if 0 <= row_index < len(displayed_row_ids):
            selected_rows.add(displayed_row_ids[row_index])
    return selected_rows


def find_map_bounds(value: object) -> tuple[float, float, float, float] | None:
    """Best-effort extraction of (south, west, north, east) from map event/state."""
    if isinstance(value, dict):
        bounds_value = value.get("bounds")
        if isinstance(bounds_value, list) and len(bounds_value) == 2:
            try:
                south = float(bounds_value[0][0])
                west = float(bounds_value[0][1])
                north = float(bounds_value[1][0])
                east = float(bounds_value[1][1])
                return south, west, north, east
            except (TypeError, ValueError, IndexError):
                pass

        southwest = value.get("_southWest")
        northeast = value.get("_northEast")
        if isinstance(southwest, dict) and isinstance(northeast, dict):
            south_v = southwest.get("lat")
            west_v = southwest.get("lng")
            north_v = northeast.get("lat")
            east_v = northeast.get("lng")
            if None in (south_v, west_v, north_v, east_v):
                pass
            else:
                try:
                    south = float(str(south_v))
                    west = float(str(west_v))
                    north = float(str(north_v))
                    east = float(str(east_v))
                    return south, west, north, east
                except (TypeError, ValueError):
                    pass

        # Common naming variants for viewport bounds.
        keysets = [
            ("south", "west", "north", "east"),
            ("minLat", "minLon", "maxLat", "maxLon"),
            ("min_lat", "min_lon", "max_lat", "max_lon"),
        ]
        for south_key, west_key, north_key, east_key in keysets:
            if all(k in value for k in (south_key, west_key, north_key, east_key)):
                try:
                    south = float(value[south_key])
                    west = float(value[west_key])
                    north = float(value[north_key])
                    east = float(value[east_key])
                    return south, west, north, east
                except (TypeError, ValueError):
                    pass

        # Some payloads provide center+zoom instead of explicit bounds.
        lat = value.get("latitude", value.get("lat"))
        lon = value.get("longitude", value.get("lon", value.get("lng")))
        zoom = value.get("zoom")
        if lat is not None and lon is not None and zoom is not None:
            try:
                center_lat = float(lat)
                center_lon = float(lon)
                zoom_level = float(zoom)
                world_px = 256.0 * (2.0 ** zoom_level)
                # Use a conservative viewport estimate based on configured map height.
                viewport_w = 1100.0
                viewport_h = float(DEFAULT_MAP_HEIGHT)
                lon_half = (viewport_w / 2.0) * (360.0 / world_px)

                lat_rad = math.radians(center_lat)
                merc_y = math.log(math.tan(math.pi / 4.0 + lat_rad / 2.0))
                merc_delta = (viewport_h / 2.0) * (2.0 * math.pi / world_px)
                north_lat = math.degrees(2.0 * math.atan(math.exp(merc_y + merc_delta)) - math.pi / 2.0)
                south_lat = math.degrees(2.0 * math.atan(math.exp(merc_y - merc_delta)) - math.pi / 2.0)
                return south_lat, center_lon - lon_half, north_lat, center_lon + lon_half
            except (TypeError, ValueError, OverflowError):
                pass

        for nested in value.values():
            bounds = find_map_bounds(nested)
            if bounds is not None:
                return bounds

    return None


def find_map_view_state(
    value: object,
    allow_bounds_fallback: bool = True,
) -> tuple[float, float, float] | None:
    """Best-effort extraction of (latitude, longitude, zoom) from map event/state."""
    import math
    
    if isinstance(value, dict):
        center = value.get("center")
        zoom = value.get("zoom")
        if isinstance(center, dict) and zoom is not None:
            lat = center.get("lat", center.get("latitude"))
            lon = center.get("lng", center.get("lon", center.get("longitude")))
            if lat is not None and lon is not None:
                try:
                    return float(lat), float(lon), float(zoom)
                except (TypeError, ValueError):
                    pass

        lat = value.get("latitude", value.get("lat"))
        lon = value.get("longitude", value.get("lon", value.get("lng")))
        zoom = value.get("zoom")
        if lat is not None and lon is not None and zoom is not None:
            try:
                return float(lat), float(lon), float(zoom)
            except (TypeError, ValueError):
                pass

        # Optionally estimate view from bounds if center+zoom are not present.
        if allow_bounds_fallback:
            bounds = find_map_bounds(value)
            if bounds is not None:
                south, west, north, east = bounds
                center_lat = (south + north) / 2.0
                center_lon = (west + east) / 2.0
                # Estimate zoom from bounds extent using standard web mercator formula
                # zoom = ceil(log2(360 / lat_extent))
                lat_extent = max(0.01, abs(north - south))  # Avoid division by very small numbers
                try:
                    zoom_estimate = math.ceil(math.log2(360.0 / lat_extent))
                    zoom_estimate = max(0, min(20, zoom_estimate))
                    return center_lat, center_lon, float(zoom_estimate)
                except (ValueError, ZeroDivisionError):
                    # If zoom calculation fails, return center without zoom - will auto-fit
                    return center_lat, center_lon, 4.5

        for nested in value.values():
            found = find_map_view_state(nested, allow_bounds_fallback=allow_bounds_fallback)
            if found is not None:
                return found

    return None


def pick_best_map_state(*candidates: object) -> object:
    """Pick the richest map payload, preferring one with bounds or view state."""
    for candidate in candidates:
        if candidate is None:
            continue
        if find_map_bounds(candidate) is not None:
            return candidate
        if find_map_view_state(candidate) is not None:
            return candidate
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return {}


def visible_in_bounds_mask(dataframe: pd.DataFrame, bounds: tuple[float, float, float, float]) -> pd.Series:
    south, west, north, east = bounds
    lat_visible = (
        dataframe["BEGIN_LAT"].between(south, north)
        | dataframe["END_LAT"].between(south, north)
    )
    if west <= east:
        lon_visible = (
            dataframe["BEGIN_LON"].between(west, east)
            | dataframe["END_LON"].between(west, east)
        )
    else:
        # Handle anti-meridian crossing windows.
        lon_visible = (
            dataframe["BEGIN_LON"].ge(west)
            | dataframe["BEGIN_LON"].le(east)
            | dataframe["END_LON"].ge(west)
            | dataframe["END_LON"].le(east)
        )
    return lat_visible & lon_visible


def add_density_overlay(
    fmap: Any,
    dataframe: pd.DataFrame,
    group_by: str,
    min_points: int = 3,
) -> None:
    """
    Add heatmap-based density overlays to the folium map, grouped by a field.
    Each group gets a semi-transparent heatmap layer.
    
    Args:
        fmap: folium.Map object
        dataframe: DataFrame with BEGIN_LAT, BEGIN_LON coordinates
        group_by: Column name to group by (SEASON, INTENSITY_NORM, COUNTRY, etc.)
        min_points: Minimum points required per group to generate heatmap
    """
    if folium is None:
        return
    
    try:
        _heat_map = __import__("folium.plugins", fromlist=["HeatMap"])
        HeatMap = getattr(_heat_map, "HeatMap", None)
        if HeatMap is None:
            return
    except (ImportError, AttributeError):
        return
    
    if group_by not in dataframe.columns:
        return
    
    groups = dataframe.groupby(group_by, observed=True)
    
    # Define colors for different groups (hex)
    group_colors = {
        "Spring": "#22c55e",     # Green
        "Summer": "#f97316",     # Orange
        "Fall": "#a855f7",       # Purple
        "Winter": "#3b82f6",     # Blue
    }

    def _hex_to_rgba(hex_str: str, alpha: float) -> str:
        """Convert a #rrggbb hex string to an rgba() CSS string."""
        h = hex_str.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"

    for group_name, group_df in groups:
        # Extract valid coordinates
        coords = group_df[["BEGIN_LAT", "BEGIN_LON"]].dropna()
        
        if len(coords) < min_points:
            continue
        
        lats = coords["BEGIN_LAT"].values
        lons = coords["BEGIN_LON"].values
        
        try:
            # Create heat data from points
            heat_data = [[float(lat), float(lon)] for lat, lon in zip(lats, lons)]
            
            if len(heat_data) > 1:
                group_name_str = str(group_name)
                layer_name = f"Density: {group_name_str} ({len(coords)} events)"
                color = group_colors.get(group_name_str, "#ef4444")
                # Use the group's own hue at alpha=0 for transparent stops so
                # Leaflet interpolates within the same hue family.
                transparent = _hex_to_rgba(color, 0)
                # max_val anchors the normalisation to a cluster-scale density.
                # An isolated single point contributes intensity ~1; with
                # max_val set to sqrt(n) it normalises to 1/sqrt(n) which is
                # well below the 0.6 gradient threshold, eliminating halos.
                max_val = max(5, int(len(heat_data) ** 0.45))
                
                HeatMap(
                    heat_data,
                    name=layer_name,
                    min_opacity=0.0,
                    max_val=max_val,
                    radius=35,
                    blur=25,
                    gradient={
                        0.0: transparent,
                        0.59: transparent,
                        0.6: color,
                        1.0: color,
                    },
                ).add_to(fmap)
        except Exception:
            # Silently skip groups that fail
            pass


def _zoom_for_bounds(latitudes: list[float], longitudes: list[float]) -> float:
    """Estimate a comfortable Leaflet zoom level to fit the data bounding box.

    Assumes roughly 1100 px of map width and adds 20 % padding on each axis.
    Returns a float; build_map rounds it to int via int(round(...)).
    """
    if len(latitudes) < 2 or len(longitudes) < 2:
        return 4.5
    lat_span = max(latitudes) - min(latitudes)
    lon_span = max(longitudes) - min(longitudes)
    # Apply padding and account for Mercator foreshortening at higher latitudes
    fit_span = max(lon_span * 1.2, lat_span * 1.8, 0.5)
    # 360 * map_px / (256 * span_degrees) gives the zoom that fills map_px width
    zoom = math.log2(360.0 * 1100.0 / (256.0 * fit_span))
    return max(2.5, min(8.0, zoom))


def build_map(
    dataframe: pd.DataFrame,
    selected_rows: set[int] | None = None,
    show_hover_popup: bool = True,
    map_state: object | None = None,
    persisted_view: tuple[float, float, float] | None = None,
    map_height: int = DEFAULT_MAP_HEIGHT,
    max_map_rows: int = MAX_MAP_ROWS,
    show_density_overlay: bool = False,
    density_group_by: str | None = None,
) -> tuple[Any, float, float, float]:
    if folium is None:
        raise RuntimeError("folium is not installed")

    map_df = dataframe.copy()
    selected_rows = selected_rows or set()
    map_df["TOOLTIP_AREA_NAME"] = map_df["AREA_NAME"].fillna("Unknown area")
    map_df["TOOLTIP_EVENT_DATE"] = map_df["BEGIN_DATE_TIME"].apply(format_event_date)
    map_df["TOOLTIP_LENGTH"] = map_df.apply(
        lambda row: format_measurement(
            row["TRACK_LENGTH"],
            "km" if row["COUNTRY"] == "Canada" else "mi",
        ),
        axis=1,
    )
    map_df["TOOLTIP_WIDTH"] = map_df.apply(
        lambda row: format_measurement(
            row["TRACK_WIDTH"],
            "m" if row["COUNTRY"] == "Canada" else "yd",
        ),
        axis=1,
    )
    map_df["TOOLTIP_TEMP_HIGH"] = map_df.get("TEMP_HIGH", pd.Series("—", index=map_df.index)).fillna("—")
    map_df["TOOLTIP_TEMP_LOW"] = map_df.get("TEMP_LOW", pd.Series("—", index=map_df.index)).fillna("—")
    map_df["IS_SELECTED"] = map_df["_row_idx"].isin(selected_rows)

    # Cap the number of rendered elements to prevent browser GPU overload / BSOD on zoom.
    # Always keep all selected rows; sample from the remainder to reach the limit.
    if len(map_df) > max_map_rows:
        selected_df = map_df[map_df["IS_SELECTED"]]
        unselected_df = map_df[~map_df["IS_SELECTED"]]
        remaining_slots = max(0, max_map_rows - len(selected_df))
        if remaining_slots > 0 and len(unselected_df) > remaining_slots:
            unselected_df = unselected_df.sample(n=remaining_slots, random_state=42)
        elif remaining_slots == 0:
            unselected_df = unselected_df.iloc[:0]
        map_df = pd.concat([selected_df, unselected_df], ignore_index=True)
        st.warning(
            f"Map display capped at {max_map_rows:,} events to prevent browser GPU overload. "
            f"Use the date/state filters to narrow your selection, or increase the Map row limit in Advanced settings."
        )

    map_df["DISPLAY_LINE_COLOR"] = map_df["IS_SELECTED"].apply(
        lambda is_selected: [250, 204, 21, 245] if is_selected else [204, 36, 29, 180]
    )
    map_df["DISPLAY_POINT_COLOR"] = map_df.apply(
        lambda row: [250, 204, 21, 245] if row["IS_SELECTED"] else row["SEASON_COLOR"],
        axis=1,
    )
    map_df["DISPLAY_LINE_WIDTH"] = map_df["IS_SELECTED"].apply(lambda is_selected: 9000 if is_selected else 4000)
    map_df["DISPLAY_POINT_RADIUS"] = map_df["IS_SELECTED"].apply(lambda is_selected: 2600 if is_selected else 1500)

    # Build coordinate-to-row-index mapping for click event lookup
    st.session_state["_map_coord_to_idx"] = {
        (round(float(row["BEGIN_LAT"]), 3), round(float(row["BEGIN_LON"]), 3)): int(row["_row_idx"])
        for _, row in map_df.iterrows()
        if pd.notna(row["BEGIN_LAT"]) and pd.notna(row["BEGIN_LON"])
    }

    latitudes = [float(value) for value in pd.concat([map_df["BEGIN_LAT"], map_df["END_LAT"]], ignore_index=True).dropna().tolist()]
    longitudes = [float(value) for value in pd.concat([map_df["BEGIN_LON"], map_df["END_LON"]], ignore_index=True).dropna().tolist()]

    if persisted_view is not None:
        view_lat, view_lon, view_zoom = persisted_view
    elif map_state is not None:
        inferred = find_map_view_state(map_state, allow_bounds_fallback=False)
        if inferred is not None:
            view_lat, view_lon, view_zoom = inferred
        else:
            view_lat = sum(latitudes) / len(latitudes) if latitudes else 38.8
            view_lon = sum(longitudes) / len(longitudes) if longitudes else -98.6
            view_zoom = _zoom_for_bounds(latitudes, longitudes)
    else:
        view_lat = sum(latitudes) / len(latitudes) if latitudes else 38.8
        view_lon = sum(longitudes) / len(longitudes) if longitudes else -98.6
        view_zoom = _zoom_for_bounds(latitudes, longitudes)

    # Build the map with the computed view baked in.
    # Esri World Shaded Relief: greyscale hillshade, no subdomain, publicly available.
    fmap = folium.Map(
        location=[view_lat, view_lon],
        zoom_start=int(round(view_zoom)),
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Source: Esri, USGS, NOAA",
        control_scale=True,
    )
    if MeasureControl is not None:
        MeasureControl(
            position="bottomleft",
            primary_length_unit="kilometers",
            secondary_length_unit="miles",
            primary_area_unit="sqmeters",
            secondary_area_unit="acres",
        ).add_to(fmap)
    # Transparent overlay: roads + city labels on top of the shaded relief.
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Reference_Overlay/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri",
        name="roads_labels",
        overlay=True,
        control=False,
        opacity=0.6,
    ).add_to(fmap)

    # Add density overlay if requested
    if show_density_overlay and density_group_by:
        add_density_overlay(fmap, map_df, density_group_by)

    # Fixed dot diameter — DivIcon is HTML so the browser scales it with the page;
    # server-side zoom is not needed and caused map resets.
    default_point_radius = 8.0
    selected_point_radius = 10.0

    for _, row in map_df.iterrows():
        selected = bool(row["IS_SELECTED"])
        line_color = "#facc15" if selected else "#cc241d"
        line_weight = 5 if selected else 3
        point_radius = selected_point_radius if selected else default_point_radius

        popup_html = (
            f"<b>{row['TOOLTIP_AREA_NAME']}, {row['ADMIN_AREA']}, {row['COUNTRY']}</b><br/>"
            f"Event date: {row['TOOLTIP_EVENT_DATE']}<br/>"
            f"Intensity: {row['INTENSITY']}<br/>"
            f"Length: {row['TOOLTIP_LENGTH']}<br/>"
            f"Width: {row['TOOLTIP_WIDTH']}<br/>"
            f"Temp High: {row['TOOLTIP_TEMP_HIGH']}<br/>"
            f"Temp Low: {row['TOOLTIP_TEMP_LOW']}<br/>"
            f"Source: {row['SOURCE_DB']}"
        )

        folium.PolyLine(
            locations=[[float(row["BEGIN_LAT"]), float(row["BEGIN_LON"])], [float(row["END_LAT"]), float(row["END_LON"])]],
            color=line_color,
            weight=line_weight,
            opacity=0.9,
        ).add_to(fmap)

        # Derive short intensity label: EF0→"0", EF1→"1", …, EF5→"5", anything else→"U"
        intensity_norm = str(row.get("INTENSITY_NORM", "Unk"))
        if intensity_norm.startswith("EF") and len(intensity_norm) > 2 and intensity_norm[2:].isdigit():
            dot_label = intensity_norm[2:]
        else:
            dot_label = "U"

        dot_diam = max(14, int(point_radius * 2))
        font_size = max(7, dot_diam - 5)
        bg_hex = "#facc15" if selected else f"#{int(row['SEASON_COLOR'][0]):02x}{int(row['SEASON_COLOR'][1]):02x}{int(row['SEASON_COLOR'][2]):02x}"
        border_hex = "#c69400" if selected else "#cc241d"
        icon_html = (
            f'<div style="'
            f"width:{dot_diam}px;height:{dot_diam}px;"
            f"border-radius:50%;background:{bg_hex};"
            f"border:1.5px solid {border_hex};"
            f"display:flex;align-items:center;justify-content:center;"
            f"font-family:Arial,sans-serif;font-size:{font_size}px;font-weight:bold;"
            f'color:#1a1a1a;opacity:0.95;box-sizing:border-box;user-select:none;">'
            f"{dot_label}</div>"
        )
        marker = folium.Marker(
            location=[float(row["BEGIN_LAT"]), float(row["BEGIN_LON"])],
            icon=folium.DivIcon(
                html=icon_html,
                icon_size=(dot_diam, dot_diam),
                icon_anchor=(dot_diam // 2, dot_diam // 2),
            ),
        )
        if show_hover_popup:
            marker.add_child(folium.Popup(popup_html, max_width=360))
        marker.add_to(fmap)

    fig = folium.Figure(width="100%", height=map_height)
    fig.add_child(fmap)
    return fig, view_lat, view_lon, view_zoom



def export_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    export_frame = dataframe[
        [
            "BEGIN_DATE_TIME",
            "END_DATE_TIME",
            "COUNTRY",
            "ADMIN_AREA",
            "AREA_NAME",
            "INTENSITY",
            "TRACK_LENGTH",
            "TRACK_WIDTH",
            "BEGIN_LAT",
            "BEGIN_LON",
            "END_LAT",
            "END_LON",
            "SOURCE_DB",
            "EVENT_NARRATIVE",
        ]
    ].copy()
    export_frame.columns = [
        "begin_time",
        "end_time",
        "country",
        "state_or_province",
        "local_area",
        "intensity",
        "track_length",
        "track_width",
        "begin_lat",
        "begin_lon",
        "end_lat",
        "end_lon",
        "source_database",
        "event_narrative",
    ]
    return export_frame



def to_csv_bytes(dataframe: pd.DataFrame) -> bytes:
    return export_columns(dataframe).to_csv(index=False).encode("utf-8")



def to_excel_bytes(dataframe: pd.DataFrame) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export_columns(dataframe).to_excel(writer, index=False, sheet_name="tornadoes")
    return buffer.getvalue()



def main() -> None:
    # Run once per server session: remove stale fetch_log entries so
    # any data older than CACHE_TTL_DAYS is re-downloaded this run.
    _run_startup_maintenance()

    st.set_page_config(page_title="Tornado Map", layout="wide")
    st.markdown(
        """
        <style>
        /* Hide Streamlit chrome bar without collapsing app content into it. */
        header[data-testid="stHeader"] {
            display: none !important;
        }
        /* Keep the app tight, but preserve enough top room for the title row. */
        section[data-testid="stMain"] .block-container,
        [data-testid="block-container"] {
            max-width: 100% !important;
            padding-top: 0.85rem !important;
            padding-right: 0.75rem !important;
            padding-bottom: 0.2rem !important;
            padding-left: 0.75rem !important;
        }
        /* Tighten vertical gaps between elements */
        [data-testid="stVerticalBlock"] {
            gap: 0.25rem !important;
        }
        .app-titlebar {
            display: flex;
            align-items: baseline;
            flex-wrap: nowrap;
            white-space: nowrap;
            gap: 0.6rem;
            margin: 0 0 calc(0.5rem + 5px) 0;
            line-height: 1.2;
        }
        .app-title {
            font-size: 1.35rem;
            font-weight: 700;
            margin: 0;
        }
        .app-subtitle {
            font-size: 0.78rem;
            color: #6b7280;
            margin: 0;
        }
        [data-testid="stExpander"] {
            margin-top: 0.35rem !important;
            margin-bottom: 0 !important;
        }
        /* Keep sidebar usable while preserving Streamlit's drag-resize handle. */
        [data-testid="stSidebar"] {
            min-width: 280px !important;
        }
        [data-testid="stSidebar"] [data-testid="block-container"] {
            padding-right: 1rem !important;
            padding-left: 1rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        "<div class='app-titlebar'>"
        "<span class='app-title'>Tornado Map</span>"
        "<span class='app-subtitle'>NOAA Storm Events &middot; Canadian National Tornado Database (best-effort)</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    current_year = pd.Timestamp.now("UTC").year
    year_start, year_end = default_year_range()

    _density_group_options = {
        "Season": "SEASON",
        "Intensity": "INTENSITY_NORM",
        "Country": "COUNTRY",
        "State/Province": "ADMIN_AREA",
    }
    location_options, location_token_to_area = build_grouped_location_options(
        COUNTRY_ADMIN_AREAS,
        COUNTRY_REGIONS,
    )
    default_locations = [
        token for token in ["__REGION__::United States::Great Lakes"] if token in location_options
    ]

    # Applied scope filters drive data loading. Draft controls live in the form
    # and only become active when the user clicks Apply.
    if "_scope_year_range" not in st.session_state:
        st.session_state["_scope_year_range"] = (year_start, year_end)
    if "_scope_location_tokens" not in st.session_state:
        st.session_state["_scope_location_tokens"] = default_locations
    if "_scope_show_density" not in st.session_state:
        st.session_state["_scope_show_density"] = False
    if "_scope_density_group_option" not in st.session_state:
        st.session_state["_scope_density_group_option"] = "Season"

    if "_draft_scope_year_range" not in st.session_state:
        st.session_state["_draft_scope_year_range"] = st.session_state["_scope_year_range"]
    if "_draft_scope_location_tokens" not in st.session_state:
        st.session_state["_draft_scope_location_tokens"] = st.session_state["_scope_location_tokens"]
    if "_draft_scope_show_density" not in st.session_state:
        st.session_state["_draft_scope_show_density"] = st.session_state["_scope_show_density"]
    if "_draft_scope_density_group_option" not in st.session_state:
        st.session_state["_draft_scope_density_group_option"] = st.session_state["_scope_density_group_option"]

    with st.sidebar:
        st.header("Filters")
        with st.form("scope_filters_form", border=False):
            st.toggle(
                "Show density overlay",
                key="_draft_scope_show_density",
                help="Display semi-transparent heatmaps showing where tornado events cluster.",
            )
            if st.session_state.get("_draft_scope_show_density", False):
                st.selectbox(
                    "Group density by",
                    options=list(_density_group_options.keys()),
                    key="_draft_scope_density_group_option",
                    help="Choose how to group events for density visualisation.",
                )
            st.slider(
                "Year range",
                min_value=OLDEST_DATASET_YEAR,
                max_value=current_year,
                key="_draft_scope_year_range",
            )
            st.multiselect(
                "Locations",
                options=location_options,
                key="_draft_scope_location_tokens",
                format_func=format_location_option,
                help="United States and Canada are grouped in one list. Additional countries can be added later.",
            )
            scope_submitted = st.form_submit_button("Apply scope filters", use_container_width=True)

    if scope_submitted:
        st.session_state["_scope_show_density"] = bool(st.session_state.get("_draft_scope_show_density", False))
        st.session_state["_scope_density_group_option"] = str(
            st.session_state.get("_draft_scope_density_group_option", "Season")
        )
        st.session_state["_scope_year_range"] = tuple(st.session_state.get("_draft_scope_year_range", (year_start, year_end)))
        st.session_state["_scope_location_tokens"] = [
            token
            for token in st.session_state.get("_draft_scope_location_tokens", [])
            if token in location_token_to_area
        ]
        st.session_state["_map_needs_reposition"] = True

    show_density = bool(st.session_state.get("_scope_show_density", False))
    selected_group = str(st.session_state.get("_scope_density_group_option", "Season"))
    st.session_state["_show_density_overlay"] = show_density
    st.session_state["_density_group_by"] = _density_group_options.get(selected_group, "SEASON")

    selected_years = tuple(st.session_state.get("_scope_year_range", (year_start, year_end)))
    selected_location_tokens = [
        token
        for token in st.session_state.get("_scope_location_tokens", default_locations)
        if token in location_token_to_area
    ]

    admin_areas = resolve_selected_admin_areas(selected_location_tokens, location_token_to_area)
    years = list(range(selected_years[0], selected_years[1] + 1))
    all_intensities = ["EF0", "EF1", "EF2", "EF3", "EF4", "EF5", "Unk"]

    # Load data and calculate available filters, even if admin_areas is empty (for responsive UI)
    data_revision = int(st.session_state.get("_data_revision", 0))
    need_canada = any(area in CANADIAN_PROVINCES_AND_TERRITORIES for area in admin_areas) or any(
        1980 <= year <= 2009 for year in years
    )
    data_key = (tuple(years), tuple(sorted(admin_areas)), need_canada, data_revision)
    should_show_spinner = st.session_state.get("_last_data_key") != data_key

    canada_warning: str | None = None
    ctx = st.spinner("Loading tornado history...") if should_show_spinner else nullcontext()
    with ctx:
        tornadoes = load_noaa_years_cached(tuple(years), data_revision).copy()
        if need_canada:
            try:
                canada = load_canadian_tornado_data_cached(data_revision)
                tornadoes = pd.concat([tornadoes, canada], ignore_index=True)
            except Exception as error:
                canada_warning = (
                    "Canadian data could not be loaded in this environment. "
                    f"Continuing with NOAA data only. Details: {error}"
                )
    st.session_state["_last_data_key"] = data_key

    # Normalise intensity values so EF-prefixed and F-prefixed both match checkboxes
    tornadoes["INTENSITY_NORM"] = tornadoes["INTENSITY"].apply(normalise_intensity)

    # Season colour: Spring=green, Summer=orange, Fall=purple, Winter=blue
    tornadoes["SEASON_COLOR"] = tornadoes["MONTH_NUM"].apply(season_color)

    # Season name column for density overlay grouping
    _month_to_season: dict[int, str] = {
        m: season
        for season, months in SEASON_MONTHS.items()
        for m in months
    }
    tornadoes["SEASON"] = tornadoes["MONTH_NUM"].map(_month_to_season).fillna("Unknown")

    prefiltered = tornadoes.loc[
        tornadoes["ADMIN_AREA"].isin(admin_areas)
        & tornadoes["YEAR"].isin(years)
    ].copy() if admin_areas else tornadoes.iloc[0:0].copy()  # Empty DataFrame if no admin_areas

    # Availability should come from stable loaded data, not from transient checkbox state.
    availability_source = tornadoes.loc[tornadoes["YEAR"].isin(years)].copy()
    if admin_areas:
        scoped_availability = availability_source.loc[
            availability_source["ADMIN_AREA"].isin(admin_areas)
        ].copy()
        availability_source = scoped_availability

    available_months = set(availability_source["MONTH_NUM"].dropna().astype(int).tolist())
    available_intensities = set(availability_source["INTENSITY_NORM"].dropna().astype(str).tolist())

    default_months = list(MONTH_NAMES.keys())
    default_seasons = list(SEASON_MONTHS.keys())
    default_intensities = ["EF3", "EF4", "EF5"]

    if "_detail_selected_months" not in st.session_state:
        st.session_state["_detail_selected_months"] = default_months.copy()
    if "_detail_selected_seasons" not in st.session_state:
        st.session_state["_detail_selected_seasons"] = default_seasons.copy()
    if "_detail_selected_intensities" not in st.session_state:
        st.session_state["_detail_selected_intensities"] = default_intensities.copy()

    for month in MONTH_NAMES:
        draft_key = f"_draft_month_{month}"
        if draft_key not in st.session_state:
            st.session_state[draft_key] = month in set(st.session_state["_detail_selected_months"])
    for season in SEASON_MONTHS:
        draft_key = f"_draft_season_{season}"
        if draft_key not in st.session_state:
            st.session_state[draft_key] = season in set(st.session_state["_detail_selected_seasons"])
    for label in all_intensities:
        draft_key = f"_draft_intensity_{label}"
        if draft_key not in st.session_state:
            st.session_state[draft_key] = label in set(st.session_state["_detail_selected_intensities"])

    # Render detail filters in a form to avoid reruns on every checkbox click.
    with st.sidebar:
        with st.form("detail_filters_form", border=False):
            st.markdown("**Months**")
            month_cols = st.columns(3)
            for idx, month in enumerate(MONTH_NAMES):
                is_available = month in available_months
                month_label = MONTH_ABBR[month] if is_available else f":gray[{MONTH_ABBR[month]}]"
                month_cols[idx % 3].checkbox(month_label, key=f"_draft_month_{month}")

            st.markdown("**Season**")
            season_list = list(SEASON_MONTHS)
            for row_start in range(0, len(season_list), 2):
                row_cols = st.columns(2)
                for col_idx, season in enumerate(season_list[row_start:row_start + 2]):
                    season_available = any(month in available_months for month in SEASON_MONTHS[season])
                    label = (
                        f"{SEASON_ICONS[season]} {season}"
                        if season_available
                        else f":gray[{SEASON_ICONS[season]} {season}]"
                    )
                    row_cols[col_idx].checkbox(label, key=f"_draft_season_{season}")

            st.markdown("**Intensity**")
            intensity_cols = st.columns(3)
            for i, label in enumerate(all_intensities):
                is_available = label in available_intensities
                intensity_label = label if is_available else f":gray[{label}]"
                intensity_cols[i % 3].checkbox(intensity_label, key=f"_draft_intensity_{label}")

            detail_submitted = st.form_submit_button("Apply detail filters", use_container_width=True)

    if detail_submitted:
        st.session_state["_detail_selected_months"] = [
            month for month in MONTH_NAMES if st.session_state.get(f"_draft_month_{month}", False)
        ]
        st.session_state["_detail_selected_seasons"] = [
            season for season in SEASON_MONTHS if st.session_state.get(f"_draft_season_{season}", False)
        ]
        st.session_state["_detail_selected_intensities"] = [
            label for label in all_intensities if st.session_state.get(f"_draft_intensity_{label}", False)
        ]

    selected_month_numbers: list[int] = list(st.session_state.get("_detail_selected_months", default_months))
    selected_seasons: list[str] = list(st.session_state.get("_detail_selected_seasons", default_seasons))
    selected_intensities: list[str] = list(st.session_state.get("_detail_selected_intensities", default_intensities))

    season_months: list[int] = []
    for season in (selected_seasons if selected_seasons else list(SEASON_MONTHS)):
        season_months.extend(SEASON_MONTHS[season])

    # Early return: stop processing if no locations are selected (but filters remain visible)
    if not admin_areas:
        st.info("Select at least one location.")
        return

    selected_months_in_scope = [m for m in selected_month_numbers if m in available_months]
    selected_intensities_in_scope = [i for i in selected_intensities if i in available_intensities]
    effective_months = [m for m in selected_months_in_scope if m in season_months]
    intensity_filter_values = (
        selected_intensities_in_scope
        if selected_intensities_in_scope
        else sorted(available_intensities)
    )

    filtered = prefiltered.loc[
        prefiltered["MONTH_NUM"].isin(effective_months if effective_months else selected_months_in_scope)
        & prefiltered["INTENSITY_NORM"].isin(intensity_filter_values)
    ].copy()
    filtered = filtered.sort_values("BEGIN_DATE_TIME").reset_index(drop=True)
    filtered["_row_idx"] = filtered.index

    # Clear stale map selection whenever the active filter set changes so the
    # table always reflects the current filtered rows.
    filter_key = (
        tuple(sorted(admin_areas)),
        tuple(sorted(effective_months if effective_months else selected_months_in_scope)),
        tuple(sorted(years)),
        tuple(sorted(intensity_filter_values)),
    )
    filters_changed = st.session_state.get("_filter_key") != filter_key
    if filters_changed:
        st.session_state["_filter_key"] = filter_key
        map_state = st.session_state.get("tornado_map")
        if isinstance(map_state, dict):
            map_state["selection"] = {}
            st.session_state["tornado_map"] = map_state
        st.session_state.pop("tornado_table", None)
        st.session_state.pop("_table_display_row_ids", None)
        st.session_state["_map_needs_reposition"] = True
        st.session_state.pop("_last_clicked_raw", None)
        st.session_state.pop("_map_view_state", None)

    if canada_warning:
        st.warning(canada_warning)

    if filtered.empty:
        st.warning("No tornadoes match the selected filters.")
        return

    temp_status_placeholder = None
    temp_scope_placeholder = None
    temp_coverage_placeholder = None

    with st.sidebar:
        st.markdown("---")
        st.caption("Selection summary")
        map_height = st.slider(
            "Map height (px)",
            min_value=300,
            max_value=1400,
            value=st.session_state.get("_map_height", DEFAULT_MAP_HEIGHT),
            step=50,
            key="_map_height",
            help="Drag to resize the map panel.",
        )
        
        if st.button(
            "Clear map selection",
            use_container_width=True,
            disabled=not bool(st.session_state.get("_last_clicked_raw")),
        ):
            st.session_state.pop("_last_clicked_raw", None)
        stats_a, stats_b = st.columns(2)
        stats_a.metric("Tracks", f"{len(filtered):,}")
        stats_b.metric("Areas", f"{filtered['ADMIN_AREA'].nunique()}")
        stats_c, stats_d = st.columns(2)
        stats_c.metric("Countries", f"{filtered['COUNTRY'].nunique()}")
        stats_d.metric("Years", f"{filtered['YEAR'].nunique()}")
        correction_series = filtered.get("TRACK_LENGTH_YARDS_CORRECTED")
        if correction_series is None:
            # Backward-compatibility for older typoed cache/state keys.
            correction_series = filtered.get(
                "Track_lengh_Yards_Corrected",
                pd.Series(False, index=filtered.index),
            )
        corrected_count = int(correction_series.fillna(False).astype(bool).sum())
        st.metric("Length Corrections", f"{corrected_count:,}")
        st.caption(
            "Counts selected rows where a northern-state NOAA track length above 99 "
            "was interpreted as yards and converted to miles."
        )
        temp_status_placeholder = st.empty()
        temp_scope_placeholder = st.empty()
        temp_coverage_placeholder = st.empty()
        with st.expander("Advanced", expanded=False):
            use_fahrenheit = st.toggle("Show temperatures in °F", value=True)
            temp_debounce_seconds = st.slider(
                "Temperature viewport settle (seconds)",
                min_value=1,
                max_value=5,
                value=3,
                step=1,
                help="Wait time after map resize/pan/zoom before temperature API checks refresh.",
            )
            max_map_rows = st.slider(
                "Map row limit",
                min_value=100,
                max_value=5000,
                value=st.session_state.get("_max_map_rows", MAX_MAP_ROWS),
                step=100,
                key="_max_map_rows",
                help=(
                    "Maximum tornado events rendered on the map at once. "
                    "⚠️ Values above 2,000 may cause slow rendering or GPU driver crashes on some systems."
                ),
            )
            if st.button(
                "Refresh data from source",
                width="stretch",
                help=(
                    "Mark all locally cached data as stale. "
                    "Fresh data will be downloaded from source the next time a filter changes. "
                    "Your current view and selections are not affected."
                ),
            ):
                try:
                    _init_db()
                    with sqlite3.connect(DB_PATH) as _conn:
                        _conn.execute("DELETE FROM fetch_log")
                except Exception:
                    pass
                st.session_state["_data_revision"] = int(st.session_state.get("_data_revision", 0)) + 1
                st.toast("Cache invalidated. Data will refresh on the next filter change.", icon="✅")

    previous_display_row_ids = st.session_state.get("_table_display_row_ids")
    if not isinstance(previous_display_row_ids, list) or len(previous_display_row_ids) != len(filtered):
        previous_display_row_ids = filtered.index.astype(int).tolist()
    previous_display_row_ids = [int(idx) for idx in previous_display_row_ids]

    map_selected_idxs = get_map_selected_rows(
        {"last_object_clicked": st.session_state.get("_last_clicked_raw")}
    )
    table_selected_idxs = get_table_selected_rows(
        st.session_state.get("tornado_table", {}), previous_display_row_ids
    )
    selected_idxs = map_selected_idxs | table_selected_idxs

    table_columns = [
        "BEGIN_DATE_TIME",
        "END_DATE_TIME",
        "COUNTRY",
        "ADMIN_AREA",
        "AREA_NAME",
        "INTENSITY",
        "TRACK_LENGTH",
        "TRACK_WIDTH",
        "BEGIN_LAT",
        "BEGIN_LON",
        "END_LAT",
        "END_LON",
        "SOURCE_DB",
    ]
    display_df = filtered[table_columns].copy()

    temp_scope_text = ""
    temp_status_text = ""
    temp_coverage_text = ""

    display_df["TEMP_HIGH"] = "—"
    display_df["TEMP_LOW"] = "—"
    filtered["TEMP_HIGH"] = "—"
    filtered["TEMP_LOW"] = "—"

    map_state_for_temp = st.session_state.get("tornado_map", {})
    map_bounds = find_map_bounds(map_state_for_temp)
    debounce_seconds = float(temp_debounce_seconds)

    now_ts = time.time()
    bounds_sig = tuple(round(value, 3) for value in map_bounds) if map_bounds is not None else None
    if st.session_state.get("_temp_bounds_sig") != bounds_sig:
        st.session_state["_temp_bounds_sig"] = bounds_sig
        st.session_state["_temp_bounds_changed_at"] = now_ts
    changed_at = float(st.session_state.get("_temp_bounds_changed_at", now_ts))
    seconds_since_change = max(0.0, now_ts - changed_at)
    bounds_stable = map_bounds is None or seconds_since_change >= debounce_seconds
    temp_refresh_needed = bool(filters_changed) or (
        bounds_stable and st.session_state.get("_temp_last_processed_sig") != bounds_sig
    )

    if map_bounds is not None:
        south, west, north, east = map_bounds
        lat_visible = (
            filtered["BEGIN_LAT"].between(south, north)
            | filtered["END_LAT"].between(south, north)
        )
        if west <= east:
            lon_visible = (
                filtered["BEGIN_LON"].between(west, east)
                | filtered["END_LON"].between(west, east)
            )
        else:
            # Handle anti-meridian crossing windows.
            lon_visible = (
                filtered["BEGIN_LON"].ge(west)
                | filtered["BEGIN_LON"].le(east)
                | filtered["END_LON"].ge(west)
                | filtered["END_LON"].le(east)
            )
        temp_source_df = filtered[lat_visible & lon_visible]
    else:
        temp_source_df = filtered

    temp_scope_text = f"Temp fetch scope: {len(temp_source_df):,} visible rows / {len(filtered):,} filtered rows."
    temp_source_idx = set(temp_source_df.index)

    # Rows in the viewport that don't yet have a stored temperature.
    already_cached = filtered["TEMP_HIGH_C"].notna()
    rows_needing_fetch = temp_source_df[~already_cached.loc[temp_source_df.index]]

    if not rows_needing_fetch.empty and temp_refresh_needed:
        fetch_df = rows_needing_fetch[["BEGIN_LAT", "BEGIN_LON", "COUNTRY", "BEGIN_DATE_TIME"]].copy()
        fetch_df["_lat_r"] = fetch_df["BEGIN_LAT"].round(0)
        fetch_df["_lon_r"] = fetch_df["BEGIN_LON"].round(0)
        fetch_df["_date_s"] = fetch_df["BEGIN_DATE_TIME"].dt.strftime("%Y-%m-%d")
        unique_combos = fetch_df[["_lat_r", "_lon_r", "_date_s"]].drop_duplicates()
        if len(unique_combos) > TEMPERATURE_FETCH_LIMIT:
            temp_status_text = (
                f"Temperature note: fetching {TEMPERATURE_FETCH_LIMIT:,} of "
                f"{len(unique_combos):,} unique location/date combinations."
            )
            unique_combos = unique_combos.head(TEMPERATURE_FETCH_LIMIT)

        # Single-query bulk lookup of the standalone temperature cache.
        combo_cache = _batch_query_temperature_cache(unique_combos)

        # API-fetch only combos absent from the standalone cache.
        api_keys = [
            (float(r["_lat_r"]), float(r["_lon_r"]), str(r["_date_s"]))
            for _, r in unique_combos.iterrows()
            if (float(r["_lat_r"]), float(r["_lon_r"]), str(r["_date_s"])) not in combo_cache
        ]
        if api_keys:
            with st.spinner(f"Fetching temperatures for {len(api_keys):,} new location/date combinations…"):
                for key in api_keys:
                    combo_cache[key] = fetch_daily_temperature(key[0], key[1], key[2])

        # Write newly fetched temps onto filtered and persist to the event cache tables.
        db_updates: list[tuple] = []
        for idx in rows_needing_fetch.index:
            frow = filtered.loc[idx]
            begin_date = pd.to_datetime(frow["BEGIN_DATE_TIME"], errors="coerce")
            if pd.isna(begin_date):
                continue
            temp_key = (
                round(float(frow["BEGIN_LAT"]), 0),
                round(float(frow["BEGIN_LON"]), 0),
                begin_date.strftime("%Y-%m-%d"),
            )
            high_c, low_c = combo_cache.get(temp_key, (None, None))
            filtered.at[idx, "TEMP_HIGH_C"] = high_c
            filtered.at[idx, "TEMP_LOW_C"] = low_c
            if high_c is not None or low_c is not None:
                db_updates.append((
                    high_c, low_c, str(frow["SOURCE_DB"]),
                    str(frow["BEGIN_DATE_TIME"]),
                    float(frow["BEGIN_LAT"]), float(frow["BEGIN_LON"]),
                ))
        if db_updates:
            _update_event_temps_in_db(db_updates)

    elif not rows_needing_fetch.empty and not bounds_stable:
        remaining_seconds = max(0.0, debounce_seconds - seconds_since_change)
        temp_status_text = (
            f"Temperature checks paused while map view changes. "
            f"Waiting {remaining_seconds:.1f}s for viewport to settle."
        )

    if temp_refresh_needed and bounds_stable:
        st.session_state["_temp_last_processed_sig"] = bounds_sig

    # Build formatted display strings from the (now-populated) celsius columns.
    loaded_count = 0
    for idx in display_df.index:
        in_scope = idx in temp_source_idx
        high_c_val = filtered.at[idx, "TEMP_HIGH_C"] if in_scope else None
        low_c_val = filtered.at[idx, "TEMP_LOW_C"] if in_scope else None
        high_c_f = coerce_float(high_c_val) if high_c_val is not None and pd.notna(high_c_val) else None
        low_c_f = coerce_float(low_c_val) if low_c_val is not None and pd.notna(low_c_val) else None
        h_str = format_temperature(high_c_f, use_fahrenheit)
        l_str = format_temperature(low_c_f, use_fahrenheit)
        display_df.at[idx, "TEMP_HIGH"] = h_str
        display_df.at[idx, "TEMP_LOW"] = l_str
        filtered.at[idx, "TEMP_HIGH"] = h_str
        filtered.at[idx, "TEMP_LOW"] = l_str
        if h_str != "—" or l_str != "—":
            loaded_count += 1

    scope_count = int(len(temp_source_df))
    temp_coverage_text = f"Temperatures loaded for {loaded_count:,} / {scope_count:,} rows."

    if temp_status_text and temp_status_placeholder is not None:
        temp_status_placeholder.caption(temp_status_text)
    if temp_scope_text and temp_scope_placeholder is not None:
        temp_scope_placeholder.caption(temp_scope_text)
    if temp_coverage_text and temp_coverage_placeholder is not None:
        temp_coverage_placeholder.caption(temp_coverage_text)

    if st_folium is None or folium is None:
        st.error("Map dependency missing. Install `folium` and `streamlit-folium`.")
        return

    persisted_view = st.session_state.get("_map_view_state")
    if persisted_view is None:
        persisted_view = find_map_view_state(
            st.session_state.get("tornado_map", {}),
            allow_bounds_fallback=True,
        )

    fmap, view_lat, view_lon, view_zoom = build_map(
        filtered,
        selected_rows=selected_idxs,
        map_state=st.session_state.get("_last_map_state_for_view", st.session_state.get("tornado_map", {})),
        persisted_view=persisted_view,
        map_height=map_height,
        max_map_rows=max_map_rows,
        show_density_overlay=st.session_state.get("_show_density_overlay", False),
        density_group_by=st.session_state.get("_density_group_by"),
    )
    _needs_pos = st.session_state.get("_map_needs_reposition", True)
    _st_folium_kwargs: dict = {
        "key": "tornado_map_widget",
        "height": map_height,
        "use_container_width": True,
        "returned_objects": ["zoom", "center", "bounds", "last_object_clicked"],
    }
    if _needs_pos:
        _st_folium_kwargs["center"] = (view_lat, view_lon)
        _st_folium_kwargs["zoom"] = int(round(view_zoom))
        st.session_state["_map_needs_reposition"] = False
    map_event = st_folium(fmap, **_st_folium_kwargs)
    if isinstance(map_event, dict):
        new_click = map_event.get("last_object_clicked")
        if new_click is not None:
            st.session_state["_last_clicked_raw"] = new_click

        event_view = find_map_view_state(map_event, allow_bounds_fallback=True)
        if event_view is not None:
            old_view = st.session_state.get("_map_view_state")
            if old_view is None or (
                abs(event_view[0] - old_view[0]) > 0.005
                or abs(event_view[1] - old_view[1]) > 0.005
                or abs(event_view[2] - old_view[2]) > 0.05
            ):
                st.session_state["_map_view_state"] = event_view

        st.session_state["tornado_map"] = map_event

    map_state_for_sync = pick_best_map_state(map_event, st.session_state.get("tornado_map", {}))
    st.session_state["_last_map_state_for_view"] = map_state_for_sync

    selected_idxs = get_map_selected_rows(
        {"last_object_clicked": st.session_state.get("_last_clicked_raw")}
    ) | table_selected_idxs

    visible_rows = min(len(display_df), 10)
    table_height = max(96, 34 * (visible_rows + 1))
    with st.sidebar:
        st.divider()
        _exp_left, _exp_right = st.columns(2)
        _exp_left.download_button(
            "Export CSV",
            data=to_csv_bytes(filtered),
            file_name="tornado_tracks.csv",
            mime="text/csv",
            use_container_width=True,
        )
        _exp_right.download_button(
            "Export XLSX",
            data=to_excel_bytes(filtered),
            file_name="tornado_tracks.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with st.expander("Data table", expanded=True):

        # Add formatted length/width columns with units
        display_df = display_df.copy()

        # Google search URL: date + area + admin area + "Tornado"
        def _search_url(row: pd.Series) -> str:
            date_str = (
                row["BEGIN_DATE_TIME"].strftime("%B %#d, %Y")
                if isinstance(row["BEGIN_DATE_TIME"], pd.Timestamp) and not pd.isnull(row["BEGIN_DATE_TIME"])
                else str(row["BEGIN_DATE_TIME"])
            )
            query = " ".join(filter(None, [date_str, str(row.get("AREA_NAME", "")), str(row.get("ADMIN_AREA", "")), "Tornado"]))
            return "https://www.google.com/search?" + urlencode({"q": query})

        display_df["SEARCH_URL"] = display_df.apply(_search_url, axis=1)

        track_length_numeric = pd.to_numeric(display_df["TRACK_LENGTH"], errors="coerce")
        if track_length_numeric.notna().any():
            max_track_length = float(track_length_numeric.dropna().abs().max())
            integer_digits = 2 if max_track_length < 100 else len(str(int(max_track_length)))
            track_length_width = integer_digits + 3  # decimal point + 2 fractional digits
        else:
            track_length_width = 4

        display_df["TRACK_LENGTH"] = display_df.apply(
            lambda row: (
                f"{float(pd.to_numeric(str(row['TRACK_LENGTH']), errors='coerce')):0{track_length_width}.2f} "
                f"{'km' if row['COUNTRY'] == 'Canada' else 'mi'}"
                if pd.notna(pd.to_numeric(str(row["TRACK_LENGTH"]), errors="coerce"))
                else format_measurement(
                    row["TRACK_LENGTH"],
                    "km" if row["COUNTRY"] == "Canada" else "mi",
                )
            ),
            axis=1,
        )
        display_df["TRACK_WIDTH"] = display_df.apply(
            lambda row: format_measurement(
                row["TRACK_WIDTH"],
                "m" if row["COUNTRY"] == "Canada" else "yd",
            ),
            axis=1,
        )
        if "EVENT_NARRATIVE" not in display_df.columns:
            display_df["EVENT_NARRATIVE"] = ""
        display_df = display_df[
            [
                "BEGIN_DATE_TIME",
                "END_DATE_TIME",
                "COUNTRY",
                "ADMIN_AREA",
                "AREA_NAME",
                "INTENSITY",
                "TRACK_LENGTH",
                "TRACK_WIDTH",
                "BEGIN_LAT",
                "BEGIN_LON",
                "END_LAT",
                "END_LON",
                "TEMP_HIGH",
                "TEMP_LOW",
                "EVENT_NARRATIVE",
                "SOURCE_DB",
                "SEARCH_URL",
            ]
        ]

        if selected_idxs:
            sel_mask = display_df.index.isin(selected_idxs)
            display_df = pd.concat([display_df[sel_mask], display_df[~sel_mask]])
        st.session_state["_table_display_row_ids"] = [int(idx) for idx in display_df.index.tolist()]

        # Highlight selected rows in yellow
        selected_positional = [
            i for i, idx in enumerate(display_df.index) if idx in selected_idxs
        ]

        def _highlight_selected(styler: "pd.io.formats.style.Styler") -> "pd.io.formats.style.Styler":
            if not selected_positional:
                return styler
            return styler.apply(
                lambda col: [
                    "background-color: #fde047; color: #1a1a1a;" if i in selected_positional else ""
                    for i in range(len(col))
                ],
                axis=0,
            )

        try:
            df_to_show = _highlight_selected(display_df.style)
        except Exception:
            df_to_show = display_df
        st.dataframe(
            df_to_show,
            width="stretch",
            hide_index=True,
            height=table_height,
            on_select="rerun",
            selection_mode="multi-row",
            key="tornado_table",
            column_config={
                "TRACK_LENGTH": st.column_config.TextColumn("Track Length"),
                "TRACK_WIDTH": st.column_config.TextColumn("Track Width"),
                "TEMP_HIGH": st.column_config.TextColumn("High Temp"),
                "TEMP_LOW": st.column_config.TextColumn("Low Temp"),
                "SEARCH_URL": st.column_config.LinkColumn("Search", display_text="🔍 Google"),
            },
        )


if __name__ == "__main__":
    main()
