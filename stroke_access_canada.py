"""
ONE-FILE Canada Stroke Access Pipeline
- Boundaries (DA/CT) -> centroids
- Stroke centres Excel -> geocode -> SRH/PSC/CSC
- OSMnx routing -> travel time to nearest SRH/PSC/CSC
- Outputs: access_metrics.csv + per-province routing caches + geocode cache

Run:
  pip install pandas numpy geopandas shapely pyproj pyyaml openpyxl osmnx networkx tqdm pyarrow statsmodels matplotlib contextily geopy requests
  python stroke_access_canada.py
"""

from __future__ import annotations

import os
import sys
import time

# PyInstaller fix: Set GDAL_DATA if frozen
if getattr(sys, 'frozen', False):
    import glob
    base_wd = sys._MEIPASS
    # Look for gdal data in potential locations
    potentials = [
        os.path.join(base_wd, "gdal"),
        os.path.join(base_wd, "share", "gdal"),
        os.path.join(base_wd, "pyogrio", "gdal_data"),
        os.path.join(base_wd, "osgeo", "data", "gdal"),
    ]
    # Also search for any folder named 'gdal-data' or similar
    
    found = False
    for p in potentials:
        if os.path.exists(p):
            os.environ["GDAL_DATA"] = p
            found = True
            break
    
    if not found:
        # Fallback: try to find any directory that looks like gdal data
        # recursively is too slow, just check top level directories
        try:
            for root, dirs, files in os.walk(base_wd):
                if "gdalvrt.xsd" in files:
                    os.environ["GDAL_DATA"] = root
                    break
        except Exception:
            pass


import math
import pandas as pd
import geopandas as gpd
import osmnx as ox
import networkx as nx
from tqdm import tqdm
from shapely.ops import unary_union

from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

# =========================
# EDIT THESE SETTINGS ONLY
# =========================

BOUNDARY_FILE = resource_path("statcan_boundaries.gml")   # <- your DA/CT boundary file (gml or SHP)
BOUNDARY_LAYER = None                       # <- if your gml has layers, set the layer name string here
GEO_ID_COL = "DAUID"                        # <- DAUID or CTUID column in boundaries
PROVINCE_COL = "PRUID"                      # <- province code column in boundaries (preferred)
RUN_PROVINCES = ["35"]                      # <- start with one province to test (35=Ontario). Later set many.

CENSUS_PROFILE_CSV = resource_path("census_profile.csv")   # <- optional but recommended (Population/income/etc.)
CENSUS_JOIN_COL = "DAUID"                   # <- must match GEO_ID_COL
CENSUS_KEEP_COLS = [
    "DAUID", "Population", "Median_income", "Indigenous_pct",
    "Visible_minority_pct", "Age65plus_pct", "Rural_flag"
]



# STROKE_CENTRES_XLSX = resource_path("stroke_centers.xlsx") # <- OLD local file
# Google Sheets URL (Export as CSV for "Master list" sheet)
STROKE_CENTRES_URL = "https://docs.google.com/spreadsheets/d/1iswC1SgUTdS63Nve-5CyWh9H9H8xOpA247w6889vqj8/export?format=csv&sheet=Master%20list"

CENTRES_SHEET = "Master list"
CENTRE_NAME_COL = "Hospital"
CENTRE_CITY_COL = "City"
CENTRE_PROVINCE_COL = "Province"
# ... (rest of constants are same)

# ... (Helpers function are same)

def load_and_geocode_centres() -> pd.DataFrame:
    print(f"Downloading stroke centres from: {STROKE_CENTRES_URL}...")
    
    try:
        # Read with no header first to find the correct row
        df_raw = pd.read_csv(STROKE_CENTRES_URL, header=None)
    except Exception as e:
        print(f"Error downloading/parsing CSV: {e}")
        return pd.DataFrame()

    # Smart Header Detection
    header_idx = -1
    for i in range(min(10, len(df_raw))):
        row_values = [str(x).strip() for x in df_raw.iloc[i].values]
        if "Hospital" in row_values and "City" in row_values:
            header_idx = i
            break
            
    if header_idx != -1:
        print(f"DEBUG: Found header at row {header_idx}")
        # Set proper header
        df = df_raw.iloc[header_idx+1:].copy()
        df.columns = [str(x).strip() for x in df_raw.iloc[header_idx].values]
    else:
        print("DEBUG: Could not find header row containing 'Hospital' and 'City'. Using first row.")
        df = df_raw
        df.columns = [str(x).strip() for x in df.iloc[0].values]

    print(f"DEBUG: Data columns: {list(df.columns)}")

    # Rename columns to standard internal names
    rename_map = {
        "Hospital": "centre_name",
        "City": "city",
        "Province": "prov",
        "Latitude": "lat_input",
        "Longitude": "lon_input",
        "Longtitude": "lon_input", # Handle typo
        "Lat": "lat_input",        # Handle abbreviation
        "Lon": "lon_input"         # Handle abbreviation
    }
    df.rename(columns=rename_map, inplace=True)
    
    # Filter out empty rows
    if "ID" in df.columns:
        df = df[df["ID"].notna()]
    elif "id" in df.columns.str.lower():
         # Case insensitive fallback for ID
         idx = df.columns.str.lower() == 'id'
         if idx.any():
            col = df.columns[idx][0]
            df = df[df[col].notna()]
    elif "centre_name" in df.columns:
        df = df[df["centre_name"].notna()]


    # Synthesize centre_type from the 3 columns
    def get_type(row):
        # Check "Comprehensive" first (highest level)
        if str(row.get("Comprehensive", "")).strip().lower() == "yes":
            return "CSC"
        if str(row.get("Primary", "")).strip().lower() == "yes":
            return "PSC"
        if str(row.get("Stroke ready Hospital", "")).strip().lower() == "yes":
            return "SRH"
        return "UNKNOWN"

    df["centre_type"] = df.apply(get_type, axis=1)
    
    # Filter only valid types
    centres = df[df["centre_type"].isin(["SRH", "PSC", "CSC"])].copy()
    centres = centres.drop_duplicates(subset=["centre_name", "city"]).reset_index(drop=True)

    cache = load_cache()
    geolocator = Nominatim(user_agent=GEOCODER_USER_AGENT)
    geocode = RateLimiter(
        geolocator.geocode,
        min_delay_seconds=float(GEOCODER_PAUSE_SECONDS),
        return_value_on_exception=None
    )

    lats, lons, disp, ok_list = [], [], [], []

    print(f"Processing {len(centres)} hospitals...")

    for _, r in tqdm(centres.iterrows(), total=len(centres)):
        # 1. Try to use existing lat/lon from Excel if available
        # Check if lat_input/lon_input exist and are numbers
        in_lat = r.get("lat_input", None)
        in_lon = r.get("lon_input", None)
        
        valid_input = False
        try:
            flat = float(in_lat)
            flon = float(in_lon)
            if not pd.isna(flat) and not pd.isna(flon) and flat != 0 and flon != 0:
                valid_input = True
                lats.append(flat)
                lons.append(flon)
                disp.append("From Excel")
                ok_list.append(True)
        except Exception:
            pass
            
        if valid_input:
            continue

        # 2. Fallback to Geocoding
        q = build_geocode_query(r)
        hit = cache[cache["query"] == q]
        if not hit.empty:
            lats.append(hit.iloc[0]["lat"])
            lons.append(hit.iloc[0]["lon"])
            disp.append(hit.iloc[0].get("display_name", ""))
            ok_list.append(bool(hit.iloc[0].get("success", True)))
            continue

        lat = lon = None
        dn = ""
        ok = False

        for attempt in range(1, GEOCODER_MAX_RETRIES + 1):
            try:
                loc = geocode(q)
                if loc is not None:
                    lat = float(loc.latitude)
                    lon = float(loc.longitude)
                    dn = getattr(loc, "address", "") or ""
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(float(GEOCODER_PAUSE_SECONDS) * attempt)

        lats.append(lat); lons.append(lon); disp.append(dn); ok_list.append(ok)

        cache = pd.concat([cache, pd.DataFrame([{
            "query": q, "lat": lat, "lon": lon, "display_name": dn, "success": ok
        }])], ignore_index=True)
        save_cache(cache)

    centres["lat"] = pd.to_numeric(lats, errors="coerce")
    centres["lon"] = pd.to_numeric(lons, errors="coerce")
    centres["geocode_ok"] = ok_list
    centres["geocode_display"] = disp

    failures = centres[~centres["geocode_ok"] | centres["lat"].isna() | centres["lon"].isna()]
    failures = centres[~centres["geocode_ok"] | centres["lat"].isna() | centres["lon"].isna()]
    if len(failures) > 0:
        fail_file = "centres_geocode_failures.csv"
        try:
            # Try to save next to the executable if frozen
            if getattr(sys, 'frozen', False):
                fail_file = os.path.join(os.path.dirname(sys.executable), "centres_geocode_failures.csv")
        except Exception:
            pass
            
        failures.to_csv(fail_file, index=False)
        print(f"\n⚠️ WARNING: Geocoding failed for {len(failures)} hospitals.")
        print(f"   These hospitals will be SKIPPED in the analysis.")
        print(f"   List saved to: {fail_file}")
        print("   To fix: Update your Excel file with an 'Address' column and cleaner names.\n")
        
        # Filter out failures
        centres = centres[centres["geocode_ok"] & centres["lat"].notna() & centres["lon"].notna()].copy()

    return centres[["centre_name", "centre_type", "lat", "lon"]].copy()

def build_graph_for_province(points_gdf: gpd.GeoDataFrame, pruid: str):
    ensure_dir(GRAPH_CACHE_DIR)
    graph_path = os.path.join(GRAPH_CACHE_DIR, f"graph_pruid_{pruid}.graphml")
    if os.path.exists(graph_path):
        return ox.load_graphml(graph_path)

    hull = unary_union(points_gdf.geometry).convex_hull.buffer(0.2)
    print(f"Downloading OSM graph for PRUID={pruid} ...")
    G = ox.graph_from_polygon(hull, network_type="drive", simplify=True)
    G = ox.add_edge_speeds(G)
    G = ox.add_edge_travel_times(G)
    ox.save_graphml(G, graph_path)
    print(f"Saved graph: {graph_path}")
    return G

def route_times_one_province(geos: gpd.GeoDataFrame, centres: pd.DataFrame, pruid: str) -> pd.DataFrame:
    ensure_dir(ROUTING_CACHE_DIR)
    out_path = os.path.join(ROUTING_CACHE_DIR, f"routing_pruid_{pruid}.parquet")
    if os.path.exists(out_path):
        return pd.read_parquet(out_path)

    sub = geos[geos[PROVINCE_COL].astype(str) == str(pruid)].copy()
    if sub.empty:
        return pd.DataFrame()

    sub_points = gpd.GeoDataFrame(
        sub[[GEO_ID_COL, "centroid_lon", "centroid_lat"]].copy(),
        geometry=gpd.points_from_xy(sub["centroid_lon"], sub["centroid_lat"]),
        crs=4326
    )

    G = build_graph_for_province(sub_points, pruid)

    centres_by_type = {t: df for t, df in centres.groupby("centre_type")}

    rows = []
    print(f"Routing {len(sub_points)} areas for PRUID={pruid} ... (this can take time)")

    for _, r in tqdm(sub_points.iterrows(), total=len(sub_points)):
        oid = r[GEO_ID_COL]
        origin = ox.distance.nearest_nodes(G, X=r["centroid_lon"], Y=r["centroid_lat"])

        res = {"geo_id": oid}
        for ctype, cdf in centres_by_type.items():
            best_val = math.nan
            best_name = None

            for _, c in cdf.iterrows():
                dest = ox.distance.nearest_nodes(G, X=c["lon"], Y=c["lat"])
                try:
                    if USE_TRAVEL_TIME:
                        tt_sec = nx.shortest_path_length(G, origin, dest, weight="travel_time")
                        val = tt_sec / 60.0
                    else:
                        dist_m = nx.shortest_path_length(G, origin, dest, weight="length")
                        val = dist_m / 1000.0
                except Exception:
                    continue

                if pd.isna(best_val) or val < best_val:
                    best_val = val
                    best_name = c["centre_name"]

            res[f"{ctype}_value"] = best_val
            res[f"{ctype}_nearest"] = best_name

        rows.append(res)

    out = pd.DataFrame(rows)
    out.to_parquet(out_path, index=False)
    print(f"Saved routing: {out_path}")
    return out

import traceback

def main():
    print("=== STARTING STROKE ACCESS ANALYSIS ===")
    
    # Debug: Check bundled files
    for f, name in [(BOUNDARY_FILE, "Boundary File")]:
        if os.path.exists(f):
            size_mb = os.path.getsize(f) / (1024 * 1024)
            print(f"Checking {name}: Found at {f} ({size_mb:.2f} MB)")
        else:
            print(f"Checking {name}: NOT FOUND at {f}")

    print("=== Load geographies (boundaries + centroids) ===")
    geos_gdf = load_geos()
    geos_gdf = gpd.GeoDataFrame(geos_gdf, geometry=geos_gdf.geometry, crs=4326)

    print("=== Load + geocode stroke centres ===")
    centres = load_and_geocode_centres()

    # Routing per province
    all_routes = []
    for pr in RUN_PROVINCES:
        r = route_times_one_province(geos_gdf, centres, pr)
        if not r.empty:
            all_routes.append(r)

    if not all_routes:
        raise ValueError("No routing outputs created. Check RUN_PROVINCES and your boundaries PROVINCE_COL/PRUID values.")

    routes = pd.concat(all_routes, ignore_index=True)

    # Merge back to geos
    df = geos_gdf.copy()
    df["geo_id"] = df[GEO_ID_COL].apply(to_str)
    df = df.merge(routes, on="geo_id", how="left")

    # Bands + EVT flags
    for t in ["SRH", "PSC", "CSC"]:
        vcol = f"{t}_value"
        if vcol in df.columns:
            df[f"{t}_band"] = df[vcol].apply(lambda x: bandify(x, TIME_BANDS))

    if "CSC_value" in df.columns:
        df["CSC_gt_60"] = (df["CSC_value"] > 60).astype("Int64")
        df["CSC_gt_120"] = (df["CSC_value"] > 120).astype("Int64")

    # Save outputs (geometry removed for csv)
    out_df = pd.DataFrame(df.drop(columns=["geometry"], errors="ignore"))
    out_df.to_csv(OUT_ACCESS_METRICS_CSV, index=False)
    out_df.to_parquet(OUT_ACCESS_METRICS_PARQUET, index=False)

    print(f"\nDONE ✅")
    print(f"Saved: {OUT_ACCESS_METRICS_CSV}")
    print(f"Saved: {OUT_ACCESS_METRICS_PARQUET}")
    print(f"Graphs cached in: {GRAPH_CACHE_DIR}/")
    print(f"Routing cached in: {ROUTING_CACHE_DIR}/")
    print(f"Geocode cache: {GEOCODE_CACHE_CSV}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n\n!!! CRITICAL ERROR !!!")
        traceback.print_exc()
        print("\n")
    
    input("Press Enter to exit...")

