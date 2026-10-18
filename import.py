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
import yaml
from shapely.geometry import shape, mapping, Polygon, Point, box, MultiPolygon, MultiLineString
from shapely.ops import unary_union, polygonize, linemerge, snap
from shapely import set_precision
from collections import Counter
from shapely.validation import explain_validity, make_valid
from pyproj import Transformer

logging.basicConfig(filename='adres_koppelingen.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def laad_configuratie(config_pad="config.yaml"):
    """Laadt de tuning-parameters uit een YAML-bestand of valt terug op standaarden."""
    standaard_config = {
        'filtering': {
            'active': True,
            'overlap_threshold': 0.95
        },
        'addressing': {
            'import_loose_address_nodes': False
        },
        'topology': {
            'tolerance_meters': 0.05,
            'gewenste_stap_meter': 0.01
        }
    }

    if os.path.exists(config_pad):
        try:
            with open(config_pad, 'r', encoding='utf-8') as f:
                geladen_config = yaml.safe_load(f)
                if isinstance(geladen_config, dict):
                    # Samenvoegen met standaarden
                    for sectie, waarden in geladen_config.items():
                        if sectie in standaard_config and isinstancewaarde(waarden, dict):
                            standaard_config[sectie].update(waarden)
                        else:
                            standaard_config[sectie] = waarden
                    print(f"Configuratie succesvol geladen uit '{config_pad}'.")
                    return standaard_config
        except Exception as e:
            print(f"Waarschuwing: Kon '{config_pad}' niet lezen ({e}). Standaardwaarden worden gebruikt.", file=sys.stderr)
    else:
        print(f"Geen '{config_pad}' gevonden. Standaardwaarden worden gebruikt.")

    return standaard_config


# Vormt lange reeksen huisnummers om naar handelbaar formaat
def formatteer_huisnummers(huisnummers):
    if not huisnummers:
        return None

    gesorteerde_invoer = sorted(huisnummers, key=sorteer_huisnummer)

    pure_nums = []
    suffix_dict = {}
    other_vals = []

    for h in gesorteerde_invoer:
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

        # Groepeer in sub-reeksen (chunks met constante stap 1 of 2)
        if num_ints:
            chunks = []
            current_chunk = [num_ints[0]]
            step = 2 if len(num_ints) > 1 and (num_ints[1] - num_ints[0]) in (1, 2) else 1
            if len(num_ints) > 1 and (num_ints[1] - num_ints[0]) in (1, 2):
                step = num_ints[1] - num_ints[0]
            else:
                step = 1

            for i in range(1, len(num_ints)):
                expected = current_chunk[-1] + step
                if num_ints[i] == expected:
                    current_chunk.append(num_ints[i])
                else:
                    chunks.append(current_chunk)
                    current_chunk = [num_ints[i]]
                    if i < len(num_ints) - 1:
                        s_cand = num_ints[i+1] - num_ints[i]
                        step = s_cand if s_cand in (1, 2) else 1
            chunks.append(current_chunk)

            for chunk in chunks:
                if len(chunk) > 2:
                    result_parts.append(f"{chunk[0]}-{chunk[-1]}")
                elif len(chunk) == 2:
                    result_parts.append(f"{chunk[0]};{chunk[1]}")
                elif len(chunk) == 1:
                    result_parts.append(str(chunk[0]))

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
    """Parseert bestaande gebouwen en building:parts incl. timestamp en metadata uit het JOSM .osm-bestand en transformeert ze naar EPSG:3812."""
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

        gdf_osm = gdf_osm.to_crs("EPSG:3812")

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


def get_existing_osm_addresses_in_lambert2008(osm_pad):
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
        gdf_osm_addr = gdf_osm_addr.to_crs("EPSG:3812")
        buffered_geoms = [geom.buffer(5.0) for geom in gdf_osm_addr.geometry if geom and not geom.is_empty]
        return buffered_geoms
    except Exception as e:
        print(f"Waarschuwing bij inlezen bestaande OSM adres-elementen: {e}", file=sys.stderr)
        return []


def filter_existing_gdf(gdf_grb, osm_polys_met_meta, threshold=0.95):
    """Filtert GRB-gebouwen weg op basis van de beste individuele OSM match (gebouw of building:part),
    tenzij het GRB-gebouw recenter is dan de bestaande OSM-intekening."""
    if not osm_polys_met_meta or gdf_grb.empty:
        return gdf_grb

    osm_records = [{'geometry': op['geom'], 'timestamp': op.get('timestamp')} for op in osm_polys_met_meta]
    osm_gdf = gpd.GeoDataFrame(osm_records, crs="EPSG:3812")

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
            if coverage < threshold:
                continue

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

        if best_coverage_grb == 0.0:
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


def exporteer_verouderde_osm_zoekstring(osm_polys_met_meta, gdf_grb, output_path="to_check.txt", max_overlap_ratio=0.10, min_leeftijd_jaar=3):
    if not osm_polys_met_meta or gdf_grb.empty:
        print("Geen data om te controleren.")
        return

    TARGET_CRS = "EPSG:3812"
    BESCHERMDE_BUILDINGS = {'water_tower', 'bunker', 'bridge'}
    BESCHERMDE_KEYS = {'man_made', 'historic', 'tourism', 'natural'}

    osm_records = []
    for op in osm_polys_met_meta:
        tags = op.get('tags', {})
        if 'building' not in tags:
            continue

        if tags.get('building') in BESCHERMDE_BUILDINGS:
            continue

        if any(k in tags for k in BESCHERMDE_KEYS):
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

    if gdf_grb.crs != TARGET_CRS:
        gdf_grb = gdf_grb.to_crs(TARGET_CRS)

    with np.errstate(all='ignore'):
        grb_union = unary_union([g for g in gdf_grb.geometry if g and not g.is_empty])

    print(f"Bezig met analyseren van {len(osm_gdf)} geldige OSM-gebouwen (inclusief leeftijdscheck >= {min_leeftijd_jaar} jaar)...")

    osm_gdf['area'] = osm_gdf.geometry.area
    osm_gdf = osm_gdf[osm_gdf['area'] > 0].copy()
    if osm_gdf.empty:
        print("Geen OSM-gebouwen met geldige oppervlakte over.")
        return

    intersections = osm_gdf.geometry.intersection(grb_union)
    osm_gdf['overlap_ratio'] = intersections.area / osm_gdf['area']

    huidige_tijd = pd.Timestamp.now()
    leeftijd_grens_dagen = min_leeftijd_jaar * 365.25

    def is_oud_genoeg(ts_val):
        if pd.isna(ts_val):
            return True
        ts = pd.to_datetime(ts_val, errors='coerce')
        if pd.isna(ts):
            return True
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        leeftijd_dagen = (huidige_tijd - ts).days
        return leeftijd_dagen >= leeftijd_grens_dagen

    laag_overlap = osm_gdf['overlap_ratio'] < max_overlap_ratio
    oud_genoeg = osm_gdf['timestamp'].apply(is_oud_genoeg)

    wees_panden = osm_gdf[laag_overlap & oud_genoeg]

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


def haal_wfs_data_op(typename, bbox_3812):
    """Haalt GeoJSON data direct op in Lambert 2008 (EPSG:3812) via WFS."""
    base_url = "https://geo.api.vlaanderen.be/GRB/wfs"

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typename": typename,
        "outputFormat": "application/json",
        "srsName": "EPSG:3812",
        "bbox": f"{bbox_3812[0]},{bbox_3812[1]},{bbox_3812[2]},{bbox_3812[3]},EPSG:3812"
    }
    url = f"{base_url}?{urllib.parse.urlencode(params)}"

    print(f"Bezig met ophalen van laag '{typename}' via WFS in EPSG:3812...")
    try:
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode('utf-8'))

            if not data.get("features"):
                return gpd.GeoDataFrame(geometry=[], crs="EPSG:3812")

            return gpd.GeoDataFrame.from_features(data, crs="EPSG:3812")

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
    boundaries = gdf.geometry.boundary.tolist()
    merged_lines = unary_union(MultiLineString(boundaries))
    fixed_polys = list(polygonize(merged_lines))
    gdf_fixed = gpd.GeoDataFrame(geometry=fixed_polys, crs=gdf.crs)
    return gdf_fixed


def vul_gaten_van_polygoon(geom):
  if geom is None or geom.is_empty:
    return geom

  if geom.geom_type == 'Polygon':
    return Polygon(geom.exterior)
  elif geom.geom_type == 'MultiPolygon':
    solide_delen = [Polygon(p.exterior) for p in geom.geoms]
    return MultiPolygon(solide_delen)
  return geom


def veilige_convex_hull_per_part(geom):
  if geom is None or geom.is_empty:
    return geom

  if geom.geom_type == 'Polygon':
    return geom.convex_hull
  elif geom.geom_type == 'MultiPolygon':
    hulls = [p.convex_hull for p in geom.geoms]
    return unary_union(hulls)
  return geom.convex_hull


def koppel_adressen_aan_gbg(
    gdf_poly,
    sub_addr,
    gdf_wsm,
    existing_osm_addresses,
    filter_osmadressen=False,
    import_loose_address_nodes=False,
):
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

  intersect_join = gpd.sjoin(
      proj_addr, proj_poly, how='inner', predicate='intersects'
  )

  matched_addr_indices = (
      set(intersect_join.index) if not intersect_join.empty else set()
  )
  unmatched_addr = proj_addr.loc[
      ~proj_addr.index.isin(matched_addr_indices)
  ].copy()

  if filter_osmadressen and existing_osm_addresses and not unmatched_addr.empty:
    filtered_unmatched_indices = []
    for idx, row in unmatched_addr.iterrows():
      pt = row['geometry']
      if not pt or pt.is_empty:
        continue
      reeds_aanwezig = any(
          buffered_poly.contains(pt)
          for buffered_poly in existing_osm_addresses
      )
      if not reeds_aanwezig:
        filtered_unmatched_indices.append(idx)
    unmatched_addr = unmatched_addr.loc[filtered_unmatched_indices].copy()

  nearest_join = None
  if not unmatched_addr.empty and not proj_poly.empty:
    nearest_join = gpd.sjoin_nearest(
        unmatched_addr,
        proj_poly,
        how='inner',
        distance_col='afstand_tot_gebouw',
        max_distance=4.0,
    )

  if nearest_join is not None and not nearest_join.empty:
    matched_addr_indices.update(nearest_join.index)

  hull_join = None
  still_unmatched_addr = proj_addr.loc[
      ~proj_addr.index.isin(matched_addr_indices)
  ].copy()

  if not still_unmatched_addr.empty and not proj_poly.empty:
    gekoppelde_gebouw_indices = set()
    if not intersect_join.empty:
      gekoppelde_gebouw_indices.update(intersect_join['orig_index'])
    if nearest_join is not None and not nearest_join.empty:
      gekoppelde_gebouw_indices.update(nearest_join['orig_index'])

    lege_gebouwen = proj_poly.loc[
        ~proj_poly['orig_index'].isin(gekoppelde_gebouw_indices)
    ].copy()

    if not lege_gebouwen.empty:
      gevulde_gebouwen = []
      for idx, g_row in lege_gebouwen.iterrows():
        orig_geom = g_row['geometry']
        if not orig_geom or orig_geom.is_empty:
          continue
        solide_geom = vul_gaten_van_polygoon(orig_geom)
        g_row_solide = g_row.copy()
        g_row_solide['geometry'] = solide_geom
        gevulde_gebouwen.append(g_row_solide)

      gdf_solide = gpd.GeoDataFrame(
          gevulde_gebouwen, crs=proj_poly.crs
      ).reset_index(drop=True)
      hull_join = gpd.sjoin(
          still_unmatched_addr, gdf_solide, how='inner', predicate='intersects'
      )

      if not hull_join.empty:
        matched_in_2a = set(hull_join.index)
        still_unmatched_addr = still_unmatched_addr.loc[
            ~still_unmatched_addr.index.isin(matched_in_2a)
        ].copy()
        gekoppelde_gebouw_indices.update(hull_join['orig_index'])
        lege_gebouwen = proj_poly.loc[
            ~proj_poly['orig_index'].isin(gekoppelde_gebouw_indices)
        ].copy()

      if not still_unmatched_addr.empty and not lege_gebouwen.empty:
        hull_gebouwen = []
        for idx, g_row in lege_gebouwen.iterrows():
          orig_geom = g_row['geometry']
          if not orig_geom or orig_geom.is_empty:
            continue
          hull_geom = veilige_convex_hull_per_part(orig_geom)
          g_row_hull = g_row.copy()
          g_row_hull['geometry'] = hull_geom
          hull_gebouwen.append(g_row_hull)

        if hull_gebouwen:
          gdf_hulls = gpd.GeoDataFrame(
              hull_gebouwen, crs=proj_poly.crs
          ).reset_index(drop=True)
          convex_join = gpd.sjoin(
              still_unmatched_addr,
              gdf_hulls,
              how='inner',
              predicate='intersects',
          )

          if not convex_join.empty:
            matched_in_2b = set(convex_join.index)
            matched_addr_indices.update(matched_in_2b)
            still_unmatched_addr = still_unmatched_addr.loc[
                ~still_unmatched_addr.index.isin(matched_in_2b)
            ].copy()

          if hull_join is None or hull_join.empty:
            hull_join = convex_join
          elif not convex_join.empty:
            hull_join = gpd.pd.concat(
                [hull_join, convex_join], ignore_index=True
            )

    if import_loose_address_nodes and not still_unmatched_addr.empty:
      for _, row_unmatched in still_unmatched_addr.iterrows():
        straat_val = row_unmatched.get('addr:street', row_unmatched.get('LSTRNM', ''))
        huisnr_val = row_unmatched.get('addr:housenumber', row_unmatched.get('HUISNR', ''))

        extra_address_nodes.append({
            'geometry': row_unmatched['geometry'],
            'addr:street': straat_val,
            'addr:housenumber': huisnr_val,
            'fixme': 'Niet gekoppeld aan gebouw: behouden als los adrespunt.',
        })

  all_matches = []
  if not intersect_join.empty:
    all_matches.append(intersect_join)
  if nearest_join is not None and not nearest_join.empty:
    all_matches.append(nearest_join)
  if hull_join is not None and not hull_join.empty:
    all_matches.append(hull_join)

  combined_join = (
      gpd.pd.concat(all_matches, ignore_index=True)
      if all_matches
      else gpd.GeoDataFrame()
  )

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

  dubbele_adressen = {
      k: v for k, v in adres_naar_gebouwen.items() if len(v) > 1
  }
  indices_to_drop = set()

  if dubbele_adressen and proj_wsm is not None and not proj_wsm.empty:
    straat_kolom = (
        'LSTRNM'
        if 'LSTRNM' in proj_wsm.columns
        else next(
            (
                col
                for col in [
                    'STRAATNM',
                    'straatnaam',
                    'Straatnaam',
                    'NAAM',
                    'naam',
                ]
                if col in proj_wsm.columns
            ),
            None,
        )
    )

    for (straat, huisnr), gebouw_indices in dubbele_adressen.items():
      sub_gebouwen = proj_poly.loc[proj_poly['orig_index'].isin(gebouw_indices)]

      specifieke_segmenten = proj_wsm
      if straat_kolom:
        mask = (
            proj_wsm[straat_kolom]
            .astype(str)
            .str.strip()
            .str.lower()
            == straat
        )
        gefilterd = proj_wsm[mask]
        if not gefilterd.empty:
          specifieke_segmenten = gefilterd

      gebouw_afstanden = {}
      for g_idx, g_row in sub_gebouwen.iterrows():
        g_geom = g_row['geometry']
        if g_geom and not g_geom.is_empty:
          gebouw_afstanden[g_idx] = (
              specifieke_segmenten.geometry.distance(g_geom).min()
          )

      if gebouw_afstanden:
        beste_g_idx = min(gebouw_afstanden, key=gebouw_afstanden.get)

        for g_idx in gebouw_afstanden:
          if g_idx != beste_g_idx:
            foute_rows = combined_join[
                (combined_join['orig_index'] == g_idx)
                & (
                    combined_join['addr:street'].str.strip().str.lower()
                    == straat
                )
                & (
                    combined_join['addr:housenumber'].str.strip().str.lower()
                    == huisnr
                )
            ]
            indices_to_drop.update(foute_rows.index)

  if indices_to_drop:
    combined_join = combined_join.drop(index=list(indices_to_drop)).reset_index(
        drop=True
    )

  df_addr_records = []
  for orig_index, group in combined_join.groupby('orig_index'):
    straten_raw = [
        str(s).strip()
        for s in group['addr:street'].dropna()
        if str(s).strip() != '' and str(s).lower() != 'nan'
    ]
    if not straten_raw:
      continue
    primair_straat = max(set(straten_raw), key=straten_raw.count)
    groep_primair = group[group['addr:street'] == primair_straat]
    huisnummers_primair = sorted(
        list({
            str(h).strip()
            for h in groep_primair['addr:housenumber'].dropna()
            if str(h).strip() != ''
        }),
        key=sorteer_huisnummer,
    )

    is_complex = False
    if len(huisnummers_primair) > 1:
      try:
        nummers_int = sorted([
            int(re.search(r'\d+', h).group())
            for h in huisnummers_primair
            if re.search(r'\d+', h)
        ])
        if len(nummers_int) == len(huisnummers_primair):
          verschillen = [
              nummers_int[i + 1] - nummers_int[i]
              for i in range(len(nummers_int) - 1)
          ]
          if any(d not in (1, 2) for d in verschillen) and len(set(verschillen)) > 3:
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
            'fixme': (
                'Complexe/samengevoegde situaties: automatisch omgezet naar'
                ' los adrespunt.'
            ),
        })
      continue

    geformatteerde_nummers = formatteer_huisnummers(huisnummers_primair)

    df_addr_records.append({
        'orig_index': orig_index,
        'addr:street': primair_straat,
        'addr:housenumber': geformatteerde_nummers,
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
  new_props = {}
  props_lower = {
      str(k).lower(): v for k, v in props.items() if k is not None
  }
  gba_type = props_lower.get('type')

  try:
    gba_type_int = int(gba_type) if gba_type is not None else None
  except (ValueError, TypeError):
    gba_type_int = None

  if gba_type_int == 2:
    new_props['building'] = 'roof'
  elif gba_type_int == 3:
    new_props['building:part'] = 'corridor'
    new_props['building:min_level'] = '1'
    new_props['building:levels'] = '2'
  elif gba_type_int == 1:
    new_props['building:part'] = 'yes'
    new_props['building:min_level'] = '1'
    new_props['building:levels'] = '2'
  elif gba_type_int == 4:
    new_props['building:part'] = 'steps'
  else:
    new_props['building:part'] = 'yes'

  return new_props


def verwerk_knw_attributen(props):
    new_props = {}
    knw_type = props.get('LBLTYPE')

    if not knw_type:
        return new_props

    if knw_type == 'cabine':
        new_props['building'] = 'service'
    elif knw_type.startswith('silo'):
        new_props['building'] = 'silo'
    else:
        new_props['building'] = knw_type

    return new_props


def laad_en_prepareer_data(args, bbox, config):
    """Haalt alle WFS data op, filtert, koppelt adressen en voegt GBG en GBA samen."""
    print(f"Bounding box uitlezen uit {args.osm_bestand}...")

    gdf_poly = haal_wfs_data_op("GRB:GBG", bbox)
    gdf_poly['grb_layer'] = 'Gbg'
    gdf_gba  = haal_wfs_data_op("GRB:GBA", bbox)
    gdf_gba['grb_layer'] = 'Gba'
    gdf_addr = haal_wfs_data_op("GRB:Adres", bbox)
    gdf_wsm  = haal_wfs_data_op("GRB:Wegsegment", bbox)
    gdf_knw = haal_wfs_data_op("GRB:KNW", bbox)
    gdf_knw['grb_layer'] = 'Knw'

    if gdf_poly.empty:
        print("Geen gebouwen gevonden in de WFS voor deze bounding box.", file=sys.stderr)
        sys.exit(0)

    print(f"Succesvol {len(gdf_poly)} gebouwen, {len(gdf_gba)} bijgebouwen, {len(gdf_addr)} adrespunten en {len(gdf_wsm)} wegsegmenten opgehaald via WFS.")

    for gdf, name in [(gdf_addr, 'addr'), (gdf_wsm, 'wsm'), (gdf_gba, 'gba')]:
        if gdf_poly.crs != gdf.crs:
            globals()[f'gdf_{name}'] = gdf.to_crs(gdf_poly.crs)

    mapping_columns = {}
    for col in ["HUISNR", "huisnummer", "Huisnummer"]:
        if col in gdf_addr.columns:
            mapping_columns[col] = "addr:housenumber"
    for col in ["STRAATNM", "straatnaam", "Straatnaam"]:
        if col in gdf_addr.columns:
            mapping_columns[col] = "addr:street"

    sub_addr = gdf_addr[[c for c in mapping_columns.keys()] + ['geometry']].copy()
    sub_addr = sub_addr.rename(columns=mapping_columns)

    existing_osm_addresses = get_existing_osm_addresses_in_lambert2008(args.osm_bestand)

    print("Bezig met slim koppelen en sorteren van adressen aan GBG gebouwen...")
    import_losse_knopen = config.get('addressing', {}).get('import_loose_address_nodes', False)
    gekoppeld, extra_address_nodes = koppel_adressen_aan_gbg(
        gdf_poly=gdf_poly,
        sub_addr=sub_addr,
        gdf_wsm=gdf_wsm,
        existing_osm_addresses=existing_osm_addresses,
        import_loose_address_nodes=import_losse_knopen
    )

    if not config.get('addressing', {}).get('import_loose_address_nodes', False):
        print("Opmerking: Losse adrespunten worden genegeerd (instelling 'addressing.import_loose_address_nodes').")
        extra_address_nodes = []

    if not gdf_gba.empty and 'TYPE' in gdf_gba.columns:
        gdf_gba['TYPE'] = pd.to_numeric(gdf_gba['TYPE'], errors='coerce')
        aantal_voor = len(gdf_gba)
        gdf_gba = gdf_gba[gdf_gba['TYPE'] != 6]
        print(f"GBA gefilterd: {len(gdf_gba)} over ({aantal_voor - len(gdf_gba)} garagetoegangen verwijderd).")

    if not gdf_knw.empty and 'TYPE' in gdf_knw.columns:
        gdf_knw['TYPE'] = pd.to_numeric(gdf_knw['TYPE'], errors='coerce')
        uitsluitingen = [4, 5, 6, 12, 14, 22, 23, 24]
        aantal_voor = len(gdf_knw)
        gdf_knw = gdf_knw[~gdf_knw['TYPE'].isin(uitsluitingen)]
        print(f"KNW gefilterd: {len(gdf_knw)} over ({aantal_voor - len(gdf_knw)} technische/waterbouwkundige objecten verwijderd).")

    if not gdf_gba.empty:
        for col in ['addr:street', 'addr:housenumber']:
            if col not in gdf_gba.columns:
                gdf_gba[col] = None
        gekoppeld = pd.concat([gekoppeld, gdf_gba], ignore_index=True)

    if not gdf_knw.empty:
        for col in ['addr:street', 'addr:housenumber']:
            if col not in gdf_knw.columns:
                gdf_knw[col] = None
        gekoppeld = pd.concat([gekoppeld, gdf_knw], ignore_index=True)

    gekoppeld['wkt'] = gekoppeld['geometry'].apply(lambda g: g.wkt if g else "")
    gekoppeld = gekoppeld.drop_duplicates(subset=['wkt'], keep='first').drop(columns=['wkt'])

    if gekoppeld.crs is None or "3812" not in str(gekoppeld.crs.to_string()):
        gekoppeld = gekoppeld.to_crs("EPSG:3812")

    gekoppeld['geometry'] = gekoppeld['geometry'].buffer(0)
    gekoppeld = gekoppeld.explode(index_parts=False).reset_index(drop=True)

    gekoppeld = gekoppeld[gekoppeld.geometry.area >= 1.0]

    existing_osm_polys = get_existing_osm_polys_met_meta(args.osm_bestand)

    # Filtering toepassen op basis van CLI vlag OF configuratiebestand
    filtering_actief = config.get('filtering', {}).get('active', True) and not args.fno
    drempelwaarde = config.get('filtering', {}).get('overlap_threshold', 0.95)

    if filtering_actief:
        print(f"Filtering actief: controleert GRB-gebouwen tegen OSM met overlapdrempel van {drempelwaarde * 100:.0f}%...")
        gekoppeld = filter_existing_gdf(gekoppeld, existing_osm_polys, threshold=drempelwaarde)
    else:
        print("Filtering uitgeschakeld: alle GRB-gebouwen worden meegenomen.")

    return gekoppeld, extra_address_nodes, existing_osm_polys, gdf_poly


def verwerk_topologie_en_simplificatie(gekoppeld, originele_bbox_wgs84, config):
    """Bouwt het TopoJSON netwerk op en vereenvoudigt de geometrieën netjes."""
    gekoppeld.to_crs("EPSG:4326").to_file("debug_1_ruwe_polygonen.geojson", driver="GeoJSON")
    print("--> Dump 1 opgeslagen: debug_1_ruwe_polygonen.geojson")

    gekoppeld['geometry'] = gekoppeld['geometry'].apply(lambda geom: set_precision(geom, grid_size=0.001))

    TOLERANCE_METERS = config.get('topology', {}).get('tolerance_meters', 0.05)
    GEWENSTE_STAP_METER = config.get('topology', {}).get('gewenste_stap_meter', 0.01)

    print(f"Stap 1/2: TopoJSON netwerk opbouwen voor {len(gekoppeld)} gebouw(en) (tolerance={TOLERANCE_METERS}m)...")

    sindex = gekoppeld.sindex
    connected_indices = set()

    for idx, geom in gekoppeld.geometry.items():
        if geom is None or geom.is_empty:
            continue
        possible_matches = list(sindex.query(geom, predicate="intersects"))
        if [m for m in possible_matches if m != idx]:
            connected_indices.add(idx)

    gdf_connected = gekoppeld.loc[list(connected_indices)].copy()
    gdf_isolated = gekoppeld.loc[~gekoppeld.index.isin(connected_indices)].copy()
    gdf_connected.to_crs("EPSG:4326").to_file("debug_2gekoppeld_voor_topojson.geojson", driver="GeoJSON")

    total_bounds = gdf_connected.total_bounds
    if len(gdf_connected) == 0 or np.isnan(total_bounds[0]):
        dynamische_prequantize = 1000
    else:
        langste_zijde = max(total_bounds[2] - total_bounds[0], total_bounds[3] - total_bounds[1])
        dynamische_prequantize = int(langste_zijde / GEWENSTE_STAP_METER) if langste_zijde > 0 else 1000

    processed_parts = []

    if not gdf_connected.empty:
        topo = tp.Topology(gdf_connected, prequantize=dynamische_prequantize)
        topo_simplified = topo.toposimplify(TOLERANCE_METERS)
        processed_gdf = topo_simplified.to_gdf()
        processed_gdf.index = gdf_connected.index
        processed_gdf['geometry'] = processed_gdf['geometry'].buffer(0)
        processed_parts.append(processed_gdf)

    if not gdf_isolated.empty:
        topo_iso = tp.Topology(gdf_isolated, prequantize=dynamische_prequantize)
        gdf_isolated = topo_iso.toposimplify(TOLERANCE_METERS).to_gdf()
        processed_parts.append(gdf_isolated)

    gdf_simplified = pd.concat(processed_parts, ignore_index=False).to_crs("EPSG:4326")

    box_wgs84 = box(originele_bbox_wgs84[0], originele_bbox_wgs84[1], originele_bbox_wgs84[2], originele_bbox_wgs84[3])
    gdf_simplified = gdf_simplified[gdf_simplified.geometry.intersects(box_wgs84)].copy()

    return gdf_simplified


def main():
    parser = argparse.ArgumentParser(description="Leest een JOSM .osm-bestand en haalt GRB-gebouwen op.")
    parser.add_argument("osm_bestand", help="Pad naar het JOSM .osm-bestand")
    parser.add_argument("-fno", action='store_true', help="Schakel filtering uit (overrules config).")
    parser.add_argument("--config", default="config.yaml", help="Pad naar het YAML-configuratiebestand.")
    args = parser.parse_args()

    if not os.path.exists(args.osm_bestand):
        print(f"Fout: Het bestand '{args.osm_bestand}' is niet gevonden.", file=sys.stderr)
        sys.exit(1)

    # 0. Configuratie inladen
    config = laad_configuratie(args.config)

    try:
        originele_bbox_wgs84 = lees_bbox_uit_osm(args.osm_bestand)
        minlon, minlat, maxlon, maxlat = originele_bbox_wgs84

        to_lambert = Transformer.from_crs('EPSG:4326', 'EPSG:3812', always_xy=True)
        minx, miny = to_lambert.transform(minlon, minlat)
        maxx, maxy = to_lambert.transform(maxlon, maxlat)

        buffer = 100.0
        bbox_3812 = (
            minx - buffer,
            miny - buffer,
            maxx + buffer,
            maxy + buffer
        )

    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    # 1. Data inladen en voorbereiden
    gekoppeld, extra_address_nodes, existing_osm_polys, gdf_poly = laad_en_prepareer_data(args, bbox_3812, config)

    # 2. Topologie en simplificatie
    gdf_simplified = verwerk_topologie_en_simplificatie(gekoppeld, originele_bbox_wgs84, config)

    # 3. Zoekstring genereren
    exporteer_verouderde_osm_zoekstring(
        osm_polys_met_meta=existing_osm_polys,
        gdf_grb=gdf_poly,
        output_path="to_check.txt"
    )

    # 4. Tags toevoegen en GeoJSON wegschrijven
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
            new_props = verwerk_gba_attributen(props)

        elif grb_laag == 'KNW' or prefix == 'Knw':
            new_props = verwerk_knw_attributen(props)

        else:
            if in_farmyard:
                building_val = 'farm' if heeft_adres else 'farm_auxiliary'
            else:
                building_val = 'house' if heeft_adres else 'yes'
            new_props['building'] = building_val

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
        gdf_extra = gpd.GeoDataFrame(extra_address_nodes, crs="EPSG:3812").to_crs("EPSG:4326")
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
