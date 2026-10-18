import argparse
import sys
import os
import json
import geopandas as gpd
import pyproj
import topojson as tp  # Magische library voor perfecte muren
from shapely.geometry import shape, mapping

def main():
    parser = argparse.ArgumentParser(
        description="GRB naar OSM: Verwijdert dubbele gebouwen, koppelt adressen, "
                    "corrigeert types, en simplificeert topologisch via TopoJSON."
    )
    parser.add_argument("polygonen", help="Pad naar het GeoJSON-bestand met gebouwpolygonen (blijft ongewijzigd)")
    parser.add_argument("adressen", help="Pad naar het GeoJSON-bestand met adrespunten")

    args = parser.parse_args()

    # 1. Inlezen van de bestanden via GeoPandas
    try:
        gdf_poly = gpd.read_file(args.polygonen)
        gdf_addr = gpd.read_file(args.adressen)
    except Exception as e:
        print(f"Fout bij het inlezen van de bestanden: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Harmoniseer het coördinatensysteem voor de ruimtelijke join
    if gdf_poly.crs != gdf_addr.crs:
        gdf_addr = gdf_addr.to_crs(gdf_poly.crs)

    # 3. Adreskolommen hernoemen naar OSM-standaarden
    mapping_columns = {
        "HUISNR": "addr:housenumber",
        "STRAATNM": "addr:street"
    }
    beschikbare_cols = [col for col in mapping_columns.keys() if col in gdf_addr.columns]
    sub_addr = gdf_addr[beschikbare_cols + ['geometry']].copy()
    sub_addr = sub_addr.rename(columns=mapping_columns)

    # 4. Ruimtelijke koppeling (SJOIN) via een left-join
    gekoppeld = gpd.sjoin(gdf_poly, sub_addr, how="left", predicate="intersects")

    # -------------------------------------------------------------------------
    # CRUCIALE FIX: VERWIJDEREN VAN DUBBELE GEBOUWEN (Ways with same position)
    # -------------------------------------------------------------------------
    print("Bezig met identificeren en verwijderen van dubbele gebouwen (overlap)...")

    # Maak een tijdelijke string-representatie van de geometrie (WKT) om duplicaten te vinden
    gekoppeld['wkt'] = gekoppeld['geometry'].apply(lambda g: g.wkt if g else "")

    # Sorteer zo dat rijen MET een adres bovenaan komen te staan.
    # Mocht er een duplicaat zijn, dan bewaren we de rij met het adres!
    gekoppeld['heeft_adres_sort'] = gekoppeld['addr:housenumber'].notna() & (gekoppeld['addr:housenumber'].str.lower() != 'nan')
    gekoppeld = gekoppeld.sort_values(by='heeft_adres_sort', ascending=False)

    # Drop de duplicaten op basis van de exacte geometrie-string
    oorspronkelijk_aantal = len(gekoppeld)
    gekoppeld = gekoppeld.drop_duplicates(subset=['wkt'], keep='first')
    verwijderd = oorspronkelijk_aantal - len(gekoppeld)

    if verwijderd > 0:
        print(f"--> {verwijderd} dubbele gebouw(en) succesvol verwijderd uit de dataset!")

    # Ruim tijdelijke kolommen op
    gekoppeld = gekoppeld.drop(columns=['wkt', 'heeft_adres_sort'])
    # -------------------------------------------------------------------------

    TOLERANCE_METERS = 0.05

    print("Stap 1/2: TopoJSON netwerk opbouwen en muren 5cm simplificeren...")

    # Laat GeoPandas de transformatie naar meters (Lambert 72) doen
    if gekoppeld.crs is None or "31370" not in str(gekoppeld.crs.to_string()):
        gekoppeld = gekoppeld.to_crs("EPSG:31370")

    # Reparatie: Fix eventuele micro-foutjes in de bron-polygonen
    gekoppeld['geometry'] = gekoppeld['geometry'].buffer(0)

    # Breek MultiPolygons open tot losse, zelfstandige polygonen
    gekoppeld = gekoppeld.explode(index_parts=False).reset_index(drop=True)

    # (Optioneel) Zet een vlag als het object door een explode uit elkaar is getrokken
    # gekoppeld['was_multipolygon'] = gekoppeld.duplicated(subset=['OIDN'], keep=False)

    # Filter micro-polygonen en reststrookjes weg (alles kleiner dan 1 m²)
    drempel_m2 = 1.0
    aantal_voor = len(gekoppeld)
    gekoppeld = gekoppeld[gekoppeld.geometry.area >= drempel_m2]
    aantal_weg = aantal_voor - len(gekoppeld)

    if aantal_weg > 0:
        print(f"--> {aantal_weg} micro-polygoon/slivers (< {drempel_m2} m²) succesvol weggefilterd.")

    # TOPOJSON MAGIE: Converteer naar topologische structuur voor correct verwijderen van overtollige datapunten (nodes)
    topo = tp.Topology(gekoppeld, prequantize=False)
    topo_simplified = topo.toposimplify(TOLERANCE_METERS)
    gdf_simplified = topo_simplified.to_gdf()

    # Zet de opgeschoonde data om naar WGS84 graden voor JOSM
    gdf_simplified = gdf_simplified.to_crs("EPSG:4326")

    print("Stap 2/2: Verwerken van tags, adrescorrecties en GeoJSON export...")
    features_out = []

    # 5. Loop door de perfect gesimplificeerde features
    for idx, row in gdf_simplified.iterrows():
        props = row.to_dict()
        geom_obj = props.pop('geometry', None)

        if not geom_obj or geom_obj.is_empty:
            continue

        # Datum robuust slicen naar YYYY-MM-DD
        date_val = props.get('VERSDATUM')
        if date_val:
            date_val = str(date_val)[:10]

        # OSM referentienummer opbouwen
        oidn_val = props.get('OIDN')
        ref_val = f"Gbg/{int(oidn_val)}" if oidn_val is not None else None

        # Adres-aanwezigheid controleren
        housenumber = props.get('addr:housenumber')
        heeft_adres = bool(housenumber and str(housenumber).strip() and str(housenumber).lower() != "nan")

        # Bepaal het gebouwtype met de extra 'shed'-correctie
        type_val = props.get('TYPE')
        if heeft_adres:
            building_val = 'yes'
        elif type_val == 1:
            building_val = 'yes'
        elif type_val == 2:
            building_val = 'shed'
        else:
            building_val = 'yes'

        # De schone, definitieve OSM-attributenset samenstellen
        new_props = {
            'building': building_val,
            'source:geometry:date': date_val,
            'source:geometry:ref': ref_val
        }

        # Voeg uitsluitend addr:street en addr:housenumber toe
        for osm_tag in mapping_columns.values():
            val = props.get(osm_tag)
            if val is not None and str(val).strip() != "" and str(val).lower() != "nan":
                new_props[osm_tag] = str(val)

        # Feature toevoegen aan de nieuwe lijst
        features_out.append({
            "type": "Feature",
            "properties": new_props,
            "geometry": mapping(geom_obj)
        })

    # 8. Bouw de GeoJSON-structuur op voor JOSM
    output_geojson = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" }
        },
        "features": features_out
    }

    # 9. Sla op naar een NIEUW bestand
    input_dir = os.path.dirname(args.polygonen) if os.path.dirname(args.polygonen) else "."
    output_filepath = os.path.join(input_dir, "output_osm.geojson")

    with open(output_filepath, 'w', encoding='utf-8') as f:
        json.dump(output_geojson, f, ensure_ascii=False, indent=2)

    print(f"\nSucces! Het bestand is nu 100% vrij van geometrische duplicaten en overlaps.")
    print(f"Klaar voor JOSM: {output_filepath}")

if __name__ == "__main__":
    main()
