import urllib.parse
import urllib.request
import json
import sys
import geopandas as gpd

def haal_wfs_data_op(typename: str, bbox: tuple, base_url: str = "https://geo.api.vlaanderen.be/GRB/wfs") -> gpd.GeoDataFrame:
    """
    Haalt GeoJSON data op via WFS direct in Lambert 2008 (EPSG:3812).

    bbox: tuple van (minx, miny, maxx, maxy) in EPSG:3812 of EPSG:4326 afhankelijk van de serverconfiguratie.
    """
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typename": typename,
        "outputFormat": "application/json",
        "srsName": "EPSG:3812",  # Direct Lambert 2008 opvragen
        "bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},EPSG:3812"
    }

    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    print(f"Ophalen laag '{typename}' via WFS (EPSG:3812)...")

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Python-GIS-Script'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))

            if not data.get("features"):
                return gpd.GeoDataFrame(geometry=[], crs="EPSG:3812")

            # Direct inlezen als Lambert 2008, geen reprojectie nodig
            gdf = gpd.GeoDataFrame.from_features(data, crs="EPSG:3812")
            return gdf

    except Exception as e:
        print(f"Fout bij ophalen van {typename}: {e}", file=sys.stderr)
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:3812")
