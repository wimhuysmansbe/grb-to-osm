import json
import sys
import os
from shapely.geometry import shape, mapping
from shapely.ops import transform
import pyproj

def process_geojson(filepath):
    if not os.path.exists(filepath):
        print(f"Bestand niet gevonden: {filepath}")
        return

    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 1. Automatische CRS (coördinatensysteem) detectie
    crs_property = data.get('crs', {}).get('properties', {}).get('name', '')
    is_lambert = "31370" in crs_property

    # Initialiseer pyproj transformers als we van Lambert 72 naar WGS84 moeten transformeren
    if is_lambert:
        print("Lambert 72 (EPSG:31370) gedetecteerd. Geometrie wordt getransformeerd naar WGS84...")
        crs_from = pyproj.CRS('EPSG:31370')
        crs_to = pyproj.CRS('EPSG:4326')
        # always_xy=True zorgt dat longitude/latitude (x/y) in de juiste volgorde blijven voor GeoJSON
        project_to_wgs84 = pyproj.Transformer.from_crs(crs_from, crs_to, always_xy=True).transform
    else:
        print("WGS84 (EPSG:4326) of onbekend gedetecteerd. Geen initiële transformatie nodig.")

    # Tolerantie in meters (0.05 = 5 centimeter opschoning)
    TOLERANCE_METERS = 0.05

    for feature in data.get('features', []):
        props = feature.get('properties', {})
        geom_data = feature.get('geometry')

        if geom_data:
            geom_obj = shape(geom_data)

            if is_lambert:
                # A) Data is Lambert 72: We kunnen DIRECT simplificeren in meters!
                simplified_geom = geom_obj.simplify(TOLERANCE_METERS, preserve_topology=True)
                # Daarna transformeren we de opgeschoonde meters naar WGS84 graden voor JOSM
                final_geom = transform(project_to_wgs84, simplified_geom)
            else:
                # B) Data is al WGS84: Om 5cm te simplificeren moeten we tijdelijk naar meters en terug
                crs_wgs84 = pyproj.CRS('EPSG:4326')
                crs_lambert = pyproj.CRS('EPSG:31370')
                to_meters = pyproj.Transformer.from_crs(crs_wgs84, crs_lambert, always_xy=True).transform
                to_degrees = pyproj.Transformer.from_crs(crs_lambert, crs_wgs84, always_xy=True).transform

                geom_meters = transform(to_meters, geom_obj)
                simplified_meters = geom_meters.simplify(TOLERANCE_METERS, preserve_topology=True)
                final_geom = transform(to_degrees, simplified_meters)

            feature['geometry'] = mapping(final_geom)

        # 2. Datum robuuster knippen met string-slicing
        date_val = props.get('VERSDATUM')
        if date_val:
            date_val = str(date_val)[:10]

        oidn_val = props.get('OIDN')
        type_val = props.get('TYPE')

        ref_val = f"Gbg/{int(oidn_val)}" if oidn_val is not None else None

        # Bepaal de building tag op basis van TYPE (1 = hoofdgebouw, 2 = bijgebouw)
        if type_val == 1:
            building_val = 'yes'
        elif type_val == 2:
            building_val = 'shed'
        else:
            building_val = 'yes'

        feature['properties'] = {
            'building': building_val,
            'source:geometry:date': date_val,
            'source:geometry:ref': ref_val
        }

    # 3. Werk de CRS-header in de GeoJSON bij naar WGS84 zodat JOSM het snapt
    if 'crs' in data:
        data['crs'] = {
            "type": "name",
            "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" }
        }

    # Overschrijf het originele bestand
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Succesvol verwerkt, omgezet naar WGS84 en overschreven: {filepath}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Gebruik: python transform.py <jouw_bestand.geojson>")
    else:
        process_geojson(sys.argv[1])
