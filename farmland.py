import argparse
import sys
import os
import json
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
import geopandas as gpd
import pyproj
import topojson as tp
from shapely.geometry import shape, mapping, Polygon, Point, box
from shapely.ops import unary_union

def lees_bbox_uit_osm(osm_pad):
    """Leest de bounding box coördinaten uit de header van een JOSM .osm-bestand."""
    try:
        tree = ET.parse(osm_pad)
        root = tree.getroot()
        bounds = root.find('bounds')
        if bounds is not None:
            minlat = float(bounds.get('minlat'))
            minlon = float(bounds.get('minlon'))
            maxlat = float(bounds.get('maxlat'))
            maxlon = float(bounds.get('maxlon'))
            return (minlon, minlat, maxlon, maxlat)
    except Exception as e:
        print(f"Fout bij het uitlezen van de bounds uit {osm_pad}: {e}", file=sys.stderr)
    raise ValueError(f"Geen geldige <bounds> tag gevonden in het OSM-bestand: {osm_pad}")

def get_existing_osm_polys_in_lambert72(osm_pad):
    """Parseert bestaande gebouwen uit het JOSM .osm-bestand en transformeert ze naar EPSG:31370."""
    try:
        tree = ET.parse(osm_pad)
        root = tree.getroot()

        nodes = {}
        for node in root.findall('node'):
            nid = node.get('id')
            lat = float(node.get('lat'))
            lon = float(node.get('lon'))
            nodes[nid] = (lon, lat)

        polys = []
        for way in root.findall('way'):
            is_building = False
            for tag in way.findall('tag'):
                if tag.get('k') == 'building':
                    is_building = True
                    break
            if not is_building:
                continue
            nds = [nd.get('ref') for nd in way.findall('nd')]
            coords = [nodes[ref] for ref in nds if ref in nodes]
            if len(coords) >= 4 and coords[0] == coords[-1]:
                polys.append(Polygon(coords))

        if not polys:
            return []

        gdf_osm = gpd.GeoDataFrame(geometry=polys, crs="EPSG:4326")
        gdf_osm = gdf_osm.to_crs("EPSG:31370")
        return [geom for geom in gdf_osm.geometry if geom and not geom.is_empty]
    except Exception as e:
        print(f"Waarschuwing bij inlezen bestaande OSM gebouwen: {e}", file=sys.stderr)
        return []

def get_existing_osm_addresses_in_lambert72(osm_pad):
    """
    Parseert alle bestaande OSM elementen die een 'addr:housenumber' tag bevatten
    uit het JOSM .osm-bestand en transformeert ze naar EPSG:31370.
    """
    try:
        tree = ET.parse(osm_pad)
        root = tree.getroot()

        nodes = {}
        for node in root.findall('node'):
            nid = node.get('id')
            lat = float(node.get('lat'))
            lon = float(node.get('lon'))
            nodes[nid] = (lon, lat)

        geoms = []

        for node in root.findall('node'):
            nid = node.get('id')
            if nid in nodes:
                tags = {tag.get('k'): tag.get('v') for tag in node.findall('tag')}
                if 'addr:housenumber' in tags:
                    pt = shape({"type": "Point", "coordinates": nodes[nid]})
                    geoms.append(pt)

        for way in root.findall('way'):
            tags = {tag.get('k'): tag.get('v') for tag in way.findall('tag')}
            if 'addr:housenumber' in tags:
                nds = [nd.get('ref') for nd in way.findall('nd') if nd.get('ref') in nodes]
                coords = [nodes[ref] for ref in nds]
                if len(coords) >= 2:
                    try:
                        if coords[0] == coords[-1] and len(coords) >= 4:
                            geoms.append(Polygon(coords))
                        else:
                            geoms.append(shape({"type": "LineString", "coordinates": coords}))
                    except Exception:
                        pass

        if not geoms:
            return []

        gdf_osm_addr = gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")
        gdf_osm_addr = gdf_osm_addr.to_crs("EPSG:31370")
        buffered_geoms = [geom.buffer(5.0) for geom in gdf_osm_addr.geometry if geom and not geom.is_empty]
        return buffered_geoms
    except Exception as e:
        print(f"Waarschuwing bij inlezen bestaande OSM adres-elementen: {e}", file=sys.stderr)
        return []

def filter_existing_gdf(gdf_grb, osm_polys_l72, threshold=0.85):
    """Filtert GRB-gebouwen weg die al volledig bedekt zijn door bestaande OSM-gebouwen."""
    if not osm_polys_l72 or gdf_grb.empty:
        return gdf_grb

    filtered_indices = []
    for idx, row in gdf_grb.iterrows():
        geom = row['geometry']
        if not geom or geom.is_empty:
            continue
        area = geom.area
        if area == 0:
            continue

        relevant_osm = [op for op in osm_polys_l72 if op.intersects(geom)]
        if not relevant_osm:
            filtered_indices.append(idx)
            continue

        local_union = unary_union(relevant_osm)
        intersection_area = geom.intersection(local_union).area
        coverage_ratio = intersection_area / area

        if coverage_ratio >= threshold:
            continue
        else:
            filtered_indices.append(idx)

    return gdf_grb.loc[filtered_indices].copy()

def haal_wfs_data_op(typename, bbox):
    """Haalt GeoJSON data op van de Vlaanderen WFS op basis van een bounding box."""
    base_url = "https://geo.api.vlaanderen.be/GRB/wfs"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typename": typename,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},EPSG:4326"
    }
    url = f"{base_url}?{urllib.parse.urlencode(params)}"

    print(f"Bezig met ophalen van laag '{typename}' via WFS...")
    try:
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode('utf-8'))
            return gpd.GeoDataFrame.from_features(data, crs="EPSG:4326")
    except Exception as e:
        print(f"Fout bij het ophalen van WFS laag {typename}: {e}", file=sys.stderr)
        sys.exit(1)

def sorteer_huisnummer(val):
    val_str = str(val).strip()
    import re
    match = re.search(r'\d+', val_str)
    if match:
        nummer_deel = int(match.group())
        rest_deel = val_str[match.end():].strip().lower()
        return (0, nummer_deel, rest_deel, val_str)
    else:
        return (1, 0, val_str.lower(), val_str)

def main():
    parser = argparse.ArgumentParser(
        description="Leest een JOSM .osm-bestand, haalt automatisch GRB-gebouwen en adressen op via WFS, "
                    "verwijderd dubbele gebouwen, koppelt adressen (met 1 samengevoegde adresnode per secundaire straat)."
    )
    parser.add_argument("osm_bestand", help="Pad naar het JOSM .osm-bestand (bijv. temp.osm)")
    parser.add_argument("-fno", action='store_true', help="Schakel filtering uit; importeer alle GRB-gebouwen zonder te controleren op bestaande OSM-data.")

    args = parser.parse_args()

    # Controle of het opgegeven invoerbestand daadwerkelijk bestaat
    if not os.path.exists(args.osm_bestand):
        print(f"Fout: Het bestand '{args.osm_bestand}' is niet gevonden.", file=sys.stderr)
        sys.exit(1)

    print(f"Bounding box uitlezen uit {args.osm_bestand}...")
    try:
        bbox = lees_bbox_uit_osm(args.osm_bestand)
        print(f"Gevonden BBOX (Lon/Lat): {bbox}")
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    gdf_poly = haal_wfs_data_op("GRB:GBG", bbox)
    gdf_addr = haal_wfs_data_op("GRB:Adres", bbox)
    if gdf_poly.empty:
        print("Geen gebouwen gevonden in de WFS voor deze bounding box.", file=sys.stderr)
        sys.exit(0)

    print(f"Succesvol {len(gdf_poly)} gebouwen en {len(gdf_addr)} adrespunten opgehaald via WFS.")

    if gdf_poly.crs != gdf_addr.crs:
        gdf_addr = gdf_addr.to_crs(gdf_poly.crs)

    mapping_columns = {}
    for col in ["HUISNR", "huisnummer", "Huisnummer"]:
        if col in gdf_addr.columns:
            mapping_columns[col] = "addr:housenumber"
    for col in ["STRAATNM", "straatnaam", "Straatnaam"]:
        if col in gdf_addr.columns:
            mapping_columns[col] = "addr:street"

    sub_addr = gdf_addr[[c for c in mapping_columns.keys()] + ['geometry']].copy()
    sub_addr = sub_addr.rename(columns=mapping_columns)

    print("Controleren op reeds bestaande OSM-adressen...")
    existing_osm_addresses = get_existing_osm_addresses_in_lambert72(args.osm_bestand)
    if existing_osm_addresses and not sub_addr.empty:
        sub_addr_l72 = sub_addr.to_crs("EPSG:31370")
        filtered_addr_indices = []
        for idx, row in sub_addr_l72.iterrows():
            pt = row['geometry']
            if not pt or pt.is_empty:
                continue
            reeds_aanwezig = any(buffered_poly.contains(pt) for buffered_poly in existing_osm_addresses)
            if not reeds_aanwezig:
                filtered_addr_indices.append(idx)

        aantal_overgeslagen = len(sub_addr) - len(filtered_addr_indices)
        if aantal_overgeslagen > 0:
            print(f"--> {aantal_overgeslagen} GRB-adrespunt(en) overgeslagen omdat OSM deze al bevat.")
        sub_addr = sub_addr.loc[filtered_addr_indices].copy()

    print("Bezig met slim koppelen en sorteren van adressen aan gebouwen...")
    extra_address_nodes = []

    if not sub_addr.empty and not gdf_poly.empty:
        proj_poly = gdf_poly.to_crs("EPSG:31370").copy()
        proj_poly['orig_index'] = proj_poly.index
        proj_addr = sub_addr.to_crs("EPSG:31370")

        intersect_join = gpd.sjoin(proj_addr, proj_poly, how="inner", predicate="intersects")
        matched_addr_indices = set(intersect_join.index)
        unmatched_addr = proj_addr.loc[~proj_addr.index.isin(matched_addr_indices)]

        nearest_join_results = []
        if not unmatched_addr.empty:
            nearest_join = gpd.sjoin_nearest(
                unmatched_addr,
                proj_poly,
                how="inner",
                distance_col="afstand_tot_gebouw",
                max_distance=4.0
            )
            nearest_join_results.append(nearest_join)

        all_matches = [intersect_join] + nearest_join_results
        combined_join = gpd.pd.concat(all_matches, ignore_index=True) if all_matches else intersect_join

        building_address_records = []
        if not combined_join.empty and 'orig_index' in combined_join.columns:
            for orig_idx, group in combined_join.groupby('orig_index'):
                straten_raw = [str(s).strip() for s in group['addr:street'].dropna() if str(s).strip() != "" and str(s).lower() != "nan"]

                if not straten_raw:
                    continue

                primair_straat = max(set(straten_raw), key=straten_raw.count)

                groep_primair = group[group['addr:street'] == primair_straat]
                huisnummers_primair = sorted(list({str(h).strip() for h in groep_primair['addr:housenumber'].dropna() if str(h).strip() != ""}), key=sorteer_huisnummer)

                building_address_records.append({
                    'orig_index': orig_idx,
                    'addr:street': primair_straat,
                    'addr:housenumber': "; ".join(huisnummers_primair) if huisnummers_primair else None
                })

                groep_secundair = group[group['addr:street'] != primair_straat]
                if not groep_secundair.empty:
                    for sec_straat, sec_group in groep_secundair.groupby('addr:street'):
                        sec_huisnummers = sorted(list({str(h).strip() for h in sec_group['addr:housenumber'].dropna() if str(h).strip() != ""}), key=sorteer_huisnummer)

                        sec_geom = sec_group['geometry'].iloc[0]

                        if sec_straat and sec_huisnummers and sec_geom and not sec_geom.is_empty:
                            extra_address_nodes.append({
                                'geometry': sec_geom,
                                'addr:street': str(sec_straat),
                                'addr:housenumber': "; ".join(sec_huisnummers)
                            })

        if building_address_records:
            df_addr_agg = gpd.GeoDataFrame(building_address_records).set_index('orig_index')
            gekoppeld = gdf_poly.join(df_addr_agg)
        else:
            gekoppeld = gdf_poly.copy()
            gekoppeld['addr:street'] = None
            gekoppeld['addr:housenumber'] = None
    else:
        gekoppeld = gdf_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None

    print("Bezig met identificeren en verwijderen van dubbele gebouwen (overlap)...")
    gekoppeld['wkt'] = gekoppeld['geometry'].apply(lambda g: g.wkt if g else "")
    oorspronkelijk_aantal = len(gekoppeld)
    gekoppeld = gekoppeld.drop_duplicates(subset=['wkt'], keep='first')
    verwijderd = oorspronkelijk_aantal - len(gekoppeld)
    if verwijderd > 0:
        print(f"--> {verwijderd} dubbel(e) gebouw(en) succesvol verwijderd uit de dataset!")

    if 'wkt' in gekoppeld.columns:
        gekoppeld = gekoppeld.drop(columns=['wkt'])

    TOLERANCE_METERS = 0.05
    print("Stap 1/2: TopoJSON netwerk opbouwen en muren 5cm simplificeren...")

    if gekoppeld.crs is None or "31370" not in str(gekoppeld.crs.to_string()):
        gekoppeld = gekoppeld.to_crs("EPSG:31370")

    gekoppeld['geometry'] = gekoppeld['geometry'].buffer(0)
    gekoppeld = gekoppeld.explode(index_parts=False).reset_index(drop=True)

    drempel_m2 = 1.0
    aantal_voor = len(gekoppeld)
    gekoppeld = gekoppeld[gekoppeld.geometry.area >= drempel_m2]
    aantal_weg = aantal_voor - len(gekoppeld)
    if aantal_weg > 0:
        print(f"--> {aantal_weg} micro-polygoon/slivers (< {drempel_m2} m²) succesvol weggefilterd.")

    topo = tp.Topology(gekoppeld, prequantize=False)
    topo_simplified = topo.toposimplify(TOLERANCE_METERS)
    gdf_simplified = topo_simplified.to_gdf()

    box_wgs84 = box(bbox[0], bbox[1], bbox[2], bbox[3])
    box_gdf = gpd.GeoDataFrame(geometry=[box_wgs84], crs="EPSG:4326").to_crs("EPSG:31370")
    bbox_poly_l72 = box_gdf.geometry.iloc[0]

    aantal_voor_bbox = len(gdf_simplified)
    gdf_simplified = gdf_simplified[gdf_simplified.geometry.intersects(bbox_poly_l72)].copy()
    aantal_buiten = aantal_voor_bbox - len(gdf_simplified)
    if aantal_buiten > 0:
        print(f"--> {aantal_buiten} gebouw(en) buiten de bounding box weggefilterd.")

    if not args.fno:
        print("Bezig met filteren van reeds bestaande OSM-gebouwen...")
        existing_osm_polys = get_existing_osm_polys_in_lambert72(args.osm_bestand)
        aantal_voor_filter = len(gdf_simplified)
        gdf_simplified = filter_existing_gdf(gdf_simplified, existing_osm_polys)
        aantal_gefilterd = aantal_voor_filter - len(gdf_simplified)
        if aantal_gefilterd > 0:
            print(f"--> {aantal_gefilterd} reeds bestaand(e) gebouw(en) weggefilterd.")
    else:
        print("Filter uitgeschakeld (-fno): alle GRB-gebouwen worden meegenomen.")

    gdf_simplified = gdf_simplified.to_crs("EPSG:4326")

    print("Stap 2/2: Verwerken van tags, adrescorrecties en GeoJSON export...")
    features_out = []

    for idx, row in gdf_simplified.iterrows():
        props = row.to_dict()
        geom_obj = props.pop('geometry', None)

        if not geom_obj or geom_obj.is_empty:
            continue

        date_val = props.get('VERSDATUM') or props.get('begindatum')
        if date_val:
            date_val = str(date_val)[:10]

        oidn_val = props.get('OIDN') or props.get('id')
        ref_val = f"Gbg/{int(oidn_val)}" if oidn_val is not None and str(oidn_val).isdigit() else (f"Gbg/{oidn_val}" if oidn_val else None)

        housenumber = props.get('addr:housenumber')
        heeft_adres = bool(housenumber and str(housenumber).strip() and str(housenumber).lower() != "nan")

        building_val = 'house' if heeft_adres else 'yes'

        new_props = {
            'building': building_val,
        }
        if date_val:
            new_props['source:geometry:date'] = date_val
        if ref_val:
            new_props['source:geometry:ref'] = ref_val

        for osm_tag in ["addr:street", "addr:housenumber"]:
            val = props.get(osm_tag)
            if val is not None and str(val).strip() != "" and str(val).lower() != "nan":
                new_props[osm_tag] = str(val)

        features_out.append({
            "type": "Feature",
            "properties": new_props,
            "geometry": mapping(geom_obj)
        })

    if extra_address_nodes:
        print(f"--> {len(extra_address_nodes)} extra samengevoegde adresnode(s) aangemaakt voor secundaire straatnamen.")
        gdf_extra = gpd.GeoDataFrame(extra_address_nodes, crs="EPSG:31370").to_crs("EPSG:4326")
        for _, ext_row in gdf_extra.iterrows():
            ext_geom = ext_row['geometry']
            if not ext_geom or ext_geom.is_empty:
                continue
            features_out.append({
                "type": "Feature",
                "properties": {
                    "addr:street": ext_row['addr:street'],
                    "addr:housenumber": ext_row['addr:housenumber']
                },
                "geometry": mapping(ext_geom)
            })

    output_geojson = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": { "name": "urn:ogc:def:crs:OGC:1.3:CRS84" }
        },
        "features": features_out
    }

    # Dynamische naamgeving op basis van de naam van het invoerbestand (ipv hardcoded output_osm.geojson)
    input_dir = os.path.dirname(args.osm_bestand) if os.path.dirname(args.osm_bestand) else "."
    base_filename = os.path.splitext(os.path.basename(args.osm_bestand))[0]
    output_filepath = os.path.join(input_dir, f"{base_filename}_grb.geojson")

    with open(output_filepath, 'w', encoding='utf-8') as f:
        json.dump(output_geojson, f, ensure_ascii=False, indent=2)

    print(f"\nSucces! Het bestand is gegenereerd op basis van de bounding box uit {args.osm_bestand}.")
    print(f"Klaar voor JOSM: {output_filepath}")

    print("Bezig met openen in JOSM...")
    try:
        abs_output_path = os.path.abspath(output_filepath)
        josm_url = "http://127.0.0.1:8111/open_file"
        params = {"filename": abs_output_path}
        full_url = f"{josm_url}?{urllib.parse.urlencode(params)}"

        with urllib.request.urlopen(full_url) as response:
            if response.status == 200:
                print("Succes! Het bestand is geopend in JOSM.")
            else:
                print(f"Waarschuwing: JOSM reageerde met status {response.status}", file=sys.stderr)

    except urllib.error.HTTPError as e:
        error_message = e.read().decode('utf-8', errors='ignore')
        print(f"Kon JOSM Remote Control niet bereiken (HTTP {e.code}): {e.reason}", file=sys.stderr)
        if error_message:
            print(f"JOSM melding: {error_message}", file=sys.stderr)
    except Exception as e:
        print(f"Kon JOSM Remote Control niet bereiken: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
