# import.py

import argparse
import os
import sys
import time
import json
import gc
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
import geopandas as gpd
import pandas as pd
import numpy as np
import logging
import re
import yaml
import math
from shapely.geometry import shape, mapping, Polygon, Point, box, MultiPolygon, MultiLineString
from shapely.ops import unary_union, polygonize, linemerge
from shapely import set_precision
from shapely.validation import make_valid
from pyproj import Transformer
import topojson as tp
from concurrent.futures import ThreadPoolExecutor
from shapely.ops import snap
from address_handler import koppel_adressen

logging.basicConfig(filename='adres_koppelingen.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==============================================================================
# 1. CONFIGURATIE & HULPFUNCTIES
# ==============================================================================

def laad_configuratie(config_pad="tuning.yaml"):
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
        },
        'performance': {
            'show_timers': True
        }
    }

    if os.path.exists(config_pad):
        try:
            with open(config_pad, 'r', encoding='utf-8') as f:
                geladen_config = yaml.safe_load(f)
                if isinstance(geladen_config, dict):
                    for sectie, waarden in geladen_config.items():
                        if sectie in standaard_config and isinstance(waarden, dict):
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

def log_timer(config, bericht):
    """Print alleen als timers ingeschakeld zijn in de configuratie."""
    if config.get('performance', {}).get('show_timers', True):
        print(bericht)

def test_snap_naar_buren(gdf, snap_tolerance=0.01):
    if gdf.empty:
        return gdf

    gdf = gdf.copy().reset_index(drop=True)
    geoms = gdf.geometry.tolist()
    sindex = gdf.sindex

    for i in range(len(geoms)):
        geom = geoms[i]
        if geom is None or geom.is_empty:
            continue

        possible_matches = list(sindex.intersection(geom.bounds))

        for j in possible_matches:
            if i == j:
                continue

            other_geom = geoms[j]
            if other_geom is None or other_geom.is_empty:
                continue

            if geoms[i].distance(other_geom) <= snap_tolerance:
                snapped = snap(geoms[i], other_geom, tolerance=snap_tolerance)
                if not snapped.is_empty and snapped.is_valid:
                    geoms[i] = snapped

    gdf.geometry = geoms
    gdf['geometry'] = gdf['geometry'].buffer(0)
    return gdf

# ==============================================================================
# 2. EXTRACTIE & PARSING (OSM EN WFS)
# ==============================================================================

class OsmContext:
    """Streamende parser die het .osm bestand geheugenefficiënt verwerkt."""
    def __init__(self, osm_pad):
        self.osm_pad = osm_pad
        self.bbox_wgs84 = None
        self.bbox_3812 = None
        self.existing_polys_3812 = []
        self.existing_farmyards_wgs84 = []
        self.existing_addresses_buffered_3812 = []

        self._parse_osm_streaming()

    def _parse_osm_streaming(self):
        try:
            nodes = {}
            node_tags = {}
            ways = {}
            way_meta = {}
            way_tags = {}
            relations = []

            for event, elem in ET.iterparse(self.osm_pad, events=('end',)):
                tag = elem.tag

                if tag == 'bounds':
                    minlat, minlon = float(elem.get('minlat')), float(elem.get('minlon'))
                    maxlat, maxlon = float(elem.get('maxlat')), float(elem.get('maxlon'))
                    self.bbox_wgs84 = (minlon, minlat, maxlon, maxlat)
                    elem.clear()

                elif tag == 'node':
                    nid = elem.get('id')
                    lat_str, lon_str = elem.get('lat'), elem.get('lon')
                    if lat_str is not None and lon_str is not None:
                        try:
                            nodes[nid] = (float(lon_str), float(lat_str))
                            tags = {t.get('k'): t.get('v') for t in elem.findall('tag')}
                            if tags:
                                node_tags[nid] = tags
                        except ValueError:
                            pass
                    elem.clear()

                elif tag == 'way':
                    wid = elem.get('id')
                    nds = [nd.get('ref') for nd in elem.findall('nd') if nd.get('ref') in nodes]
                    coords = [nodes[ref] for ref in nds]
                    if len(coords) >= 2:
                        ways[wid] = coords
                        way_meta[wid] = elem.get('timestamp')
                        way_tags[wid] = {t.get('k'): t.get('v') for t in elem.findall('tag')}
                    elem.clear()

                elif tag == 'relation':
                    rel_id = elem.get('id')
                    rel_timestamp = elem.get('timestamp')
                    tags = {t.get('k'): t.get('v') for t in elem.findall('tag')}
                    members = [(m.get('type'), m.get('ref'), m.get('role')) for m in elem.findall('member')]
                    relations.append((rel_id, rel_timestamp, tags, members))
                    elem.clear()

            if self.bbox_wgs84 is None:
                raise ValueError(f"Geen <bounds> gevonden in {self.osm_pad}")

            to_lambert = Transformer.from_crs('EPSG:4326', 'EPSG:3812', always_xy=True)
            minx, miny = to_lambert.transform(self.bbox_wgs84[0], self.bbox_wgs84[1])
            maxx, maxy = to_lambert.transform(self.bbox_wgs84[2], self.bbox_wgs84[3])
            buffer = 100.0
            self.bbox_3812 = (minx - buffer, miny - buffer, maxx + buffer, maxy + buffer)

            polys_meta = []
            farmyard_polys = []
            addr_geoms = []

            for nid, tags in node_tags.items():
                if 'addr:housenumber' in tags:
                    addr_geoms.append(shape({"type": "Point", "coordinates": nodes[nid]}))

            for wid, coords in ways.items():
                wtags = way_tags.get(wid, {})

                if wtags.get('landuse') == 'farmyard':
                    if len(coords) >= 4 and coords[0] == coords[-1]:
                        farmyard_polys.append(Polygon(coords))

                if 'addr:housenumber' in wtags:
                    if len(coords) >= 4 and coords[0] == coords[-1]:
                        addr_geoms.append(Polygon(coords))
                    else:
                        addr_geoms.append(shape({"type": "LineString", "coordinates": coords}))

                is_building = 'building' in wtags or any(k.startswith('building:part') for k in wtags)
                if is_building and len(coords) >= 4 and coords[0] == coords[-1]:
                    try:
                        polys_meta.append({
                            'geometry': Polygon(coords),
                            'timestamp': way_meta.get(wid),
                            'osm_id': wid,
                            'tags': wtags
                        })
                    except Exception:
                        pass

            for rel_id, rel_timestamp, tags, members in relations:
                if any(k.startswith('building') for k in tags):
                    outer_linestrings = []
                    for m_type, m_ref, _ in members:
                        if m_type == 'way' and m_ref in ways:
                            outer_linestrings.append(shape({"type": "LineString", "coordinates": ways[m_ref]}))
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
                            pass

            if polys_meta:
                gdf_osm = gpd.GeoDataFrame({
                    'timestamp': [item['timestamp'] for item in polys_meta],
                    'osm_id': [item['osm_id'] for item in polys_meta],
                    'tags': [item['tags'] for item in polys_meta]
                }, geometry=[item['geometry'] for item in polys_meta], crs="EPSG:4326").to_crs("EPSG:3812")

                self.existing_polys_3812 = [
                    {'geom': row['geometry'], 'timestamp': row['timestamp'], 'osm_id': row['osm_id'], 'tags': row['tags']}
                    for _, row in gdf_osm.iterrows() if row['geometry'] and not row['geometry'].is_empty
                ]

            if farmyard_polys:
                self.existing_farmyards_wgs84 = [g for g in gpd.GeoDataFrame(geometry=farmyard_polys, crs="EPSG:4326").geometry if g and not g.is_empty]

            if addr_geoms:
                gdf_addr = gpd.GeoDataFrame(geometry=addr_geoms, crs="EPSG:4326").to_crs("EPSG:3812")
                self.existing_addresses_buffered_3812 = [g.buffer(5.0) for g in gdf_addr.geometry if g and not g.is_empty]

            gc.collect()

        except Exception as e:
            print(f"Fout bij het parsen van OSM-bestand {self.osm_pad}: {e}", file=sys.stderr)
            raise

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
    print(f"Bezig met ophalen van laag '{typename}' via WFS...")
    try:
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode('utf-8'))
            if not data.get("features"):
                return gpd.GeoDataFrame(geometry=[], crs="EPSG:3812")
            return gpd.GeoDataFrame.from_features(data, crs="EPSG:3812")
    except Exception as e:
        print(f"Fout bij ophalen laag {typename}: {e}", file=sys.stderr)
        sys.exit(1)

def haal_alle_grb_lagen_op(bbox_3812):
    lagen = {
        'gbg': "GRB:GBG",
        'gba': "GRB:GBA",
        'knw': "GRB:KNW",
        'adres': "GRB:Adres",
        'wegsegment': "GRB:Wegsegment"
    }

    resultaten = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_key = {
            executor.submit(haal_wfs_data_op, typename, bbox_3812): key
            for key, typename in lagen.items()
        }
        for future in future_to_key:
            key = future_to_key[future]
            resultaten[key] = future.result()

    resultaten['gbg']['grb_layer'] = 'Gbg'
    resultaten['gba']['grb_layer'] = 'Gba'
    if not resultaten['knw'].empty:
        resultaten['knw']['grb_layer'] = 'Knw'

    return resultaten

# ==============================================================================
# 3. TRANSFORMATIE & RUIMTELIJKE VERWERKING
# ==============================================================================

def filter_existing_gdf(gdf_grb, osm_polys_met_meta, threshold=0.95):
    if not osm_polys_met_meta or gdf_grb.empty:
        return gdf_grb

    osm_gdf = gpd.GeoDataFrame([{'geometry': op['geom'], 'timestamp': op.get('timestamp')} for op in osm_polys_met_meta], crs="EPSG:3812")

    filtered_indices = []
    osm_geoms = osm_gdf['geometry'].values
    osm_timestamps = osm_gdf['timestamp'].values

    grb_datums = pd.to_datetime(gdf_grb.get('VERSDATUM', gdf_grb.get('begindatum', pd.Series([None]*len(gdf_grb)))), errors='coerce')

    for idx, row in enumerate(gdf_grb.itertuples()):
        orig_idx = gdf_grb.index[idx]
        geom = row.geometry
        if not geom or geom.is_empty or geom.area == 0:
            continue
        grb_area = geom.area
        grb_datum = grb_datums.iloc[idx]
        if pd.notna(grb_datum) and hasattr(grb_datum, 'tzinfo') and grb_datum.tzinfo is not None:
            grb_datum = grb_datum.tz_localize(None)

        possible_matches = list(osm_gdf.sindex.intersection(geom.bounds))
        if not possible_matches:
            filtered_indices.append(orig_idx)
            continue

        relevant_osm = [
            (osm_geoms[p], osm_timestamps[p])
            for p in possible_matches
            if osm_geoms[p].intersects(geom)
        ]

        if not relevant_osm:
            filtered_indices.append(orig_idx)
            continue

        best_diff, best_coverage_grb, beste_osm_datum, best_osm_area = float('inf'), 0.0, None, 0.0

        for match_geom, match_ts in relevant_osm:
            if match_geom.area == 0:
                continue
            intersection_area = geom.intersection(match_geom).area
            coverage = intersection_area / grb_area
            if coverage < threshold:
                continue
            diff = abs(match_geom.area - grb_area)
            if diff < best_diff:
                best_diff, best_osm_area, best_coverage_grb = diff, match_geom.area, coverage
                if match_ts:
                    dt = pd.to_datetime(match_ts, errors='coerce')
                    beste_osm_datum = dt.tz_localize(None) if pd.notna(dt) and hasattr(dt, 'tzinfo') and dt.tzinfo is not None else dt

        if best_coverage_grb == 0.0:
            filtered_indices.append(orig_idx)
            continue

        is_nagenoeg_gelijk = (abs(grb_area - best_osm_area) / best_osm_area) < 0.02 if best_osm_area > 0 else False

        grb_is_recenter = False
        if pd.notna(grb_datum) and pd.notna(beste_osm_datum):
            grb_is_recenter = grb_datum > beste_osm_datum

        if is_nagenoeg_gelijk:
            if grb_is_recenter:
                filtered_indices.append(orig_idx)
        else:
            if grb_is_recenter or pd.isna(beste_osm_datum) or pd.isna(grb_datum):
                filtered_indices.append(orig_idx)

    return gdf_grb.loc[filtered_indices].copy()

def verwerk_topologie_en_simplificatie(gekoppeld, bbox_wgs84, config):
    t_start = time.perf_counter()
    t_stap = t_start

    if gekoppeld.empty:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    gekoppeld = gekoppeld.copy()
    gekoppeld['geometry'] = set_precision(gekoppeld.geometry.values, grid_size=0.001)

    log_timer(config, f"      - Topo | set_precision: {time.perf_counter() - t_stap:.2f}s")
    t_stap = time.perf_counter()

    TOLERANCE_METERS = config.get('topology', {}).get('tolerance_meters', 0.05)
    GEWENSTE_STAP_METER = config.get('topology', {}).get('gewenste_stap_meter', 0.01)

    OFFSET_X = 500000.0
    OFFSET_Y = 5200000.0

    minx, miny, maxx, maxy = gekoppeld.total_bounds

    minx_gesnapt = OFFSET_X + math.floor((minx - OFFSET_X) / GEWENSTE_STAP_METER) * GEWENSTE_STAP_METER
    miny_gesnapt = OFFSET_Y + math.floor((miny - OFFSET_Y) / GEWENSTE_STAP_METER) * GEWENSTE_STAP_METER
    maxx_gesnapt = OFFSET_X + math.ceil((maxx - OFFSET_X) / GEWENSTE_STAP_METER) * GEWENSTE_STAP_METER
    maxy_gesnapt = OFFSET_Y + math.ceil((maxy - OFFSET_Y) / GEWENSTE_STAP_METER) * GEWENSTE_STAP_METER

    breedte = maxx_gesnapt - minx_gesnapt
    hoogte = maxy_gesnapt - miny_gesnapt
    langste_zijde = max(breedte, hoogte)

    maxx_gesnapt = minx_gesnapt + langste_zijde
    maxy_gesnapt = miny_gesnapt + langste_zijde

    quant = int(langste_zijde / GEWENSTE_STAP_METER) if langste_zijde > 0 else 1000

    # --- VEILIGHEIDSMAATREGEL: BEWAAR ALLE ATTRIBUTEN APART ---
    gekoppeld['temp_topo_id'] = range(len(gekoppeld))

    # Bewaar alle niet-geometrische kolommen (inclusief addr:*, OIDN, etc.)
    attributen_df = gekoppeld.drop(columns=['geometry'], errors='ignore').copy()

    # Geef enkel de geometrie en de unieke ID mee aan TopoJSON
    gekoppeld_geometrie_only = gekoppeld[['temp_topo_id', 'geometry']].copy()
    dummy_poly = box(minx_gesnapt, miny_gesnapt, maxx_gesnapt, maxy_gesnapt)

    dummy_row = gpd.GeoDataFrame([{'temp_topo_id': -1, 'geometry': dummy_poly, 'is_dummy': True}], crs=gekoppeld.crs)
    gekoppeld_geometrie_only['is_dummy'] = False

    gekoppeld_gecombineerd = pd.concat([gekoppeld_geometrie_only, dummy_row], ignore_index=True)

    # TopoJSON verwerking puur op geometrie
    topo = tp.Topology(gekoppeld_gecombineerd, prequantize=quant).toposimplify(TOLERANCE_METERS).to_gdf()

    topo = topo[topo['is_dummy'] != True].copy()
    topo['geometry'] = topo['geometry'].buffer(0)
    topo = test_snap_naar_buren(topo, snap_tolerance=0.01)

    log_timer(config, f"      - Topo | buffer & snap_naar_buren: {time.perf_counter() - t_stap:.2f}s")
    t_stap = time.perf_counter()

    # --- KOPPEL ALLE ATTRIBUTEN WEER TERUG VIA 'temp_topo_id' ---
    if 'temp_topo_id' in topo.columns:
        topo['temp_topo_id'] = topo['temp_topo_id'].astype(int)
        # Merge de versimpelde geometrieën terug op de originele attributentabel
        gdf_merged = attributen_df.merge(topo[['temp_topo_id', 'geometry']], on='temp_topo_id', how='inner', suffixes=('', '_topo'))
        if 'geometry_topo' in gdf_merged.columns:
            gdf_merged['geometry'] = gdf_merged['geometry_topo']
            gdf_merged = gdf_merged.drop(columns=['geometry_topo'], errors='ignore')
        gdf_simplified = gpd.GeoDataFrame(gdf_merged, geometry='geometry', crs=gekoppeld.crs)
    else:
        # Fallback als TopoJSON de ID-kolom mocht strippen
        topo.index = gekoppeld.index[:len(topo)]
        attributen_df_subset = attributen_df.loc[topo.index].copy()
        attributen_df_subset['geometry'] = topo['geometry']
        gdf_simplified = gpd.GeoDataFrame(attributen_df_subset, geometry='geometry', crs=gekoppeld.crs)

    gdf_simplified = gdf_simplified.to_crs("EPSG:4326")
    box_wgs84 = box(*bbox_wgs84)
    result = gdf_simplified[gdf_simplified.geometry.intersects(box_wgs84)].copy()

    log_timer(config, f"      - Topo | reprojection & bbox filter: {time.perf_counter() - t_stap:.2f}s")
    log_timer(config, f"    -> Totaal Topologie & simplificatie duur: {time.perf_counter() - t_start:.2f}s")

    return result

def verwerk_grb_data(grb_lagen, osm_ctx, config, args):
    """Combineert filtering, adreskoppeling en simplificatie."""
    t_start = time.perf_counter()
    t_stap = t_start

    gdf_poly = grb_lagen['gbg'].reset_index(drop=True)
    gdf_gba = grb_lagen['gba']
    gdf_knw = grb_lagen['knw'].reset_index(drop=True) if not grb_lagen['knw'].empty else grb_lagen['knw']
    gdf_addr = grb_lagen['adres']
    gdf_wsm = grb_lagen['wegsegment']

    if gdf_poly.empty:
        print("Geen gebouwen gevonden in GRB.", file=sys.stderr)
        sys.exit(0)

    if not gdf_gba.empty and 'TYPE' in gdf_gba.columns:
        gdf_gba['TYPE'] = pd.to_numeric(gdf_gba['TYPE'], errors='coerce')
        gdf_gba = gdf_gba[gdf_gba['TYPE'] != 6]

    if not gdf_knw.empty and 'TYPE' in gdf_knw.columns:
        gdf_knw['TYPE'] = pd.to_numeric(gdf_knw['TYPE'], errors='coerce')
        gdf_knw = gdf_knw[~gdf_knw['TYPE'].isin([4, 5, 6, 12, 14, 22, 23, 24])]

    mapping_columns = {}
    for col in ["HUISNR", "huisnummer", "Huisnummer"]:
        if col in gdf_addr.columns: mapping_columns[col] = "addr:housenumber"
    for col in ["STRAATNM", "straatnaam", "Straatnaam"]:
        if col in gdf_addr.columns: mapping_columns[col] = "addr:street"

    sub_addr = gdf_addr[[c for c in mapping_columns.keys()] + ['geometry']].rename(columns=mapping_columns)

    te_koppelen_objecten = pd.concat([gdf_poly, gdf_knw], ignore_index=True) if not gdf_knw.empty else gdf_poly

    log_timer(config, f"    - Voorbereiding & filtering data: {time.perf_counter() - t_stap:.1f}s")
    t_stap = time.perf_counter()

    import_losse_knopen = config.get('addressing', {}).get('import_loose_address_nodes', False)
    gekoppeld, extra_address_nodes = koppel_adressen(
        gdf_poly=te_koppelen_objecten,
        sub_addr=sub_addr,
        gdf_wsm=gdf_wsm,
        existing_osm_addresses=osm_ctx.existing_addresses_buffered_3812,
        import_loose_address_nodes=import_losse_knopen
    )

    if not gdf_gba.empty:
        gekoppeld = pd.concat([gekoppeld, gdf_gba], ignore_index=True)

    log_timer(config, f"    - Adrestoekenning (`koppel_adressen`): {time.perf_counter() - t_stap:.1f}s")
    t_stap = time.perf_counter()

    gekoppeld['wkt'] = gekoppeld['geometry'].apply(lambda g: g.wkt if g else "")
    gekoppeld = gekoppeld.drop_duplicates(subset=['wkt'], keep='first').drop(columns=['wkt'])
    gekoppeld['geometry'] = gekoppeld['geometry'].buffer(0)
    gekoppeld = gekoppeld.explode(index_parts=False).reset_index(drop=True)
    gekoppeld = gekoppeld[gekoppeld.geometry.area >= 1.0]

    log_timer(config, f"    - Geometrie opschonen & ontdubbelen: {time.perf_counter() - t_stap:.1f}s")
    t_stap = time.perf_counter()

    filtering_actief = config.get('filtering', {}).get('active', True) and not args.fno
    if filtering_actief:
        gekoppeld = filter_existing_gdf(gekoppeld, osm_ctx.existing_polys_3812, threshold=config.get('filtering', {}).get('overlap_threshold', 0.95))

    log_timer(config, f"    - Filtering tegen bestaande OSM: {time.perf_counter() - t_stap:.1f}s")
    t_stap = time.perf_counter()

    gdf_simplified = verwerk_topologie_en_simplificatie(gekoppeld, osm_ctx.bbox_wgs84, config)

    log_timer(config, f"    - Topologie & simplificatie: {time.perf_counter() - t_stap:.1f}s")
    log_timer(config, f"  -> Totaal Transform duur: {time.perf_counter() - t_start:.1f}s")

    return gdf_simplified, extra_address_nodes

# ==============================================================================
# 4. TAGGING & MAPPING (GBG / GBA / KNW)
# ==============================================================================

def verwerk_gba_attributen(props):
    new_props = {}
    gba_type = props.get('TYPE') or props.get('type')
    try:
        gba_type_int = int(float(str(gba_type))) if gba_type is not None else None
    except (ValueError, TypeError):
        gba_type_int = None

    if gba_type_int == 2: new_props['building'] = 'roof'
    elif gba_type_int == 3: new_props.update({'building:part': 'corridor', 'building:min_level': '1', 'building:levels': '2'})
    elif gba_type_int == 1: new_props.update({'building:part': 'yes', 'building:min_level': '1', 'building:levels': '2'})
    elif gba_type_int == 4: new_props['building:part'] = 'steps'
    else: new_props['building:part'] = 'yes'

    return new_props

def verwerk_knw_attributen(props):
    knw_type = str(props.get('LBLTYPE', '')).lower()
    if not knw_type or knw_type == 'none' or knw_type == 'nan':
        return None

    new_props = {}
    if knw_type == 'cabine':
        new_props['building'] = 'service'
    elif knw_type.startswith('silo'):
        new_props['building'] = 'silo'
    elif knw_type.startswith('watertoren'):
        new_props['man_made'] = 'water_tower'
    else:
        return None

    return new_props

def ken_osm_tags_toe(gdf_simplified, osm_ctx):
    features_out = []

    for idx, row in gdf_simplified.iterrows():
        props = row.to_dict()
        geom_obj = props.pop('geometry', None)
        if not geom_obj or geom_obj.is_empty:
            continue

        date_val = str(props.get('VERSDATUM') or props.get('begindatum') or '')[:10]

        grb_laag_raw = str(props.get('grb_layer', 'Gbg')).strip()
        if grb_laag_raw.lower() == 'gba':
            prefix = 'Gba'
        elif grb_laag_raw.lower() == 'knw':
            prefix = 'Knw'
        else:
            prefix = 'Gbg'

        grb_laag = grb_laag_raw.upper()

        oidn_val = props.get('OIDN') or props.get('id')
        clean_id = None
        if oidn_val is not None:
            try:
                clean_id = int(float(str(oidn_val)))
            except (ValueError, TypeError):
                clean_id = oidn_val

        ref_val = f"{prefix}/{clean_id}" if clean_id is not None else None

        # Controleer of er een huisnummer aanwezig is (check zowel dubbelepunt als underscore varianten)
        housenumber = props.get('addr:housenumber') or props.get('addr_housenumber')
        heeft_adres = bool(housenumber and str(housenumber).strip() and str(housenumber).lower() not in ["nan", "none"])
        in_farmyard = any(fy.intersects(geom_obj) for fy in osm_ctx.existing_farmyards_wgs84)

        new_props = None

        if grb_laag == 'GBA' or prefix == 'Gba':
            new_props = verwerk_gba_attributen(props)

        elif grb_laag == 'KNW' or prefix == 'Knw':
            new_props = verwerk_knw_attributen(props)
            if new_props is None:
                continue

            for key, val in props.items():
                clean_key = key.replace('_', ':') if key.startswith('addr_') else key
                if clean_key.startswith('addr:') and val is not None and str(val).strip() != "" and str(val).lower() not in ["nan", "none"]:
                    new_props[clean_key] = str(val)

            fixme_val = props.get('fixme')
            if fixme_val is not None and str(fixme_val).strip() != "" and str(fixme_val).lower() not in ["nan", "none"]:
                new_props['fixme'] = str(fixme_val)
                print(f"[DEBUG TAGGING] Adres succesvol toegevoegd: {key} = {val}")  # <-- Voeg dit toe

        else: # GBG
            new_props = {}
            new_props['building'] = ('farm' if heeft_adres else 'farm_auxiliary') if in_farmyard else ('house' if heeft_adres else 'yes')

            for key, val in props.items():
                clean_key = key.replace('_', ':') if key.startswith('addr_') else key
                if clean_key.startswith('addr:') and val is not None and str(val).strip() != "" and str(val).lower() not in ["nan", "none"]:
                    new_props[clean_key] = str(val)

            fixme_val = props.get('fixme')
            if fixme_val is not None and str(fixme_val).strip() != "" and str(fixme_val).lower() not in ["nan", "none"]:
                new_props['fixme'] = str(fixme_val)

        if date_val: new_props['source:geometry:date'] = date_val
        if ref_val: new_props['source:geometry:ref'] = ref_val

        features_out.append({
            "type": "Feature",
            "properties": new_props,
            "geometry": mapping(geom_obj)
        })

    return features_out


# ==============================================================================
# 5. EXPORT & JOSM INTEGRATIE
# ==============================================================================

def exporteer_verouderde_osm_zoekstring(osm_polys_met_meta, gdf_grb, output_path="to_check.txt", max_overlap_ratio=0.10, min_leeftijd_jaar=3, config=None):
    t_start = time.perf_counter()
    t_stap = t_start

    if not osm_polys_met_meta or gdf_grb.empty:
        return

    BESCHERMDE_BUILDINGS = {'water_tower', 'bunker', 'bridge'}
    BESCHERMDE_KEYS = {'man_made', 'historic', 'tourism', 'natural'}

    osm_records = [
        {'geometry': op['geom'], 'osm_id': op.get('osm_id'), 'timestamp': op.get('timestamp')}
        for op in osm_polys_met_meta
        if 'building' in op.get('tags', {}) and op.get('tags', {}).get('building') not in BESCHERMDE_BUILDINGS and not any(k in op.get('tags', {}) for k in BESCHERMDE_KEYS)
    ]

    if not osm_records:
        return

    osm_gdf = gpd.GeoDataFrame(osm_records, crs="EPSG:3812")
    if gdf_grb.crs != "EPSG:3812":
        gdf_grb = gdf_grb.to_crs("EPSG:3812")

    osm_gdf['area'] = osm_gdf.geometry.area
    osm_gdf = osm_gdf[osm_gdf['area'] > 0].copy()

    huidige_tijd = pd.Timestamp.now()
    leeftijd_grens_dagen = min_leeftijd_jaar * 365.25

    def is_oud_genoeg(ts_val):
        if pd.isna(ts_val): return True
        ts = pd.to_datetime(ts_val, errors='coerce')
        if pd.isna(ts): return True
        return (huidige_tijd - (ts.tz_localize(None) if hasattr(ts, 'tzinfo') and ts.tzinfo is not None else ts)).days >= leeftijd_grens_dagen

    osm_gdf['is_oud'] = osm_gdf['timestamp'].apply(is_oud_genoeg)
    oude_panden = osm_gdf[osm_gdf['is_oud']].copy()

    log_timer(config, f"    - Export setup & leeftijd filtering ({len(oude_panden)} van {len(osm_gdf)} te controleren): {time.perf_counter() - t_stap:.1f}s")
    t_stap = time.perf_counter()

    sindex = gdf_grb.sindex
    overlap_ratios = []

    for row in oude_panden.itertuples():
        osm_geom = row.geometry
        osm_area = row.area

        possible_matches_index = list(sindex.intersection(osm_geom.bounds))
        possible_matches = gdf_grb.geometry.iloc[possible_matches_index]
        intersecting_geoms = possible_matches[possible_matches.intersects(osm_geom)]

        if not intersecting_geoms.empty:
            overlap_area = intersecting_geoms.intersection(osm_geom).area.sum()
            overlap_ratios.append(overlap_area / osm_area)
        else:
            overlap_ratios.append(0.0)

    oude_panden['overlap_ratio'] = overlap_ratios

    log_timer(config, f"    - Intersecties & overlap via sindex: {time.perf_counter() - t_stap:.1f}s")
    t_stap = time.perf_counter()

    wees_panden = oude_panden[oude_panden['overlap_ratio'] < max_overlap_ratio]
    ids_schoon = list(dict.fromkeys(["".join(filter(str.isdigit, str(oid))) for oid in wees_panden['osm_id'].dropna() if str(oid)]))

    if ids_schoon:
        zoek_string = "type:way (" + " or ".join([f"id:{oid}" for oid in ids_schoon]) + ")"
        with open(output_path, "w") as f:
            f.write(zoek_string)
        print(f"JOSM-zoekstring voor {len(ids_schoon)} verouderde wees-panden opgeslagen in '{output_path}'.")

    log_timer(config, f"    - Leeftijd filtering & export: {time.perf_counter() - t_stap:.1f}s")
    log_timer(config, f"  -> Totaal Export duur: {time.perf_counter() - t_start:.1f}s")

def exporteer_geojson(features_out, extra_address_nodes, osm_bestand):

    if extra_address_nodes:
        print(f"--> {len(extra_address_nodes)} extra samengevoegde adresnode(s) aangemaakt.")
        gdf_extra = gpd.GeoDataFrame(extra_address_nodes, crs="EPSG:3812").to_crs("EPSG:4326")
        for row in gdf_extra.itertuples():
            geom = getattr(row, 'geometry')
            if geom and not geom.is_empty:
                ext_props = {
                    "addr:street": getattr(row, 'addr_street', getattr(row, 'addr:street', '')),
                    "addr:housenumber": getattr(row, 'addr_housenumber', getattr(row, 'addr:housenumber', ''))
                }
                fixme_val = getattr(row, 'fixme', None)
                if pd.notna(fixme_val):
                    ext_props['fixme'] = fixme_val
                features_out.append({"type": "Feature", "properties": ext_props, "geometry": mapping(geom)})

    nieuwe_adres_nodes = []
    for feat in features_out:
        props = feat.get("properties", {})
        geom_dict = feat.get("geometry")

        n_indices = sorted([int(re.match(r'^addr:(\d+):street$', k).group(1))
                            for k in list(props.keys()) if re.match(r'^addr:(\d+):street$', k)])

        if n_indices and geom_dict:
            try:
                poly_geom = shape(geom_dict)
                base_pt = poly_geom.representative_point()

                total_extra = len(n_indices)
                offset_deg = 0.00003

                for idx, n in enumerate(n_indices):
                    s_key = f'addr:{n}:street'
                    h_key = f'addr:{n}:housenumber'

                    street_val = props.pop(s_key, None)
                    housenumber_val = props.pop(h_key, None)

                    if street_val and housenumber_val:
                        if total_extra == 1:
                            offset_pt = Point(base_pt.x + offset_deg, base_pt.y)
                        else:
                            angle = (2 * math.pi * idx) / total_extra
                            dx = offset_deg * math.cos(angle)
                            dy = offset_deg * math.sin(angle)
                            offset_pt = Point(base_pt.x + dx, base_pt.y + dy)

                        node_props = {
                            "addr:street": str(street_val).strip(),
                            "addr:housenumber": str(housenumber_val).strip()
                        }
                        nieuwe_adres_nodes.append({
                            "type": "Feature",
                            "properties": node_props,
                            "geometry": mapping(offset_pt)
                        })
            except Exception as e:
                print(f"Waarschuwing: kon geen verspreide adresnode afsplitsen: {e}")

    if nieuwe_adres_nodes:
        print(f"--> {len(nieuwe_adres_nodes)} genummerde nevenadres(sen) afgesplitst in WGS84.")
        features_out.extend(nieuwe_adres_nodes)

    output_geojson = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features_out
    }

    input_dir = os.path.dirname(osm_bestand) if os.path.dirname(osm_bestand) else "."
    base_filename = os.path.splitext(os.path.basename(osm_bestand))[0]
    output_filepath = os.path.join(input_dir, f"{base_filename}_grb.geojson")

    with open(output_filepath, 'w', encoding='utf-8') as f:
        json.dump(output_geojson, f, ensure_ascii=False, indent=2)

    return output_filepath

def open_in_josm(output_filepath):
    print("Bezig met openen in JOSM...")
    try:
        abs_output_path = os.path.abspath(output_filepath)
        full_url = f"http://127.0.0.1:8111/open_file?{urllib.parse.urlencode({'filename': abs_output_path})}"
        with urllib.request.urlopen(full_url, timeout=3.0) as response:
            if response.status == 200:
                print("Succes! Het bestand is geopend in JOSM.")
    except Exception as e:
        print(f"Kon JOSM Remote Control niet bereiken: {e}", file=sys.stderr)

# ==============================================================================
# 6. HOOFDPROGRAMMA (PIJPLIJN)
# ==============================================================================

def main():

    parser = argparse.ArgumentParser(description="Leest een JOSM .osm-bestand en haalt GRB-gebouwen op.")
    parser.add_argument("osm_bestand", help="Pad naar het JOSM .osm-bestand")
    parser.add_argument("-fno", action='store_true', help="Schakel filtering uit (overrules config).")
    parser.add_argument("--config", default="tuning.yaml", help="Pad naar het YAML-configuratiebestand.")
    args = parser.parse_args()

    if not os.path.exists(args.osm_bestand):
        print(f"Fout: Het bestand '{args.osm_bestand}' is niet gevonden.", file=sys.stderr)
        sys.exit(1)

    t_start_totaal = time.perf_counter()
    t_stap = t_start_totaal

    print("=== Start import pipeline ===")

    # 1. EXTRACT
    config = laad_configuratie(args.config)
    osm_ctx = OsmContext(args.osm_bestand)
    grb_lagen = haal_alle_grb_lagen_op(osm_ctx.bbox_3812)

    duur = time.perf_counter() - t_stap
    log_timer(config, f"  -> 1. Extract (config, OSM context & GRB lagen ophalen) voltooid in {duur:.1f}s")
    t_stap = time.perf_counter()

    # 2. TRANSFORM
    gdf_simplified, extra_address_nodes = verwerk_grb_data(grb_lagen, osm_ctx, config, args)

    duur = time.perf_counter() - t_stap
    log_timer(config, f"  -> 2. Transform (GRB data verwerking & filtering) voltooid in {duur:.1f}s")
    t_stap = time.perf_counter()

    # 3. TAGGING
    features_out = ken_osm_tags_toe(gdf_simplified, osm_ctx)

    duur = time.perf_counter() - t_stap
    log_timer(config, f"  -> 3. Tagging voltooid in {duur:.1f}s")
    t_stap = time.perf_counter()

    # 4. EXPORT
    exporteer_verouderde_osm_zoekstring(osm_ctx.existing_polys_3812, grb_lagen['gbg'], output_path="to_check.txt", config=config)
    output_filepath = exporteer_geojson(features_out, extra_address_nodes, args.osm_bestand)

    duur = time.perf_counter() - t_stap
    log_timer(config, f"  -> 4. Export (GeoJSON & checks) voltooid in {duur:.1f}s")

    print(f"\nSucces! Het bestand is gegenereerd op basis van de bounding box uit {args.osm_bestand}.")
    print(f"Klaar voor JOSM: {output_filepath}")

    totaal_duur = time.perf_counter() - t_start_totaal
    log_timer(config, f"\nSucces! Totale uitvoeringstijd: {totaal_duur:.1f}s")

    open_in_josm(output_filepath)

if __name__ == "__main__":
    main()
