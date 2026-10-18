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
import numpy as np
import logging
import re
from shapely.geometry import shape, mapping, Polygon, Point, box, MultiPolygon
from shapely.ops import unary_union, polygonize, linemerge
from shapely import set_precision
from collections import Counter
from shapely.validation import explain_validity


# Zet eventueel basic logging aan als dat nog niet zo was
#logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Vormt lange reeksen huisnummers om naar handelbaar formaat
def formatteer_huisnummers(huisnummers):
    if not huisnummers:
        return None

    pure_nums = []
    suffix_dict = {} # bijvoorbeeld: {2: ['A', 'B', 'C', 'D', 'E', 'F']}
    other_vals = []

    for h in huisnummers:
        h_str = str(h).strip()
        # Match bijvoorbeeld "2A" of "12bis"
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

    # 1. Verwerk pure getallen (bijv. 2, 4, 6, 8 -> 2-16)
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

    # 2. Verwerk getallen met suffixes (bijv. 2A t/m 2F -> 2A-2F)
    for num in sorted(suffix_dict.keys()):
        suffixes = sorted(list(set(suffix_dict[num])))
        if len(suffixes) > 1:
            # Check of letters opeenvolgend zijn (A, B, C...)
            is_seq = all(ord(suffixes[i+1]) - ord(suffixes[i]) == 1 for i in range(len(suffixes)-1))
            if is_seq:
                result_parts.append(f"{num}{suffixes[0]}-{num}{suffixes[-1]}")
            else:
                result_parts.append(";".join([f"{num}{s}" for s in suffixes]))
        else:
            result_parts.append(f"{num}{suffixes[0]}")

    # 3. Voeg eventuele vreemde eenden in de bijt toe
    if other_vals:
        result_parts.extend(other_vals)

    return "; ".join(result_parts) if result_parts else "; ".join(map(str, huisnummers))

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
    """Parseert bestaande gebouwen (inclusief multipolygon relaties) uit het JOSM .osm-bestand en transformeert ze naar EPSG:31370."""
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
        for way in root.findall('way'):
            wid = way.get('id')
            nds = [nd.get('ref') for nd in way.findall('nd') if nd.get('ref') in nodes]
            coords = [nodes[ref] for ref in nds]
            if len(coords) >= 2:
                ways[wid] = coords

        polys = []

        # 1. Reguliere ways met building tag
        for way in root.findall('way'):
            wid = way.get('id')
            is_building = False
            for tag in way.findall('tag'):
                if tag.get('k') == 'building':
                    is_building = True
                    break
            if not is_building:
                continue
            if wid in ways:
                coords = ways[wid]
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    try:
                        polys.append(Polygon(coords))
                    except Exception:
                        pass

        # 2. Relaties die gebouwen / multipolygons zijn
        for rel in root.findall('relation'):
            tags = {tag.get('k'): tag.get('v') for tag in rel.findall('tag')}
            is_building_rel = False
            if 'building' in tags or (tags.get('type') == 'multipolygon' and any(k.startswith('building') for k in tags)):
                is_building_rel = True

            if not is_building_rel:
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
                            polys.append(p)
                except Exception:
                    for line in outer_linestrings:
                        coords = list(line.coords)
                        if len(coords) >= 4 and coords[0] == coords[-1]:
                            try:
                                polys.append(Polygon(coords))
                            except Exception:
                                pass

        if not polys:
            return []

        gdf_osm = gpd.GeoDataFrame(geometry=polys, crs="EPSG:4326")
        gdf_osm = gdf_osm.to_crs("EPSG:31370")
        return [geom for geom in gdf_osm.geometry if geom and not geom.is_empty]
    except Exception as e:
        print(f"Waarschuwing bij inlezen bestaande OSM gebouwen: {e}", file=sys.stderr)
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

            if not data.get("features"):
                return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

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
                    "verwijderd dubbele gebouwen, koppelt adressen met prioritering op intersectie en fallback op nabijheid."
    )
    parser.add_argument("osm_bestand", help="Pad naar het JOSM .osm-bestand (bijv. temp.osm)")
    parser.add_argument("-fno", action='store_true', help="Schakel filtering uit; importeer alle GRB-gebouwen zonder te controleren op bestaande OSM-data.")

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
    gdf_addr = haal_wfs_data_op("GRB:Adres", bbox)
    gdf_wsm = haal_wfs_data_op("GRB:Wegsegment", bbox)  # <-- Wegsegmenten ophalen
    if gdf_poly.empty:
        print("Geen gebouwen gevonden in de WFS voor deze bounding box.", file=sys.stderr)
        sys.exit(0)

    print(f"Succesvol {len(gdf_poly)} gebouwen, {len(gdf_addr)} adrespunten en {len(gdf_wsm)} wegsegmenten opgehaald via WFS.")

    if gdf_poly.crs != gdf_addr.crs:
        gdf_addr = gdf_addr.to_crs(gdf_poly.crs)
    if gdf_poly.crs != gdf_wsm.crs:
        gdf_wsm = gdf_wsm.to_crs(gdf_poly.crs)

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

    print("Bezig met slim koppelen en sorteren van adressen aan gebouwen...")
    extra_address_nodes = []

    if not sub_addr.empty and not gdf_poly.empty:
        proj_poly = gdf_poly.to_crs("EPSG:31370").copy()
        proj_poly['orig_index'] = proj_poly.index
        proj_addr = sub_addr.to_crs("EPSG:31370")
        proj_wsm = gdf_wsm.to_crs("EPSG:31370")

        # 1. Directe intersectie: Adressen die binnen/op een gebouw liggen altijd toekennen
        intersect_join = gpd.sjoin(proj_addr, proj_poly, how="inner", predicate="intersects")

        buildings_with_direct = set(intersect_join['orig_index']) if not intersect_join.empty else set()
        matched_addr_indices = set(intersect_join.index) if not intersect_join.empty else set()

        # 2. Resterende losse adrespunten (vallen niet binnen een gebouw)
        unmatched_addr = proj_addr.loc[~proj_addr.index.isin(matched_addr_indices)].copy()

        # Alleen voor losse adrespunten controleren of ze al in OSM aanwezig zijn
        if existing_osm_addresses and not unmatched_addr.empty:
            filtered_unmatched_indices = []
            for idx, row in unmatched_addr.iterrows():
                pt = row['geometry']
                if not pt or pt.is_empty:
                    continue
                reeds_aanwezig = any(buffered_poly.contains(pt) for buffered_poly in existing_osm_addresses)
                if not reeds_aanwezig:
                    filtered_unmatched_indices.append(idx)
            unmatched_addr = unmatched_addr.loc[filtered_unmatched_indices].copy()

        # 3. Fallback voor gebouwen ZONDER direct adres: zoek losse adresnodes in de buurt (max 4.0m)
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

        building_address_records = []
    #MARK
        if not combined_join.empty and 'orig_index' in combined_join.columns:

            # --- STAP: Detecteer dubbele toewijzingen ---
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
            exclusieve_skips = set()

            if dubbele_adressen and proj_wsm is not None and not proj_wsm.empty:
                for (straat, huisnr), gebouw_indices in dubbele_adressen.items():
                    sub_gebouwen = proj_poly.loc[proj_poly['orig_index'].isin(gebouw_indices)]

                    # Gebruik direct de juiste kolomnaam 'LSTRNM' uit het GRB wegsegmentenbestand
                    straat_kolom = 'LSTRNM' if 'LSTRNM' in proj_wsm.columns else next((col for col in ['STRAATNM', 'straatnaam', 'Straatnaam', 'NAAM', 'naam'] if col in proj_wsm.columns), None)

                    specifieke_segmenten = proj_wsm
                    if straat_kolom and straat:
                        mask = proj_wsm[straat_kolom].astype(str).str.strip().str.lower() == straat
                        gefilterd = proj_wsm[mask]
                        if not gefilterd.empty:
                            specifieke_segmenten = gefilterd

                    # Bereken de minimale afstand van elk strijdig gebouw tot de segmenten
                    gebouw_afstanden = {}
                    for g_idx, g_row in sub_gebouwen.iterrows():
                        g_geom = g_row['geometry']
                        if g_geom and not g_geom.is_empty:
                            afd = specifieke_segmenten.geometry.distance(g_geom).min()
                            gebouw_afstanden[g_idx] = afd

                    if len(gebouw_afstanden) > 1:
                        min_afd = min(gebouw_afstanden.values())
                        tolerantie_meters = 3.0  # Marge voor hoekpanden

                        for g_idx, afd in gebouw_afstanden.items():
                            # Als de afstand significant groter is dan de dichtstbijzijnde, sla deze over
                            if afd > min_afd + tolerantie_meters:
                                exclusieve_skips.add((g_idx, straat, huisnr))

            # --- STAP: Los gedeelde adressen op die een reekssprong veroorzaken op een gebouw ---
            if not combined_join.empty:
                # 1. Bouw een mapping van (straat, huisnr) -> set van gebouw indices
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

                # Adressen die op MEERDERE gebouwen voorkomen
                gedeelde_adressen = {k for k, v in adres_naar_gebouwen.items() if len(v) > 1}

                if gedeelde_adressen:
                    rows_to_drop = set()

                    # 2. Loop door alle toewijzingen en check of het adres een sprong veroorzaakt op dat gebouw
                    for idx, row in combined_join.iterrows():
                        orig_idx = row['orig_index']
                        straat = str(row.get('addr:street', '')).strip().lower()
                        huisnr = str(row.get('addr:housenumber', '')).strip().lower()
                        key = (straat, huisnr)

                        if key in gedeelde_adressen:
                            # Verzamel alle huisnummer-waarden op DIT specifieke gebouw
                            gebouw_rows = combined_join[combined_join['orig_index'] == orig_idx]
                            gebouw_nums = []
                            for _, g_row in gebouw_rows.iterrows():
                                h_str = str(g_row.get('addr:housenumber', '')).strip()
                                m = re.search(r'\d+', h_str)
                                if m:
                                    gebouw_nums.append(int(m.group()))

                            # Als dit gebouw meerdere getallen heeft, check of dit specifieke getal een 'sprong' (outlier) is
                            if len(gebouw_nums) > 1:
                                h_match = re.search(r'\d+', huisnr)
                                if h_match:
                                    h_num = int(h_match.group())
                                    other_nums = [n for n in gebouw_nums if n != h_num]
                                    if other_nums:
                                        # Kleinste afstand tot de andere getallen op dit gebouw
                                        min_diff = min(abs(h_num - o) for o in other_nums)
                                        # Als de afstand tot de rest groter is dan 2, is het een foute sprong op dit gebouw
                                        if min_diff > 2:
                                            rows_to_drop.add(idx)

                    # Verwijder de foute toewijzing waar de sprong optreedt
                    if rows_to_drop:
                        combined_join = combined_join.drop(index=list(rows_to_drop)).reset_index(drop=True)

            for orig_idx, group in combined_join.groupby('orig_index'):
                straten_raw = [str(s).strip() for s in group['addr:street'].dropna() if str(s).strip() != "" and str(s).lower() != "nan"]

                if not straten_raw:
                    continue

                primair_straat = max(set(straten_raw), key=straten_raw.count)

                groep_primair = group[group['addr:street'] == primair_straat]
                huisnummers_primair = sorted(list({str(h).strip() for h in groep_primair['addr:housenumber'].dropna() if str(h).strip() != ""}), key=sorteer_huisnummer)

                # AANPASSING HIER: Roep de formatteringsfunctie aan
                geformatteerde_nummers = formatteer_huisnummers(huisnummers_primair)

                building_address_records.append({
                    'orig_index': orig_idx,
                    'addr:street': primair_straat,
                    'addr:housenumber': geformatteerde_nummers
                })

                groep_secundair = group[group['addr:street'] != primair_straat]
                if not groep_secundair.empty:
                    for sec_straat, sec_group in groep_secundair.groupby('addr:street'):
                        sec_huisnummers = sorted(list({str(h).strip() for h in sec_group['addr:housenumber'].dropna() if str(h).strip() != ""}), key=sorteer_huisnummer)

                        sec_geom = sec_group['geometry'].iloc[0]

                        # AANPASSING HIER: Ook hier netjes formatteren
                        geformatteerde_sec_nummers = formatteer_huisnummers(sec_huisnummers)

                        if sec_straat and sec_huisnummers and sec_geom and not sec_geom.is_empty:
                            extra_address_nodes.append({
                                'geometry': sec_geom,
                                'addr:street': str(sec_straat),
                                'addr:housenumber': geformatteerde_sec_nummers
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


    if gekoppeld.crs is None or "31370" not in str(gekoppeld.crs.to_string()):
        gekoppeld = gekoppeld.to_crs("EPSG:31370")

    gekoppeld['geometry'] = gekoppeld['geometry'].buffer(0)
    gekoppeld = gekoppeld.explode(index_parts=False).reset_index(drop=True)

    # 1. Filter micro-slivers (< 1 m²)
    drempel_m2 = 1.0
    aantal_voor = len(gekoppeld)
    gekoppeld = gekoppeld[gekoppeld.geometry.area >= drempel_m2]
    aantal_weg = aantal_voor - len(gekoppeld)
    if aantal_weg > 0:
        print(f"--> {aantal_weg} micro-polygoon/slivers (< {drempel_m2} m²) succesvol weggefilterd.")

    # 2. Filter direct op Bounding Box (vóór de zware berekeningen)
    box_wgs84 = box(bbox[0], bbox[1], bbox[2], bbox[3])
    box_gdf = gpd.GeoDataFrame(geometry=[box_wgs84], crs="EPSG:4326").to_crs("EPSG:31370")
    bbox_poly_l72 = box_gdf.geometry.iloc[0]

    aantal_voor_bbox = len(gekoppeld)
    gekoppeld = gekoppeld[gekoppeld.geometry.intersects(bbox_poly_l72)].copy()
    aantal_buiten = aantal_voor_bbox - len(gekoppeld)
    if aantal_buiten > 0:
        print(f"--> {aantal_buiten} gebouw(en) buiten de bounding box weggefilterd.")

    # 3. Filter reeds bestaande OSM-gebouwen
    if not args.fno:
        print("Bezig met filteren van reeds bestaande OSM-gebouwen...")
        existing_osm_polys = get_existing_osm_polys_in_lambert72(args.osm_bestand)
        aantal_voor_filter = len(gekoppeld)
        gekoppeld = filter_existing_gdf(gekoppeld, existing_osm_polys)
        aantal_gefilterd = aantal_voor_filter - len(gekoppeld)
        if aantal_gefilterd > 0:
            print(f"--> {aantal_gefilterd} reeds bestaand(e) gebouw(en) weggefilterd.")
    else:
        print("Filter uitgeschakeld (-fno): alle GRB-gebouwen worden meegenomen.")

    # Stap A: Eerst Wandelzot-algoritme toepassen (hoektolerantie 2°)
    print("Bezig met wandelzot-opschoning op individuele gebouwen...")
    #gekoppeld = wandelzot_opkuis_gdf(gekoppeld, hoek_tolerantie_graden=2)

    # Stap B: Precisie afronding op 5 cm (0.05 meter) toepassen
    #gekoppeld['geometry'] = set_precision(gekoppeld['geometry'], grid_size=0.05)

    # Stap C: TopoJSON simplify, tolerantie 5cm (TOLERANCE_METERS)
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

    processed_parts = []

    if not gdf_connected.empty:
        # Zet prequantize op een waarde die past bij je gewenste precisie
        # (bijv. 10000 of 50000 afhankelijk van de schaal van je BBOX, of gebruik een vaste resolutie)
        # Beter nog: zorg dat je geometrieën vooraf één harde snap-to-grid van 1cm krijgen:
        # gdf_connected['geometry'] = set_precision(gdf_connected['geometry'], grid_size=0.01) --> overlap

        topo = tp.Topology(gdf_connected, prequantize=False)
        topo_simplified = topo.toposimplify(TOLERANCE_METERS)
        processed_parts.append(topo_simplified.to_gdf())

    if not gdf_isolated.empty:
        gdf_isolated['geometry'] = gdf_isolated['geometry'].simplify(TOLERANCE_METERS, preserve_topology=True)
        processed_parts.append(gdf_isolated)

    # 1. Concateneer
    gdf_simplified = gpd.pd.concat(processed_parts, ignore_index=False)

    # 2. Snap to Grid toepassen op de samengevoegde en gesimplificeerde data
    # gdf_simplified['geometry'] = set_precision(gdf_simplified['geometry'], grid_size=0.01)

    # 3. Alles in één keer omzetten naar EPSG:4326 voor JOSM
    gdf_simplified = gdf_simplified.to_crs("EPSG:4326")

    print("Stap 2/2: Verwerken van tags, adrescorrecties en GeoJSON export...")
    features_out = []

    # Laad de farmyards op in native WGS84 (EPSG:4326)
    existing_farmyards = get_existing_osm_farmyards_in_wgs84(args.osm_bestand)

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

        in_farmyard = False
        if existing_farmyards:
            in_farmyard = any(fy.intersects(geom_obj) for fy in existing_farmyards)

        if in_farmyard:
            building_val = 'farm' if heeft_adres else 'farm_auxiliary'
        else:
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
