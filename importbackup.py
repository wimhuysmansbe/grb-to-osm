import argparse
import sys
import os
import json
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
import geopandas as gpd
import pandas as pd
import pyproj
import topojson as tp
import numpy as np
import logging
import re
import csv
import shapely
from shapely.geometry import shape, mapping, Polygon, Point, box, MultiPolygon, MultiLineString
from shapely.ops import unary_union, polygonize, linemerge, snap
from shapely import set_precision
from collections import Counter
from shapely.validation import explain_validity, make_valid
from pyproj import Transformer

logging.basicConfig(filename='adres_koppelingen.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# Vormt lange reeksen huisnummers om naar handelbaar formaat
def formatteer_huisnummers(huisnummers):
    if not huisnummers:
        return None

    pure_nums = []
    suffix_dict = {} # bijvoorbeeld: {2: ['A', 'B', 'C', 'D', 'E', 'F']}
    other_vals = []

    for h in huisnummers:
        h_str = str(h).strip()
        match = re.match(r'^(\d+)([A-Za-z]+)?$', h_str)
        if match:
            num = int(match.group(1))
            suffix = match.group(2)
            if not suffix:
                pure_nums.append(num)
            else:
                if num not in suffix_dict:
                    suffix_dict[num] = []
                suffix_dict[num].append(suffix.upper())
        else:
            other_vals.append(h_str)

    result_parts = []

    if pure_nums:
        num_ints = sorted(list(set(pure_nums)))
        if len(num_ints) > 2:
            diffs = [num_ints[i+1] - num_ints[i] for i in range(len(num_ints)-1)]
            if len(set(diffs)) == 1 and diffs[0] in (1, 2):
                result_parts.append(f"{num_ints[0]}-{num_ints[-1]}")
            else:
                result_parts.append(";".join(map(str, num_ints)))
        elif len(num_ints) == 2:
            result_parts.append(f"{num_ints[0]};{num_ints[1]}")
        elif len(num_ints) == 1:
            result_parts.append(str(num_ints[0]))

    for num in sorted(suffix_dict.keys()):
        suffixes = sorted(list(set(suffix_dict[num])))
        if len(suffixes) > 1:
            is_seq = all(ord(suffixes[i+1]) - ord(suffixes[i]) == 1 for i in range(len(suffixes)-1))
            if is_seq:
                result_parts.append(f"{num}{suffixes[0]}-{num}{suffixes[-1]}")
            else:
                result_parts.append(";".join([f"{num}{s}" for s in suffixes]))
        else:
            result_parts.append(f"{num}{suffixes[0]}")

    if other_vals:
        result_parts.extend(other_vals)

    return "; ".join(result_parts) if result_parts else "; ".join(map(str, huisnummers))


def lees_bbox_uit_osm(osm_pad):
    """Leest de bounding box coördinaten in WGS84 uit een JOSM .osm-bestand."""
    try:
        tree = ET.parse(osm_pad)
        root = tree.getroot()
        bounds = root.find('bounds')
        if bounds is not None:
            minlat = float(bounds.get('minlat'))
            minlon = float(bounds.get('minlon'))
            maxlat = float(bounds.get('maxlat'))
            maxlon = float(bounds.get('maxlon'))

            print(f"Gelezen bbox voor '{osm_pad}':")
            print(f"  Min. lon/lat: ({minlon}, {minlat})")
            print(f"  Max. lon/lat: ({maxlon}, {maxlat})")
            print(f"  Volledige tuple: (minlon, minlat, maxlon, maxlat) = ({minlon}, {minlat}, {maxlon}, {maxlat})")

            return (minlon, minlat, maxlon, maxlat)
    except Exception as e:
        print(f"Fout bij het uitlezen van de bounds uit {osm_pad}: {e}", file=sys.stderr)
        raise ValueError(f"Geen geldige <bounds> tag gevonden in het OSM-bestand: {osm_pad}")

    raise ValueError(f"Geen geldige <bounds> tag gevonden in het OSM-bestand: {osm_pad}")


def get_existing_osm_polys_met_meta(osm_pad):
    """Parseert bestaande gebouwen en building:parts incl. timestamp en metadata uit het JOSM .osm-bestand en transformeert ze naar EPSG:31370."""
    try:
        tree = ET.parse(osm_pad)
        root = tree.getroot()

        nodes = {}
        for node in root.findall('node'):
            nid = node.get('id')
            lat = float(node.get('lat'))
            lon = float(node.get('lon'))
            nodes[nid] = (lon, lat)

        ways = {}
        way_meta = {}
        for way in root.findall('way'):
            wid = way.get('id')
            timestamp = way.get('timestamp')

            nds = [nd.get('ref') for nd in way.findall('nd') if nd.get('ref') in nodes]
            coords = [nodes[ref] for ref in nds]
            if len(coords) >= 2:
                ways[wid] = coords
                way_meta[wid] = timestamp

        polys_meta = []

        for way in root.findall('way'):
            wid = way.get('id')
            is_target = False
            way_tags = {tag.get('k'): tag.get('v') for tag in way.findall('tag')}
            for k in way_tags:
                # Accepteer zowel reguliere gebouwen als building:parts
                if k == 'building' or k == 'building:part' or k.startswith('building:part'):
                    is_target = True
                    break
            if not is_target:
                continue
            if wid in ways:
                coords = ways[wid]
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    try:
                        poly = Polygon(coords)
                        polys_meta.append({
                            'geometry': poly,
                            'timestamp': way_meta.get(wid),
                            'osm_id': wid,
                            'tags': way_tags
                        })
                    except Exception:
                        pass

        for rel in root.findall('relation'):
            rel_id = rel.get('id')
            rel_timestamp = rel.get('timestamp')
            tags = {tag.get('k'): tag.get('v') for tag in rel.findall('tag')}
            is_target_rel = False

            # Ook voor relaties (multipolygons / sites met building of building:part)
            if 'building' in tags or any(k.startswith('building') for k in tags) or tags.get('type') == 'multipolygon':
                if any(k.startswith('building') for k in tags):
                    is_target_rel = True

            if not is_target_rel:
                continue

            outer_linestrings = []
            for member in rel.findall('member'):
                if member.get('type') == 'way':
                    mref = member.get('ref')
                    if mref in ways:
                        line = shape({"type": "LineString", "coordinates": ways[mref]})
                        outer_linestrings.append(line)

            if outer_linestrings:
                try:
                    merged = linemerge(outer_linestrings)
                    for p in polygonize(merged):
                        if not p.is_empty:
                            polys_meta.append({
                                'geometry': p,
                                'timestamp': rel_timestamp,
                                'osm_id': rel_id,
                                'tags': tags
                            })
                except Exception:
                    for line in outer_linestrings:
                        coords = list(line.coords)
                        if len(coords) >= 4 and coords[0] == coords[-1]:
                            try:
                                polys_meta.append({
                                    'geometry': Polygon(coords),
                                    'timestamp': rel_timestamp,
                                    'osm_id': rel_id,
                                    'tags': tags
                                })
                            except Exception:
                                pass

        if not polys_meta:
            return []

        geoms = [item['geometry'] for item in polys_meta]
        timestamps = [item['timestamp'] for item in polys_meta]
        osm_ids = [item['osm_id'] for item in polys_meta]
        tags_list = [item['tags'] for item in polys_meta]

        gdf_osm = gpd.GeoDataFrame({
            'timestamp': timestamps,
            'osm_id': osm_ids,
            'tags': tags_list
        }, geometry=geoms, crs="EPSG:4326")

        gdf_osm = gdf_osm.to_crs("EPSG:31370")

        result = []
        for _, row in gdf_osm.iterrows():
            geom = row['geometry']
            if geom and not geom.is_empty:
                result.append({
                    'geom': geom,
                    'timestamp': row['timestamp'],
                    'osm_id': row['osm_id'],
                    'tags': row['tags']
                })
        return result

    except Exception as e:
        print(f"Waarschuwing bij inlezen bestaande OSM gebouwen en parts: {e}", file=sys.stderr)
        return []

def get_existing_osm_farmyards_in_wgs84(osm_pad):
    """Parseert bestaande landuse=farmyard polygonen uit het JOSM .osm-bestand in native EPSG:4326."""
    try:
        tree = ET.parse(osm_pad)
        root = tree.getroot()

        nodes = {}
        for node in root.findall('node'):
            nid = node.get('id')
            lat = float(node.get('lat'))
            lon = float(node.get('lon'))
            nodes[nid] = (lon, lat)

        farmyard_polys = []
        for way in root.findall('way'):
            is_farmyard = False
            for tag in way.findall('tag'):
                if tag.get('k') == 'landuse' and tag.get('v') == 'farmyard':
                    is_farmyard = True
                    break
            if not is_farmyard:
                continue
            nds = [nd.get('ref') for nd in way.findall('nd')]
            coords = [nodes[ref] for ref in nds if ref in nodes]
            if len(coords) >= 4 and coords[0] == coords[-1]:
                farmyard_polys.append(Polygon(coords))

        if not farmyard_polys:
            return []

        gdf_farmyard = gpd.GeoDataFrame(geometry=farmyard_polys, crs="EPSG:4326")
        return [geom for geom in gdf_farmyard.geometry if geom and not geom.is_empty]

    except Exception as e:
        print(f"Waarschuwing bij inlezen bestaande landuse=farmyard: {e}", file=sys.stderr)
        return []


def get_existing_osm_addresses_in_lambert72(osm_pad):
    """Parseert alle bestaande OSM elementen die een 'addr:housenumber' tag bevatten."""
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


def filter_existing_gdf(gdf_grb, osm_polys_met_meta, threshold=0.85):
    """Filtert GRB-gebouwen weg op basis van de beste individuele OSM match (gebouw of building:part),
    tenzij het GRB-gebouw recenter is dan de bestaande OSM-intekening."""
    if not osm_polys_met_meta or gdf_grb.empty:
        return gdf_grb

    osm_records = [{'geometry': op['geom'], 'timestamp': op.get('timestamp')} for op in osm_polys_met_meta]
    osm_gdf = gpd.GeoDataFrame(osm_records, crs="EPSG:4326")

    filtered_indices = []
    for idx, row in gdf_grb.iterrows():
        geom = row['geometry']
        if not geom or geom.is_empty:
            continue
        grb_area = geom.area
        if grb_area == 0:
            continue

        grb_datum_str = row.get('VERSDATUM') or row.get('begindatum')
        grb_datum = pd.to_datetime(grb_datum_str, errors='coerce')

        possible_matches_idx = list(osm_gdf.sindex.query(geom, predicate="intersects"))

        relevant_osm = []
        for p_idx in possible_matches_idx:
            match_row = osm_gdf.iloc[p_idx]
            if match_row['geometry'].intersects(geom):
                relevant_osm.append((match_row['geometry'], match_row['timestamp']))

        if not relevant_osm:
            filtered_indices.append(idx)
            continue

        # Zoek het OSM-object dat qua oppervlakte het dichtst bij het GRB-element ligt (kleinste verschil)
        best_diff = float('inf')
        best_osm_area = 0.0
        beste_osm_datum = None
        best_coverage_grb = 0.0

        for match_geom, match_ts in relevant_osm:
            match_area = match_geom.area
            if match_area == 0:
                continue

            intersection_area = geom.intersection(match_geom).area
            if intersection_area == 0:
                continue

            coverage = intersection_area / grb_area
            # Controleer of het wel degelijk over de drempel valt (bijv. minstens 85% overlap)
            if coverage < threshold:
                continue

            # Het verschil in oppervlakte ten opzichte van het GRB-deel
            diff = abs(match_area - grb_area)

            if diff < best_diff:
                best_diff = diff
                best_osm_area = match_area
                best_coverage_grb = coverage

                if match_ts is not None:
                    dt = pd.to_datetime(match_ts, errors='coerce')
                    if pd.notna(dt):
                        if dt.tzinfo is not None:
                            dt = dt.tz_localize(None)
                        beste_osm_datum = dt

        # Als geen enkel object aan de drempel volgt, is het nieuw/afwijkend
        if best_coverage_grb < threshold:
            filtered_indices.append(idx)
            continue
        # Als zelfs de beste individuele match onder de drempel duikt, is het nieuw/afwijkend
        if best_coverage_grb < threshold:
            filtered_indices.append(idx)
            continue

        verschil_oppervlakte = abs(grb_area - best_osm_area) / best_osm_area if best_osm_area > 0 else 1.0
        is_nagenoeg_gelijk = verschil_oppervlakte < 0.02

        grb_is_recenter = False
        if pd.notna(grb_datum) and pd.notna(beste_osm_datum):
            grb_is_recenter = grb_datum > beste_osm_datum

        grb_is_kleiner = grb_area < best_osm_area

        if is_nagenoeg_gelijk:
            if pd.notna(grb_datum) and pd.notna(beste_osm_datum) and grb_is_recenter:
                filtered_indices.append(idx)
            else:
                continue
        else:
            if beste_osm_datum is not None and pd.notna(grb_datum) and not grb_is_recenter and not grb_is_kleiner:
                continue
            else:
                filtered_indices.append(idx)

    return gdf_grb.loc[filtered_indices].copy()


def exporteer_verouderde_osm_zoekstring(osm_polys_met_meta, gdf_grb, output_path="te_verwijderen_zoekstring.txt", max_overlap_ratio=0.10, min_leeftijd_jaar=3):
    if not osm_polys_met_meta or gdf_grb.empty:
        print("Geen data om te controleren.")
        return

    TARGET_CRS = "EPSG:31370"

    # 1. Haal geldige OSM-gebouwen op inclusief timestamp
    osm_records = []
    for op in osm_polys_met_meta:
        tags = op.get('tags', {})
        if 'building' not in tags:
            continue

        geom = op.get('geom') or op.get('geometry')
        if geom is None or geom.is_empty:
            continue

        if not geom.is_valid:
            geom = make_valid(geom)

        if geom.is_empty:
            continue

        osm_records.append({
            'geometry': geom,
            'osm_id': op.get('osm_id'),
            'timestamp': op.get('timestamp')
        })

    if not osm_records:
        print("Geen geldige OSM-gebouwen gevonden om te controleren.")
        return

    osm_gdf = gpd.GeoDataFrame(osm_records, crs=TARGET_CRS)

    # Zorg dat GRB in Lambert 72 staat
    if gdf_grb.crs != TARGET_CRS:
        gdf_grb = gdf_grb.to_crs(TARGET_CRS)

    # 2. Bouw één globale GRB-unie
    with np.errstate(all='ignore'):
        grb_union = unary_union([g for g in gdf_grb.geometry if g and not g.is_empty])

    print(f"Bezig met analyseren van {len(osm_gdf)} geldige OSM-gebouwen (inclusief leeftijdscheck >= {min_leeftijd_jaar} jaar)...")

    # 3. Vectoriale berekening van oppervlakte en overlap
    osm_gdf['area'] = osm_gdf.geometry.area
    osm_gdf = osm_gdf[osm_gdf['area'] > 0].copy()
    if osm_gdf.empty:
        print("Geen OSM-gebouwen met geldige oppervlakte over.")
        return

    intersections = osm_gdf.geometry.intersection(grb_union)
    osm_gdf['overlap_ratio'] = intersections.area / osm_gdf['area']

    # 4. Leeftijdscheck toepassen (bescherm recente intekeningen)
    huidige_tijd = pd.Timestamp.now()
    leeftijd_grens_dagen = min_leeftijd_jaar * 365.25

    def is_oud_genoeg(ts_val):
        if pd.isna(ts_val):
            return True  # Geen timestamp bekend? Dan beschouwen we het niet als recent gegarandeerd vers
        ts = pd.to_datetime(ts_val, errors='coerce')
        if pd.isna(ts):
            return True
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        leeftijd_dagen = (huidige_tijd - ts).days
        return leeftijd_dagen >= leeftijd_grens_dagen

    # Conditie 1: Lage overlap (< 10%)
    laag_overlap = osm_gdf['overlap_ratio'] < max_overlap_ratio

    # Conditie 2: Oud genoeg (of geen timestamp bekend)
    oud_genoeg = osm_gdf['timestamp'].apply(is_oud_genoeg)

    # Alleen panden die zowel weinig overlap hebben ALS oud genoeg zijn worden geselecteerd
    wees_panden = osm_gdf[laag_overlap & oud_genoeg]

    # 5. Schrijf weg naar de JOSM zoekstring
    ids_schoon = []
    for oid in wees_panden['osm_id'].dropna():
        oid_str = "".join(filter(str.isdigit, str(oid)))
        if oid_str:
            ids_schoon.append(oid_str)

    if ids_schoon:
        ids_schoon = list(dict.fromkeys(ids_schoon))
        zoek_string = "type:way (" + " or ".join([f"id:{oid}" for oid in ids_schoon]) + ")"
        with open(output_path, "w") as f:
            f.write(zoek_string)
        print(f"Klaar! JOSM-zoekstring voor {len(ids_schoon)} verouderde wees-panden opgeslagen in '{output_path}'.")
    else:
        print("Geen verouderde wees-panden gevonden die aan de criteria (inclusief leeftijd) voldoen.")

def haal_wfs_data_op(typename, bbox):
    """Haalt GeoJSON data op via WFS in WGS84 en zet het direct om naar Lambert 72."""
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

            if not data.get("features"):
                return gpd.GeoDataFrame(geometry=[], crs="EPSG:31370")

            gdf = gpd.GeoDataFrame.from_features(data, crs="EPSG:4326")
            return gdf.to_crs("EPSG:31370")

    except Exception as e:
        print(f"Fout bij het ophalen van laag {typename} via WFS: {e}", file=sys.stderr)
        sys.exit(1)


def sorteer_huisnummer(val):
    val_str = str(val).strip()
    match = re.search(r'\d+', val_str)
    if match:
        nummer_deel = int(match.group())
        rest_deel = val_str[match.end():].strip().lower()
        return (0, nummer_deel, rest_deel, val_str)
    else:
        return (1, 0, val_str.lower(), val_str)


def force_shared_nodes_geometries(gdf):
    """Breekt polygonen open in lijnen, knoopt ze aan elkaar en bouwt ze op."""
    boundaries = gdf.geometry.boundary.tolist()
    merged_lines = unary_union(MultiLineString(boundaries))
    fixed_polys = list(polygonize(merged_lines))
    gdf_fixed = gpd.GeoDataFrame(geometry=fixed_polys, crs=gdf.crs)
    return gdf_fixed

def koppel_adressen_aan_gbg(gdf_poly, sub_addr, gdf_wsm, existing_osm_addresses, filter_osmadressen=False):
    """
    Koppelt adrespunten uitsluitend aan de hoofdgebouwen (GBG) via intersectie
    en nabijheid, en filtert complexe of dubbele adressen uit.
    """
    extra_address_nodes = []

    if sub_addr.empty or gdf_poly.empty:
        gekoppeld = gdf_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None
        return gekoppeld, extra_address_nodes

    proj_poly = gdf_poly.copy()
    proj_poly['orig_index'] = proj_poly.index
    proj_addr = sub_addr.copy()
    proj_wsm = gdf_wsm.copy()

    intersect_join = gpd.sjoin(proj_addr, proj_poly, how="inner", predicate="intersects")

    buildings_with_direct = set(intersect_join['orig_index']) if not intersect_join.empty else set()
    matched_addr_indices = set(intersect_join.index) if not intersect_join.empty else set()

    unmatched_addr = proj_addr.loc[~proj_addr.index.isin(matched_addr_indices)].copy()

    # Alleen filteren tegen bestaande OSM adressen als dat expliciet aan staat
    if filter_osmadressen and existing_osm_addresses and not unmatched_addr.empty:
        filtered_unmatched_indices = []
        for idx, row in unmatched_addr.iterrows():
            pt = row['geometry']
            if not pt or pt.is_empty:
                continue
            reeds_aanwezig = any(buffered_poly.contains(pt) for buffered_poly in existing_osm_addresses)
            if not reeds_aanwezig:
                filtered_unmatched_indices.append(idx)
        unmatched_addr = unmatched_addr.loc[filtered_unmatched_indices].copy()

    buildings_without_direct = proj_poly.loc[~proj_poly['orig_index'].isin(buildings_with_direct)]

    nearest_join = None
    if not unmatched_addr.empty and not buildings_without_direct.empty:
        nearest_join = gpd.sjoin_nearest(
            unmatched_addr,
            buildings_without_direct,
            how="inner",
            distance_col="afstand_tot_gebouw",
            max_distance=4.0
        )

    all_matches = []
    if not intersect_join.empty:
        all_matches.append(intersect_join)
    if nearest_join is not None and not nearest_join.empty:
        all_matches.append(nearest_join)

    combined_join = gpd.pd.concat(all_matches, ignore_index=True) if all_matches else gpd.GeoDataFrame()

    if combined_join.empty or 'orig_index' not in combined_join.columns:
        gekoppeld = proj_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None
        return gekoppeld, extra_address_nodes

    adres_naar_gebouwen = {}
    for _, row in combined_join.iterrows():
        orig_idx = row['orig_index']
        straat = str(row.get('addr:street', '')).strip().lower()
        huisnr = str(row.get('addr:housenumber', '')).strip().lower()
        if straat and huisnr and straat != 'nan' and huisnr != 'nan':
            key = (straat, huisnr)
            if key not in adres_naar_gebouwen:
                adres_naar_gebouwen[key] = set()
            adres_naar_gebouwen[key].add(orig_idx)

    dubbele_adressen = {k: v for k, v in adres_naar_gebouwen.items() if len(v) > 1}
    indices_to_drop = set()

    if dubbele_adressen and proj_wsm is not None and not proj_wsm.empty:
        straat_kolom = 'LSTRNM' if 'LSTRNM' in proj_wsm.columns else next((col for col in ['STRAATNM', 'straatnaam', 'Straatnaam', 'NAAM', 'naam'] if col in proj_wsm.columns), None)

        for (straat, huisnr), gebouw_indices in dubbele_adressen.items():
            sub_gebouwen = proj_poly.loc[proj_poly['orig_index'].isin(gebouw_indices)]

            specifieke_segmenten = proj_wsm
            if straat_kolom:
                mask = proj_wsm[straat_kolom].astype(str).str.strip().str.lower() == straat
                gefilterd = proj_wsm[mask]
                if not gefilterd.empty:
                    specifieke_segmenten = gefilterd

            gebouw_afstanden = {}
            for g_idx, g_row in sub_gebouwen.iterrows():
                g_geom = g_row['geometry']
                if g_geom and not g_geom.is_empty:
                    gebouw_afstanden[g_idx] = specifieke_segmenten.geometry.distance(g_geom).min()

            if gebouw_afstanden:
                beste_g_idx = min(gebouw_afstanden, key=gebouw_afstanden.get)

                for g_idx in gebouw_afstanden:
                    if g_idx != beste_g_idx:
                        foute_rows = combined_join[
                            (combined_join['orig_index'] == g_idx) &
                            (combined_join['addr:street'].str.strip().str.lower() == straat) &
                            (combined_join['addr:housenumber'].str.strip().str.lower() == huisnr)
                        ]
                        indices_to_drop.update(foute_rows.index)

    if indices_to_drop:
        combined_join = combined_join.drop(index=list(indices_to_drop)).reset_index(drop=True)

    df_addr_records = []
    for orig_index, group in combined_join.groupby('orig_index'):
        straten_raw = [str(s).strip() for s in group['addr:street'].dropna() if str(s).strip() != "" and str(s).lower() != "nan"]
        if not straten_raw:
            continue
        primair_straat = max(set(straten_raw), key=straten_raw.count)
        groep_primair = group[group['addr:street'] == primair_straat]
        huisnummers_primair = sorted(list({str(h).strip() for h in groep_primair['addr:housenumber'].dropna() if str(h).strip() != ""}), key=sorteer_huisnummer)

        is_complex = False
        if len(huisnummers_primair) > 1:
            try:
                nummers_int = [int(re.search(r'\d+', h).group()) for h in huisnummers_primair if re.search(r'\d+', h)]
                if len(nummers_int) == len(huisnummers_primair):
                    nummers_int.sort()
                    verschillen = [nummers_int[i+1] - nummers_int[i] for i in range(len(nummers_int)-1)]
                    if max(verschillen) > 2:
                        is_complex = True
                else:
                    is_complex = True
            except Exception:
                is_complex = True

        if is_complex:
            for _, row_item in groep_primair.iterrows():
                extra_address_nodes.append({
                    'geometry': row_item['geometry'],
                    'addr:street': row_item['addr:street'],
                    'addr:housenumber': row_item['addr:housenumber'],
                    'fixme': 'Complexe/samengevoegde situaties: automatisch omgezet naar los adrespunt.'
                })
            continue

        geformatteerde_nummers = formatteer_huisnummers(huisnummers_primair)

        # --- LOGGING TOEVOEGEN ---
        logging.info(f"Adres gekoppeld -> Gebouw index {orig_index}: {primair_straat} {geformatteerde_nummers}")
        # Of als je het liever in een aparte lijst opslaat om later weg te schrijven:
        # log_lijst.append({'gebouw_index': orig_index, 'straat': primair_straat, 'huisnummer': geformatteerde_nummers})
        # -------------------------

        df_addr_records.append({
            'orig_index': orig_index,
            'addr:street': primair_straat,
            'addr:housenumber': geformatteerde_nummers
        })

    if df_addr_records:
        df_addr_agg = pd.DataFrame(df_addr_records).set_index('orig_index')
        gekoppeld = proj_poly.join(df_addr_agg, on='orig_index')
    else:
        gekoppeld = proj_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None

    return gekoppeld, extra_address_nodes

def verwerk_gba_attributen(props):
    """
    Vertaalt de GRB GBA (gebouwaanhorigheid) attributen naar de juiste OSM tags.
    - Type 2: Afdak -> building = roof
    - Type 3: Loopbrug -> building:part = corridor, min_level = 1, levels = 1
    - Type 1 : Standaard verdieping/uitbouw -> building:part = yes, min_level = 1, levels = 1
    - Type 4 : Standaard massieve trap -> building:part = steps
    - Overige : building:part = yes
    """
    new_props = {}
    gba_type = props.get('TYPE')

    try:
        gba_type_int = int(gba_type) if gba_type is not None else None
    except (ValueError, TypeError):
        gba_type_int = None

    if gba_type_int == 2:
        # Afdak
        new_props['building'] = 'roof'
    elif gba_type_int == 3:
        # Loopbrug / Corridor (hangt doorgaans boven de grond)
        new_props['building:part'] = 'corridor'
        new_props['building:min_level'] = '1'
        new_props['building:levels'] = '2'
    elif gba_type_int == 1:
        # Verdieping / uitbouw op poten / zwevende laag
        new_props['building:part'] = 'yes'
        new_props['building:min_level'] = '1'
        new_props['building:levels'] = '2'
    elif gba_type_int == 4:
        # Aangebouwde trap, massief
        new_props['building:part'] = 'steps'
    else:
        # Standaard gebouwdeel
        new_props['building:part'] = 'yes'

    return new_props

def main():
    parser = argparse.ArgumentParser(
        description="Leest een JOSM .osm-bestand, haalt automatisch GRB-gebouwen en adressen op via WFS."
    )
    parser.add_argument("osm_bestand", help="Pad naar het JOSM .osm-bestand (bijv. temp.osm)")
    parser.add_argument("-fno", action='store_true', help="Schakel filtering uit.")

    args = parser.parse_args()

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
    gdf_poly['grb_layer'] = 'Gbg'
    gdf_gba  = haal_wfs_data_op("GRB:GBA", bbox)
    gdf_gba['grb_layer'] = 'Gba'
    gdf_addr = haal_wfs_data_op("GRB:Adres", bbox)
    gdf_wsm  = haal_wfs_data_op("GRB:Wegsegment", bbox)

    if gdf_poly.empty:
        print("Geen gebouwen gevonden in de WFS voor deze bounding box.", file=sys.stderr)
        sys.exit(0)

    print(f"Succesvol {len(gdf_poly)} gebouwen, {len(gdf_gba)} bijgebouwen, {len(gdf_addr)} adrespunten en {len(gdf_wsm)} wegsegmenten opgehaald via WFS.")

    if gdf_poly.crs != gdf_addr.crs:
        gdf_addr = gdf_addr.to_crs(gdf_poly.crs)
    if gdf_poly.crs != gdf_wsm.crs:
        gdf_wsm = gdf_wsm.to_crs(gdf_poly.crs)
    if gdf_poly.crs != gdf_gba.crs:
        gdf_gba = gdf_gba.to_crs(gdf_poly.crs)

    mapping_columns = {}
    for col in ["HUISNR", "huisnummer", "Huisnummer"]:
        if col in gdf_addr.columns:
            mapping_columns[col] = "addr:housenumber"
    for col in ["STRAATNM", "straatnaam", "Straatnaam"]:
        if col in gdf_addr.columns:
            mapping_columns[col] = "addr:street"

    sub_addr = gdf_addr[[c for c in mapping_columns.keys()] + ['geometry']].copy()
    sub_addr = sub_addr.rename(columns=mapping_columns)

    existing_osm_addresses = get_existing_osm_addresses_in_lambert72(args.osm_bestand)

    print("Bezig met slim koppelen en sorteren van adressen aan GBG gebouwen...")
    gekoppeld, extra_address_nodes = koppel_adressen_aan_gbg(
        gdf_poly=gdf_poly,
        sub_addr=sub_addr,
        gdf_wsm=gdf_wsm,
        existing_osm_addresses=existing_osm_addresses
    )

    # GBG en GBA samenvoegen
    # 1 GBA filteren
    # Verwijder ingezonken garagetoegangen (TYPE 6 in de GRB GBA-codering)
    # Converteer naar numeriek, ongeldige omzettingen worden NaN
    if not gdf_gba.empty and 'TYPE' in gdf_gba.columns:
        gdf_gba['TYPE'] = pd.to_numeric(gdf_gba['TYPE'], errors='coerce')
        print("Unieke types in GBA na filter:", gdf_gba['TYPE'].unique())

        # Pas de conditie aan op basis van of 'type' een int of string is in je GeoDataFrame
        aantal_voor = len(gdf_gba)
        gdf_gba = gdf_gba[gdf_gba['TYPE'] != 6]
        aantal_na = len(gdf_gba)

        print(f"GBA gefilterd: {aantal_na} over (er zijn er {aantal_voor - aantal_na} ingezonken garagetoegangen verwijderd).")

    # De twee collecties gelijktrekken
    if not gdf_gba.empty:
        # Zorg dat gdf_gba dezelfde kolommen heeft (inclusief lege adresvelden als ze die niet hebben)
        for col in ['addr:street', 'addr:housenumber']:
            if col not in gdf_gba.columns:
                gdf_gba[col] = None
        # En samenvoegen tot dezelfde 'gekoppeld' variabele
        gekoppeld = pd.concat([gekoppeld, gdf_gba], ignore_index=True)

    print("Bezig met identificeren en verwijderen van dubbele gebouwen (overlap)...")
    gekoppeld['wkt'] = gekoppeld['geometry'].apply(lambda g: g.wkt if g else "")
    oorspronkelijk_aantal = len(gekoppeld)
    gekoppeld = gekoppeld.drop_duplicates(subset=['wkt'], keep='first')
    verwijderd = oorspronkelijk_aantal - len(gekoppeld)
    if verwijderd > 0:
        print(f"--> {verwijderd} dubbel(e) gebouw(en) succesvol verwijderd uit de dataset!")

    if 'wkt' in gekoppeld.columns:
        gekoppeld = gekoppeld.drop(columns=['wkt'])

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

    box_wgs84 = box(bbox[0], bbox[1], bbox[2], bbox[3])
    box_gdf = gpd.GeoDataFrame(geometry=[box_wgs84], crs="EPSG:4326").to_crs("EPSG:31370")
    bbox_poly_l72 = box_gdf.geometry.iloc[0]

    aantal_voor_bbox = len(gekoppeld)
    gekoppeld = gekoppeld[gekoppeld.geometry.intersects(bbox_poly_l72)].copy()
    aantal_buiten = aantal_voor_bbox - len(gekoppeld)
    if aantal_buiten > 0:
        print(f"--> {aantal_buiten} gebouw(en) buiten de bounding box weggefilterd.")

    if not args.fno:
        print("Bezig met filteren van reeds bestaande OSM-gebouwen...")
        existing_osm_polys = get_existing_osm_polys_met_meta(args.osm_bestand)
        aantal_voor_filter = len(gekoppeld)
        gekoppeld = filter_existing_gdf(gekoppeld, existing_osm_polys)
        aantal_gefilterd = aantal_voor_filter - len(gekoppeld)
        if aantal_gefilterd > 0:
            print(f"--> {aantal_gefilterd} reeds bestaand(e) gebouw(en) weggefilterd.")
    else:
        print("Filter uitgeschakeld (-fno): alle GRB-gebouwen worden meegenomen.")
        existing_osm_polys = get_existing_osm_polys_met_meta(args.osm_bestand)

    gekoppeld.to_crs("EPSG:4326").to_file("debug_1_ruwe_polygonen.geojson", driver="GeoJSON")
    print("--> Dump 1 opgeslagen: debug_1_ruwe_polygonen.geojson")

    # Rond alle coördinaten af op 1 mm nauwkeurig (grid size = 0.001 meter)
    gekoppeld['geometry'] = gekoppeld['geometry'].apply(lambda geom: shapely.set_precision(geom, grid_size=0.001))

    print(f"Stap 1/2: TopoJSON netwerk opbouwen en simplificeren voor {len(gekoppeld)} overgebleven gebouw(en)...")
    TOLERANCE_METERS = 0.05

    sindex = gekoppeld.sindex
    connected_indices = set()

    for idx, geom in gekoppeld.geometry.items():
        if geom is None or geom.is_empty:
            continue
        possible_matches = list(sindex.query(geom, predicate="touches")) + list(sindex.query(geom, predicate="overlaps"))
        neighbors = [m for m in possible_matches if m != idx]
        if neighbors:
            connected_indices.add(idx)

    gdf_connected = gekoppeld.loc[list(connected_indices)].copy()
    gdf_isolated = gekoppeld.loc[~gekoppeld.index.isin(connected_indices)].copy()
    gdf_connected.to_crs("EPSG:4326").to_file("debug_2gekoppeld_voor_topojson.geojson", driver="GeoJSON")

    processed_parts = []

    total_bounds = gdf_connected.total_bounds

    if len(gdf_connected) == 0 or np.isnan(total_bounds[0]):
        langste_zijde = 0.0
        dynamische_prequantize = 1000
    else:
        minx, miny, maxx, maxy = total_bounds
        breedte = maxx - minx
        hoogte = maxy - miny
        langste_zijde = max(breedte, hoogte)
        GEWENSTE_STAP_METER = 0.01

        if langste_zijde > 0:
            dynamische_prequantize = int(langste_zijde / GEWENSTE_STAP_METER)
        else:
            dynamische_prequantize = 1000

    if not gdf_connected.empty:
        # Stap A: Maak één grote lijnstructuur van alle grenzen zodat bogen exact samenvallen
        alle_interne_grenzen = unary_union(list(gdf_connected.geometry.boundary))

        # Stap B: Snap alle polygonen lichtjes naar die gezamenlijke grenzen (binnen 1mm)
        gdf_connected["geometry"] = gdf_connected.geometry.apply(
            lambda geom: snap(geom, alle_interne_grenzen, tolerance=0.001)
        )

        topo_init = tp.Topology(gdf_connected, prequantize=dynamische_prequantize)
        topo_init_simplified = topo_init.toposimplify(0.01)
        gdf_connected = topo_init_simplified.to_gdf()
        gdf_connected.index = gekoppeld.loc[list(connected_indices)].index
        gdf_connected.to_crs("EPSG:4326").to_file("debug_3_na_initiele topojson.geojson", driver="GeoJSON")

    if not gdf_connected.empty:
        alle_grenzen = unary_union([geom.boundary for geom in gdf_connected.geometry])
        gdf_connected["geometry"] = gdf_connected.geometry.apply(
            lambda geom: snap(geom, alle_grenzen, tolerance=GEWENSTE_STAP_METER)
        )
        gdf_connected.to_crs("EPSG:4326").to_file("debug_3_na_initiele topojson&snapping.geojson", driver="GeoJSON")

    print(f"Langste zijde van bbox: {langste_zijde:.2f} m")
    print(f"Dynamische prequantize ingesteld op: {dynamische_prequantize} (voor 1cm precisie)")

    processed_gdf = gpd.GeoDataFrame(columns=gdf_connected.columns, crs=gdf_connected.crs)

    if not gdf_connected.empty:
        topo = tp.Topology(gdf_connected, prequantize=dynamische_prequantize)
        topo_simplified = topo.toposimplify(TOLERANCE_METERS)
        processed_gdf = topo_simplified.to_gdf()
        processed_gdf.index = gdf_connected.index
        processed_gdf['geometry'] = processed_gdf['geometry'].buffer(0)
        processed_gdf.to_crs("EPSG:4326").to_file("debug_3_na_tweedetopojson.geojson", driver="GeoJSON")

    if not processed_gdf.empty:
        alle_grenzen_final = unary_union(
            [geom.boundary for geom in processed_gdf.geometry]
        )
        processed_gdf["geometry"] = processed_gdf.geometry.apply(
            lambda geom: snap(
                geom, alle_grenzen_final, tolerance=GEWENSTE_STAP_METER
            )
        )

        invalid_mask = ~processed_gdf['geometry'].is_valid
        if invalid_mask.any():
            processed_gdf.loc[invalid_mask, 'fixme'] = 'invalid'

        possible_matches_index = processed_gdf.sindex.query(processed_gdf.geometry, predicate="overlaps")
        overlapping_indices = set()
        for idx1, idx2 in zip(possible_matches_index[0], possible_matches_index[1]):
            if idx1 < idx2:
                geom1 = processed_gdf.geometry.iloc[idx1]
                geom2 = processed_gdf.geometry.iloc[idx2]
                if geom1.overlaps(geom2):
                    overlapping_indices.add(idx1)
                    overlapping_indices.add(idx2)

        if overlapping_indices:
            target_labels = processed_gdf.index[list(overlapping_indices)]
            processed_gdf.loc[target_labels, 'fixme'] = 'overlap'

        aantal_fouten = processed_gdf['fixme'].notna().sum() if 'fixme' in processed_gdf.columns else 0
        print(f"Aantal probleemgeometrieën in connected set: {aantal_fouten}")

    processed_parts.append(processed_gdf)

    if not gdf_isolated.empty:
        topo = tp.Topology(gdf_isolated, prequantize=dynamische_prequantize)
        topo_simplified = topo.toposimplify(TOLERANCE_METERS)
        gdf_isolated = topo_simplified.to_gdf()
        processed_parts.append(gdf_isolated)

    gdf_simplified = gpd.pd.concat(processed_parts, ignore_index=False)
    gdf_simplified = gdf_simplified.to_crs("EPSG:4326")

    print("Bezig met genereren van JOSM-zoekstring voor verouderde panden...")
    exporteer_verouderde_osm_zoekstring(
        osm_polys_met_meta=existing_osm_polys,
        gdf_grb=gdf_poly,
        output_path="te_verwijderen_zoekstring.txt"
    )

    print("Stap 2/2: Verwerken van tags, adrescorrecties en GeoJSON export...")
    features_out = []

    existing_farmyards = get_existing_osm_farmyards_in_wgs84(args.osm_bestand)

    for idx, row in gdf_simplified.iterrows():
        props = row.to_dict()
        geom_obj = props.pop('geometry', None)

        if not geom_obj or geom_obj.is_empty:
            continue

        date_val = props.get('VERSDATUM') or props.get('begindatum')
        if date_val:
            date_val = str(date_val)[:10]

        # Bepaal het prefix op basis van de GRB-laag (GBG of GBA)
        prefix = props.get('grb_layer', 'Gbg')
        grb_laag = str(props.get('grb_layer', '')).upper()

        oidn_val = props.get('OIDN') or props.get('id')
        ref_val = f"{prefix}/{int(oidn_val)}" if oidn_val is not None and str(oidn_val).isdigit() else (f"Gbg/{oidn_val}" if oidn_val else None)

        housenumber = props.get('addr:housenumber')
        heeft_adres = bool(housenumber and str(housenumber).strip() and str(housenumber).lower() != "nan")

        in_farmyard = False
        if existing_farmyards:
            in_farmyard = any(fy.intersects(geom_obj) for fy in existing_farmyards)

        if in_farmyard:
            building_val = 'farm' if heeft_adres else 'farm_auxiliary'
        else:
            building_val = 'house' if heeft_adres else 'yes'

        new_props = {}

        if grb_laag == 'GBA' or prefix == 'Gba':
            # Roep de nieuwe functie aan en geef de properties mee
            new_props = verwerk_gba_attributen(props)

        else:
            # Hoofdgebouw (GBG) logica
            if in_farmyard:
                building_val = 'farm' if heeft_adres else 'farm_auxiliary'
            else:
                building_val = 'house' if heeft_adres else 'yes'
            new_props['building'] = building_val

        # Metadata toevoegen
        if date_val:
            new_props['source:geometry:date'] = date_val
        if ref_val:
            new_props['source:geometry:ref'] = ref_val

        straten_raw = props.get('addr:street_list', [])
        huisnummers_raw = props.get('addr:housenumber_list', [])

        is_complex_blok = False

        if isinstance(straten_raw, list) and len(set(straten_raw)) > 1:
            is_complex_blok = True
        elif isinstance(huisnummers_raw, list) and len(huisnummers_raw) > 1:
            try:
                nummers_int = []
                for h in huisnummers_raw:
                    match = re.search(r'\d+', str(h))
                    if match:
                        nummers_int.append(int(match.group()))

                if nummers_int and len(nummers_int) == len(huisnummers_raw):
                    nummers_int.sort()
                    verschillen = [nummers_int[i+1] - nummers_int[i] for i in range(len(nummers_int)-1)]
                    max_verschil = max(verschillen) if verschillen else 0

                    if max_verschil > 2:
                        is_complex_blok = True
                else:
                    is_complex_blok = True
            except Exception:
                is_complex_blok = True

        if is_complex_blok:
            for s_val, h_val in zip(straten_raw, huisnummers_raw):
                punt_geom = props.get('original_address_point')
                if punt_geom:
                    extra_address_nodes.append({
                        'geometry': punt_geom,
                        'addr:street': s_val,
                        'addr:housenumber': h_val,
                        'fixme': 'Niet-opeenvolgende of complexe adressering: omgezet naar los adrespunt.'
                    })
        else:
            for osm_tag in ["addr:street", "addr:housenumber", "fixme"]:
                val = props.get(osm_tag)
                if val is not None and str(val).strip() != "" and str(val).lower() != "nan":
                    new_props[osm_tag] = str(val)

        features_out.append({
            "type": "Feature",
            "properties": new_props,
            "geometry": mapping(geom_obj)
        })

    if extra_address_nodes:
        print(f"--> {len(extra_address_nodes)} extra samengevoegde adresnode(s) aangemaakt.")
        gdf_extra = gpd.GeoDataFrame(extra_address_nodes, crs="EPSG:31370").to_crs("EPSG:4326")
        for _, ext_row in gdf_extra.iterrows():
            ext_geom = ext_row['geometry']
            if not ext_geom or ext_geom.is_empty:
                continue
            ext_props = {
                "addr:street": ext_row['addr:street'],
                "addr:housenumber": ext_row['addr:housenumber']
            }
            if 'fixme' in ext_row and pd.notna(ext_row['fixme']):
                ext_props['fixme'] = ext_row['fixme']

            features_out.append({
                "type": "Feature",
                "properties": ext_props,
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
