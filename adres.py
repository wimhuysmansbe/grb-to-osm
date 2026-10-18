import argparse
import sys
import geopandas as gpd

def main():
    parser = argparse.ArgumentParser(description="Koppel OSM-adres tags aan polygonen, behoud alle gebouwen, en overschrijf het bronbestand.")
    parser.add_argument("polygonen", help="Pad naar het GeoJSON-bestand met polygonen (wordt overschreven)")
    parser.add_argument("adressen", help="Pad naar het GeoJSON-bestand met adressen")

    args = parser.parse_args()

    try:
        gdf_poly = gpd.read_file(args.polygonen)
        gdf_addr = gpd.read_file(args.adressen)
    except Exception as e:
        print(f"Fout bij het inlezen van de bestanden: {e}", file=sys.stderr)
        sys.exit(1)

    if gdf_poly.crs != gdf_addr.crs:
        gdf_addr = gdf_addr.to_crs(gdf_poly.crs)

    mapping_columns = {
        "HUISNR": "addr:housenumber",
        "STRAATNM": "addr:street",
        "POSTCODE": "addr:postcode",
        "GEMEENTE": "addr:city"
    }

    beschikbare_cols = [col for col in mapping_columns.keys() if col in gdf_addr.columns]
    sub_addr = gdf_addr[beschikbare_cols + ['geometry']].copy()
    sub_addr = sub_addr.rename(columns=mapping_columns)

    # Ruimtelijke koppeling met left join (behoudt alle polygonen)
    gekoppeld = gpd.sjoin(gdf_poly, sub_addr, how="left", predicate="intersects")

    if 'index_right' in gekoppeld.columns:
        gekoppeld = gekoppeld.drop(columns=['index_right'])

    # OPMERKING: We filteren NIET meer met dropna(), dus gebouwen zonder adres blijven bestaan.
    # Waardes van kolommen die geen adres hebben worden automatisch `None` (wat in GeoJSON/OSM resulteert in het weglaten van die tags).

    osm_tags = list(mapping_columns.values())
    behouden_kolommen = [col for col in gdf_poly.columns if col != 'geometry'] + [col for col in osm_tags if col in gekoppeld.columns] + ['geometry']

    gekoppeld = gekoppeld[[col for col in behouden_kolommen if col in gekoppeld.columns]]

    gekoppeld.to_file(args.polygonen, driver="GeoJSON")
    print(f"Succes! Alle gebouwen (met en zonder adres) zijn bewaard en het bestand is overschreven: {args.polygonen}")

if __name__ == "__main__":
    main()
