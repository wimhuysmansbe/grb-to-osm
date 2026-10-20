import argparse
import sys
import os
import json
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
import geopandas as gpd
import pandas as pd
import numpy as np
import logging
import re
import yaml
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

def test_snap_naar_buren(gdf, snap_tolerance=0.01):
    """
    Trekt knopen die binnen `snap_tolerance` (bijv. 1 cm = 0.01m) van een buurgeometrie liggen
    exact vast op de lijn of knoop van die buur.
    """
    if gdf.empty:
        return gdf

    gdf = gdf.copy().reset_index(drop=True)
    geoms = gdf.geometry.tolist()
    sindex = gdf.sindex

    for i in range(len(geoms)):
        geom = geoms[i]
        if geom is None or geom.is_empty:
            continue

        # Zoek nabijgelegen buren via de bounding box index
        possible_matches = list(sindex.intersection(geom.bounds))

        for j in possible_matches:
            if i == j:
                continue

            other_geom = geoms[j]
            if other_geom is None or other_geom.is_empty:
                continue

            # Controleer of de geometrieën dicht genoeg bij elkaar liggen om te snappen
            if geom.distance(other_geom) <= snap_tolerance:
                # Snap geom naar de contouren van other_geom
                geoms[i] = snap(geoms[i], other_geom, tolerance=snap_tolerance)

    gdf.geometry = geoms
    # Herstel eventuele micro-zelfintersecties na het verschuiven van knopen
    gdf['geometry'] = gdf['geometry'].buffer(0)
    return gdf



# ==============================================================================
# 2. EXTRACTIE & PARSING (OSM EN WFS)
# ==============================================================================

class OsmContext:
    """Enkele centrale container die het .osm bestand 1x parseert."""
    def __init__(self, osm_pad):
        self.osm_pad = osm_pad
        self.bbox_wgs84 = None
        self.bbox_3812 = None
        self.existing_polys_3812 = []
        self.existing_farmyards_wgs84 = []
        self.existing_addresses_buffered_3812 = []

        self._parse_osm()

    def _parse_osm(self):
        try:
            tree = ET.parse(self.osm_pad)
            root = tree.getroot()

            # 1. Bounding Box
            bounds = root.find('bounds')
            if bounds is None:
                raise ValueError(f"Geen <bounds> gevonden in {self.osm_pad}")

            minlat, minlon = float(bounds.get('minlat')), float(bounds.get('minlon'))
            maxlat, maxlon = float(bounds.get('maxlat')), float(bounds.get('maxlon'))
            self.bbox_wgs84 = (minlon, minlat, maxlon, maxlat)

            to_lambert = Transformer.from_crs('EPSG:4326', 'EPSG:3812', always_xy=True)
            minx, miny = to_lambert.transform(minlon, minlat)
            maxx, maxy = to_lambert.transform(maxlon, maxlat)
            buffer = 100.0
            self.bbox_3812 = (minx - buffer, miny - buffer, maxx + buffer, maxy + buffer)

            # 2. Nodes inlezen
            nodes = {}
            node_tags = {}
            for node in root.findall('node'):
                nid = node.get('id')
                lat_str = node.get('lat')
                lon_str = node.get('lon')

                # Sla nodes over als coördinaten ontbreken
                if lat_str is None or lon_str is None:
                    continue

                try:
                    lat, lon = float(lat_str), float(lon_str)
                except ValueError:
                    continue  # Sla nodes over met ongeldige numerieke waarden

                nodes[nid] = (lon, lat)
                tags = {t.get('k'): t.get('v') for t in node.findall('tag')}
                if tags:
                    node_tags[nid] = tags

            # 3. Ways inlezen
            ways = {}
            way_meta = {}
            way_tags = {}
            for way in root.findall('way'):
                wid = way.get('id')
                nds = [nd.get('ref') for nd in way.findall('nd') if nd.get('ref') in nodes]
                coords = [nodes[ref] for ref in nds]
                if len(coords) >= 2:
                    ways[wid] = coords
                    way_meta[wid] = way.get('timestamp')
                    way_tags[wid] = {t.get('k'): t.get('v') for t in way.findall('tag')}

            # 4. Filter bestaande gebouwen/parts
            polys_meta = []
            farmyard_polys = []
            addr_geoms = []

            # Check nodes op adressen
            for nid, tags in node_tags.items():
                if 'addr:housenumber' in tags:
                    addr_geoms.append(shape({"type": "Point", "coordinates": nodes[nid]}))

            for wid, coords in ways.items():
                wtags = way_tags.get(wid, {})

                # Check Farmyards
                if wtags.get('landuse') == 'farmyard':
                    if len(coords) >= 4 and coords[0] == coords[-1]:
                        farmyard_polys.append(Polygon(coords))

                # Check Adressen op Ways
                if 'addr:housenumber' in wtags:
                    if len(coords) >= 4 and coords[0] == coords[-1]:
                        addr_geoms.append(Polygon(coords))
                    else:
                        addr_geoms.append(shape({"type": "LineString", "coordinates": coords}))

                # Check Gebouwen
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

            # Relations voor Multipolygons
            for rel in root.findall('relation'):
                rel_id = rel.get('id')
                rel_timestamp = rel.get('timestamp')
                tags = {t.get('k'): t.get('v') for t in rel.findall('tag')}
                if any(k.startswith('building') for k in tags):
                    outer_linestrings = []
                    for member in rel.findall('member'):
                        if member.get('type') == 'way' and member.get('ref') in ways:
                            outer_linestrings.append(shape({"type": "LineString", "coordinates": ways[member.get('ref')]}))
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

            # Zet om naar gegeefde dataframes / lijsten in gewenste CRS
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
        # Start alle 5 downloads parallel
        future_to_key = {
            executor.submit(haal_wfs_data_op, typename, bbox_3812): key
            for key, typename in lagen.items()
        }
        for future in future_to_key:
            key = future_to_key[future]
            resultaten[key] = future.result()

    # WFS-identificator tags toevoegen
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
    for idx, row in gdf_grb.iterrows():
        geom = row['geometry']
        if not geom or geom.is_empty or geom.area == 0:
            continue
        grb_area = geom.area
        grb_datum = pd.to_datetime(row.get('VERSDATUM') or row.get('begindatum'), errors='coerce')

        possible_matches = list(osm_gdf.sindex.query(geom, predicate="intersects"))
        relevant_osm = [(osm_gdf.iloc[p]['geometry'], osm_gdf.iloc[p]['timestamp']) for p in possible_matches if osm_gdf.iloc[p]['geometry'].intersects(geom)]

        if not relevant_osm:
            filtered_indices.append(idx)
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
                    beste_osm_datum = dt.tz_localize(None) if pd.notna(dt) and dt.tzinfo else dt

        if best_coverage_grb == 0.0:
            filtered_indices.append(idx)
            continue

        is_nagenoeg_gelijk = (abs(grb_area - best_osm_area) / best_osm_area) < 0.02 if best_osm_area > 0 else False
        grb_is_recenter = (grb_datum > beste_osm_datum) if (pd.notna(grb_datum) and pd.notna(beste_osm_datum)) else False

        if is_nagenoeg_gelijk:
            if grb_is_recenter:
                filtered_indices.append(idx)
        else:
            if not (beste_osm_datum and pd.notna(grb_datum) and not grb_is_recenter and grb_area >= best_osm_area):
                filtered_indices.append(idx)

    return gdf_grb.loc[filtered_indices].copy()


def verwerk_topologie_en_simplificatie(gekoppeld, bbox_wgs84, config):
    if gekoppeld.empty:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    gekoppeld = gekoppeld.copy()
    gekoppeld['geometry'] = gekoppeld['geometry'].apply(lambda geom: set_precision(geom, grid_size=0.001))

    TOLERANCE_METERS = config.get('topology', {}).get('tolerance_meters', 0.05)
    GEWENSTE_STAP_METER = config.get('topology', {}).get('gewenste_stap_meter', 0.01)

    total_bounds = gekoppeld.total_bounds
    langste_zijde = max(total_bounds[2] - total_bounds[0], total_bounds[3] - total_bounds[1])
    quant = int(langste_zijde / GEWENSTE_STAP_METER) if langste_zijde > 0 else 1000

    topo = tp.Topology(gekoppeld, prequantize=quant).toposimplify(TOLERANCE_METERS).to_gdf()
    topo.index = gekoppeld.index
    topo['geometry'] = topo['geometry'].buffer(0)
    topo = test_snap_naar_buren(topo, snap_tolerance=0.02)

    gdf_simplified = topo.to_crs("EPSG:4326")
    box_wgs84 = box(*bbox_wgs84)
    return gdf_simplified[gdf_simplified.geometry.intersects(box_wgs84)].copy()


def verwerk_grb_data(grb_lagen, osm_ctx, config, args):
    """Combineert filtering, adreskoppeling en simplificatie."""
    gdf_poly = grb_lagen['gbg'].reset_index(drop=True)
    gdf_gba = grb_lagen['gba']
    gdf_knw = grb_lagen['knw'].reset_index(drop=True) if not grb_lagen['knw'].empty else grb_lagen['knw']
    gdf_addr = grb_lagen['adres']
    gdf_wsm = grb_lagen['wegsegment']

    if gdf_poly.empty:
        print("Geen gebouwen gevonden in GRB.", file=sys.stderr)
        sys.exit(0)

    # Filter GBA & KNW op type
    if not gdf_gba.empty and 'TYPE' in gdf_gba.columns:
        gdf_gba['TYPE'] = pd.to_numeric(gdf_gba['TYPE'], errors='coerce')
        gdf_gba = gdf_gba[gdf_gba['TYPE'] != 6]

    if not gdf_knw.empty and 'TYPE' in gdf_knw.columns:
        gdf_knw['TYPE'] = pd.to_numeric(gdf_knw['TYPE'], errors='coerce')
        gdf_knw = gdf_knw[~gdf_knw['TYPE'].isin([4, 5, 6, 12, 14, 22, 23, 24])]

    # Prepareer adressen
    mapping_columns = {}
    for col in ["HUISNR", "huisnummer", "Huisnummer"]:
        if col in gdf_addr.columns: mapping_columns[col] = "addr:housenumber"
    for col in ["STRAATNM", "straatnaam", "Straatnaam"]:
        if col in gdf_addr.columns: mapping_columns[col] = "addr:street"

    sub_addr = gdf_addr[[c for c in mapping_columns.keys()] + ['geometry']].rename(columns=mapping_columns)

    # Voeg GBG en KNW samen VOOR de adreskoppeling (KNW kan adressen hebben)
    te_koppelen_objecten = pd.concat([gdf_poly, gdf_knw], ignore_index=True) if not gdf_knw.empty else gdf_poly

    import_losse_knopen = config.get('addressing', {}).get('import_loose_address_nodes', False)
    gekoppeld, extra_address_nodes = koppel_adressen(
        gdf_poly=te_koppelen_objecten,
        sub_addr=sub_addr,
        gdf_wsm=gdf_wsm,
        existing_osm_addresses=osm_ctx.existing_addresses_buffered_3812,
        import_loose_address_nodes=import_losse_knopen
    )

    # Voeg GBA toe ZONDER adreskoppeling
    if not gdf_gba.empty:
        gekoppeld = pd.concat([gekoppeld, gdf_gba], ignore_index=True)

    gekoppeld['wkt'] = gekoppeld['geometry'].apply(lambda g: g.wkt if g else "")
    gekoppeld = gekoppeld.drop_duplicates(subset=['wkt'], keep='first').drop(columns=['wkt'])
    gekoppeld['geometry'] = gekoppeld['geometry'].buffer(0)
    gekoppeld = gekoppeld.explode(index_parts=False).reset_index(drop=True)
    gekoppeld = gekoppeld[gekoppeld.geometry.area >= 1.0]

    # Filtering tegen bestaande OSM gebouwen
    filtering_actief = config.get('filtering', {}).get('active', True) and not args.fno
    if filtering_actief:
        gekoppeld = filter_existing_gdf(gekoppeld, osm_ctx.existing_polys_3812, threshold=config.get('filtering', {}).get('overlap_threshold', 0.95))

    # Topologie en vereenvoudiging
    gdf_simplified = verwerk_topologie_en_simplificatie(gekoppeld, osm_ctx.bbox_wgs84, config)

    return gdf_simplified, extra_address_nodes


# ==============================================================================
# 4. TAGGING & MAPPING (GBG / GBA / KNW)
# ==============================================================================

def verwerk_gba_attributen(props):
    new_props = {}
    gba_type = props.get('TYPE') or props.get('type')
    try: gba_type_int = int(gba_type)
    except (ValueError, TypeError): gba_type_int = None

    if gba_type_int == 2: new_props['building'] = 'roof'
    elif gba_type_int == 3: new_props.update({'building:part': 'corridor', 'building:min_level': '1', 'building:levels': '2'})
    elif gba_type_int == 1: new_props.update({'building:part': 'yes', 'building:min_level': '1', 'building:levels': '2'})
    elif gba_type_int == 4: new_props['building:part'] = 'steps'
    else: new_props['building:part'] = 'yes'

    return new_props


def verwerk_knw_attributen(props):
    knw_type = str(props.get('LBLTYPE', '')).lower()
    if not knw_type:
        return None

    new_props = {}
    if knw_type == 'cabine':
        new_props['building'] = 'service'
    elif knw_type.startswith('silo'):
        new_props['building'] = 'silo'
    elif knw_type.startswith('watertoren'):
        new_props['man_made'] = 'water_tower'
    else:
        # Sla ongewenste KNW types over
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
        prefix = props.get('grb_layer', 'Gbg')
        grb_laag = str(props.get('grb_layer', '')).upper()
        oidn_val = props.get('OIDN') or props.get('id')
        ref_val = f"{prefix}/{int(oidn_val)}" if oidn_val is not None and str(oidn_val).isdigit() else (f"Gbg/{oidn_val}" if oidn_val else None)

        housenumber = props.get('addr:housenumber')
        heeft_adres = bool(housenumber and str(housenumber).strip() and str(housenumber).lower() != "nan")
        in_farmyard = any(fy.intersects(geom_obj) for fy in osm_ctx.existing_farmyards_wgs84)

        new_props = None

        if grb_laag == 'GBA' or prefix == 'Gba':
            new_props = verwerk_gba_attributen(props)
            # GBA krijgt bewust GEEN adrestags

        elif grb_laag == 'KNW' or prefix == 'Knw':
            new_props = verwerk_knw_attributen(props)
            if new_props is None:
                continue  # DROPME / Niet ondersteund KNW type

            for osm_tag in ["addr:street", "addr:housenumber", "fixme"]:
                val = props.get(osm_tag)
                if val is not None and str(val).strip() != "" and str(val).lower() != "nan":
                    new_props[osm_tag] = str(val)

        else: # GBG
            new_props = {}
            new_props['building'] = ('farm' if heeft_adres else 'farm_auxiliary') if in_farmyard else ('house' if heeft_adres else 'yes')
            for osm_tag in ["addr:street", "addr:housenumber", "fixme"]:
                val = props.get(osm_tag)
                if val is not None and str(val).strip() != "" and str(val).lower() != "nan":
                    new_props[osm_tag] = str(val)

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

def exporteer_verouderde_osm_zoekstring(osm_polys_met_meta, gdf_grb, output_path="to_check.txt", max_overlap_ratio=0.10, min_leeftijd_jaar=3):
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

    grb_union = unary_union([g for g in gdf_grb.geometry if g and not g.is_empty])
    osm_gdf['area'] = osm_gdf.geometry.area
    osm_gdf = osm_gdf[osm_gdf['area'] > 0].copy()

    intersections = osm_gdf.geometry.intersection(grb_union)
    osm_gdf['overlap_ratio'] = intersections.area / osm_gdf['area']

    huidige_tijd = pd.Timestamp.now()
    leeftijd_grens_dagen = min_leeftijd_jaar * 365.25

    def is_oud_genoeg(ts_val):
        if pd.isna(ts_val): return True
        ts = pd.to_datetime(ts_val, errors='coerce')
        if pd.isna(ts): return True
        return (huidige_tijd - (ts.tz_localize(None) if ts.tzinfo else ts)).days >= leeftijd_grens_dagen

    wees_panden = osm_gdf[(osm_gdf['overlap_ratio'] < max_overlap_ratio) & osm_gdf['timestamp'].apply(is_oud_genoeg)]
    ids_schoon = list(dict.fromkeys(["".join(filter(str.isdigit, str(oid))) for oid in wees_panden['osm_id'].dropna() if str(oid)]))

    if ids_schoon:
        zoek_string = "type:way (" + " or ".join([f"id:{oid}" for oid in ids_schoon]) + ")"
        with open(output_path, "w") as f:
            f.write(zoek_string)
        print(f"JOSM-zoekstring voor {len(ids_schoon)} verouderde wees-panden opgeslagen in '{output_path}'.")


def exporteer_geojson(features_out, extra_address_nodes, osm_bestand):
    if extra_address_nodes:
        print(f"--> {len(extra_address_nodes)} extra samengevoegde adresnode(s) aangemaakt.")
        gdf_extra = gpd.GeoDataFrame(extra_address_nodes, crs="EPSG:3812").to_crs("EPSG:4326")
        for _, ext_row in gdf_extra.iterrows():
            if ext_row['geometry'] and not ext_row['geometry'].is_empty:
                ext_props = {"addr:street": ext_row['addr:street'], "addr:housenumber": ext_row['addr:housenumber']}
                if 'fixme' in ext_row and pd.notna(ext_row['fixme']): ext_props['fixme'] = ext_row['fixme']
                features_out.append({"type": "Feature", "properties": ext_props, "geometry": mapping(ext_row['geometry'])})

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

    # 1. EXTRACT
    config = laad_configuratie(args.config)
    osm_ctx = OsmContext(args.osm_bestand)
    grb_lagen = haal_alle_grb_lagen_op(osm_ctx.bbox_3812)

    # 2. TRANSFORM
    gdf_simplified, extra_address_nodes = verwerk_grb_data(grb_lagen, osm_ctx, config, args)

    # 3. TAGGING
    features_out = ken_osm_tags_toe(gdf_simplified, osm_ctx)

    # 4. EXPORT
    exporteer_verouderde_osm_zoekstring(osm_ctx.existing_polys_3812, grb_lagen['gbg'], output_path="to_check.txt")
    output_filepath = exporteer_geojson(features_out, extra_address_nodes, args.osm_bestand)

    print(f"\nSucces! Het bestand is gegenereerd op basis van de bounding box uit {args.osm_bestand}.")
    print(f"Klaar voor JOSM: {output_filepath}")

    open_in_josm(output_filepath)


if __name__ == "__main__":
    main()
