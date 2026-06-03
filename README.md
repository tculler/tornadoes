# Tornado Map · v1.3

A Streamlit app that displays historical tornado tracks on an interactive map using NOAA Storm Events data, with optional Canadian National Tornado Database support.

## Features

- Interactive folium map with clickable tornado tracks and start-point dots
- Dots colour-coded by season (green=Spring, orange=Summer, purple=Fall, blue=Winter)
- **Density overlay** shows heatmap patterns groupable by Season, Intensity, Country, or State/Province to identify tornado clusters
- Clicking a dot or track path highlights the matching row in the data table; selected rows are sorted to the top
- Multi-select: click multiple dots to highlight several tornadoes at once
- Temperature overlay showing daily high/low at each tornado start point (via Open-Meteo archive API); toggle °F / °C
- Hover popups with tornado details (date, intensity, location, track length)
- Map height is resizable via the sidebar slider
- **Google Search link** in the data table — each row has a clickable 🔍 Google link pre-filled with the event date, location, and "Tornado" for quick research
- Filtered results exportable to CSV or XLSX directly from the sidebar
- Apply-based sidebar filter forms reduce reruns while editing checkboxes/sliders
- Local SQLite cache (`tornado_cache.db`) stores downloaded data for 7 days so the app loads instantly on repeat visits

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
streamlit run tornadomapp.py
```

The app opens at `http://localhost:8501`. Data is downloaded from NOAA on first use and cached locally; subsequent loads are near-instant.

## Sidebar controls

### Filters

| Control | Type | Description |
|---------|------|-------------|
| **Locations** | Multi-select | Single combined selector for U.S. states and Canadian provinces/territories. Options are grouped under country headers (default: Great Lakes region). |
| **Year range** | Slider | Select a start and end year; full NOAA record runs from 1950 to the current year |
| **Months** | Checkboxes | Toggle individual months; options with no data are shown in light gray but remain selectable |
| **Season** | Checkboxes | Toggle Spring / Summer / Fall / Winter; no-data options appear in light gray |
| **Intensity** | Checkboxes | Filter by EF rating (EF0–EF5) plus Unk for unrated events (default: EF3, EF4, EF5); no-data options appear in light gray |
| **Apply scope filters** | Button | Applies density/year/location changes in one rerun |
| **Apply detail filters** | Button | Applies months/season/intensity changes in one rerun |

### Display

| Control | Type | Description |
|---------|------|-------------|
| **Show density overlay** | Checkbox | Enable semi-transparent heatmap layers showing tornado density patterns (default: off). When enabled, choose a grouping criterion below. |
| **Group density by** | Dropdown | Select how to group tornado events for density visualization: Season (spring/summer/fall/winter hotspots), Intensity (F4/F5 clusters), Country (US vs Canada patterns), or State/Province (regional concentrations). Each group gets its own heatmap layer with color coding. |
| **Map height (px)** | Slider 300–1400 | Drag to resize the map panel |

### Export

Below the filter controls a divider separates two compact buttons:

| Button | Description |
|--------|-------------|
| **Export CSV** | Download the full filtered dataset as a CSV file |
| **Export XLSX** | Download the full filtered dataset as an Excel workbook |

### Advanced (collapsed)

Expand the **Advanced** section at the bottom of the sidebar to access temperature settings, performance options, and the data refresh control.

| Control | Type | Description |
|---------|------|-------------|
| **Show temperatures in °F** | Toggle | Display temperature data in degrees Fahrenheit; off shows °C (default: on) |
| **Temperature viewport settle** | Slider 1–5 s | How long to wait after a map pan/zoom before temperature data refreshes |
| **Map row limit** | Slider 100–5000 | Maximum tornado events rendered on the map at once (default: 2,000). ⚠️ Values above 2,000 may cause slow rendering or GPU driver crashes on some systems. |
| **Refresh data from source** | Button | Marks all locally cached data as stale so it will be re-downloaded from NOAA / ECCC on the next filter change. Your current view, selections, and table contents are not affected. To discard all cached data entirely, delete `tornado_cache.db` from the project folder. |

### Selection summary

Live metrics showing the number of tracks, states/provinces, countries, and years in the current filtered view, plus a count of track length corrections applied to northern-state data.

## Using the map

1. **Pan and zoom** freely — the map view is preserved across filter changes.
2. **Click a dot** to select a tornado and jump to its row in the data table below the map.
3. **Click multiple dots** to select several tornadoes at once; the table shows all selected rows at the top.
4. **Click an empty area** on the map to clear the selection.
5. The dot size scales with zoom level so tracks remain visible when zoomed out.

## Data table

The table below the map shows all tornadoes matching the current filters. Selected rows (from map clicks) appear at the top with a yellow highlight.

Each row includes a **🔍 Google** link in the **Search** column that opens a Google search pre-filled with the event date, location, and admin area — useful for finding news reports or historical records about a specific tornado.

Use the **Export CSV** / **Export XLSX** buttons in the sidebar to download the full filtered dataset.

## Local cache

Downloaded data is stored in `tornado_cache.db` (SQLite) in the project folder. Each data source (NOAA per year, Canadian dataset, temperature readings) is refreshed automatically after 7 days.

To force a fresh download without waiting for the TTL to expire, expand the **Advanced** section at the bottom of the sidebar and click **Refresh data from source**. This marks all entries as stale so data re-downloads on the next filter change — it does not delete the cached rows or affect your current view. To wipe the cache entirely, delete the `tornado_cache.db` file.

## Data sources

- **NOAA Storm Events** — yearly gzip CSV files (1950–present):  
  https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
- **Canadian National Tornado Database** (1980–2009, best-effort) — ECCC Open Canada:  
  https://open.canada.ca/data/en/dataset/fd3355a7-ae34-4df7-b477-07306182db69  
  https://open.canada.ca/data/en/dataset/65658050-7a80-4da3-9a09-da137c203a34
- **Temperature data** — Open-Meteo historical archive API:  
  https://open-meteo.com/en/docs/historical-weather-api

## Requirements

```
streamlit>=1.45
pandas>=2.2
requests>=2.32
folium>=0.17
streamlit-folium>=0.22
openpyxl>=3.1
shapely>=2.0
streamlit-autorefresh>=1.0
```

## Notes

- For U.S. events, county labels are derived from plotted start coordinates using U.S. county polygons when available, instead of relying solely on NOAA's text fields. If the lookup is unavailable, the app falls back to NOAA `BEGIN_LOCATION` and then `CZ_NAME`.
- Canadian tornado data (1980–2009) is always included when the selected year range overlaps; the ECCC CSV is fetched from the Azure-backed API. On failure the app falls back to NOAA-only and shows a warning banner. The CSV uses Windows-1252 encoding in places; the loader falls back to latin-1 automatically.
- Intensity values are normalised from legacy F-scale (`F0`–`F5`) and EF-scale (`EF0`–`EF5`) to a common EF set; unrecognised values map to `Unk`.
- Track lengths reported above 99 for tornadoes in northern states are assumed to be in yards and converted to miles automatically; affected rows are counted in the sidebar's *Length Corrections* metric.
- Temperature data is cached per event in `tornado_cache.db`; only events visible in the current map viewport that have no stored temperature trigger API calls.

## Running tests

```powershell
.venv\Scripts\python -m pytest test_tornadomapp.py -v
```

72 tests across 21 test classes. All tests use in-memory or temporary SQLite databases and do not touch the live cache file or make network requests.

| Test class | Coverage |
|---|---|
| `TestNormaliseIntensity` | `normalise_intensity` — EF passthrough, F→EF mapping, case/whitespace, unknown values |
| `TestSeasonColor` | `season_color` — all 12 months, string input, invalid input |
| `TestParseWktEndpoints` | `parse_wkt_endpoints` — two-point line, multi-point, non-string, empty |
| `TestDefaultYearRange` | `default_year_range` — half-history span, lower bound |
| `TestGroupedLocationOptions` | `build_grouped_location_options` / `format_location_option` / `resolve_selected_admin_areas` — country headers, plain labels, dedupe, header-ignore behavior |
| `TestPickColumn` | `pick_column` — case-insensitive match, no match |
| `TestSeriesOrDefault` | `series_or_default` — found column, missing column default |
| `TestFormatUsCountyName` | `format_us_county_name` — county display-name construction |
| `TestResolveUsCountyName` | `resolve_us_county_name` — point-in-county lookup using polygon index |
| `TestResolveNoaaAreaName` | `resolve_noaa_area_name` — county lookup priority and fallback order |
| `TestIsCacheFresh` | `_is_cache_fresh` — missing entry, fresh/stale TTL, key/source isolation |
| `TestNoaaCacheRoundTrip` | `_write_noaa_to_db` / `_read_noaa_from_db` — write, replace, missing year, track column, bool flag |
| `TestCanadaCacheRoundTrip` | `_write_canada_to_db` / `_read_canada_from_db` — write, replace, empty DB |
| `TestFetchDailyTemperature` | `fetch_daily_temperature` — cache hit, cache miss + API call + store, API error |
| `TestVisibleInBoundsMask` | `visible_in_bounds_mask` — all inside, none inside, partial overlap |
| `TestCelsiusToFahrenheit` | `celsius_to_fahrenheit` — freezing, boiling, body temperature |
| `TestFormatTemperature` | `format_temperature` — None dash, °F format, °C format |
| `TestBatchQueryTemperatureCache` | `_batch_query_temperature_cache` — stored values, empty input, missing key |
| `TestUpdateEventTempsInDb` | `_update_event_temps_in_db` — NOAA row update, Canada row update |
| `TestFetchRemoteCsvEncoding` | `fetch_remote_csv` — UTF-8 CSV, latin-1 fallback on decode error, HTML raises ValueError |
| `TestNormalizeCanadaAdminArea` | `normalize_canada_admin_area` — abbreviation expansion, full-name passthrough, unknown text fallback |
| `TestEnsureFreshDataCheck` | `_ensure_fresh_data_check` — marks current year and Canada for refresh when stale, preserves old years, does nothing when fresh |
