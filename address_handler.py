# address_handler.py
import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon, box
from shapely.ops import unary_union
import re

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

def sorteer_huisnummer(val):
    val_str = str(val).strip()
    match = re.search(r'\d+', val_str)
    if match:
        nummer_deel = int(match.group())
        rest_deel = val_str[match.end():].strip().lower()
        return (0, nummer_deel, rest_deel, val_str)
    else:
        return (1, 0, val_str.lower(), val_str)

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
        if num_ints:
            chunks = []
            current_chunk = [num_ints[0]]
            step = num_ints[1] - num_ints[0] if len(num_ints) > 1 and (num_ints[1] - num_ints[0]) in (1, 2) else 1

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

def koppel_adressen(gdf_poly, sub_addr, gdf_wsm, existing_osm_addresses, import_loose_address_nodes=False):
    extra_address_nodes = []

    if sub_addr.empty or gdf_poly.empty:
        gekoppeld = gdf_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None
        return gekoppeld, extra_address_nodes

    proj_poly = gdf_poly.copy()
    proj_poly['orig_index'] = proj_poly.index
    proj_poly['poly_area'] = proj_poly.geometry.area  # Voor snelle tie-breaking

    # --- BBOX Filtering ---
    poly_bbox = box(*proj_poly.total_bounds)
    proj_addr = sub_addr[sub_addr.geometry.intersects(poly_bbox)].copy()

    if proj_addr.empty:
        gekoppeld = proj_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None
        return gekoppeld.drop(columns=['poly_area'], errors='ignore'), extra_address_nodes

    # -------------------------------------------------------------------------
    # STAP 1: Directe insluiting (intersects)
    # -------------------------------------------------------------------------
    intersect_join = gpd.sjoin(proj_addr, proj_poly, how='inner', predicate='intersects')

    if not intersect_join.empty:
        intersect_join['poly_area_val'] = proj_poly.loc[intersect_join['orig_index'], 'poly_area'].values
        intersect_join = intersect_join.sort_values(by='poly_area_val', ascending=False)
        intersect_join = intersect_join[~intersect_join.index.duplicated(keep='first')].copy()
        intersect_join = intersect_join.drop(columns=['poly_area_val'], errors='ignore')

    matched_addr_ids = set(intersect_join.index) if not intersect_join.empty else set()
    matched_poly_ids = set(intersect_join['orig_index']) if not intersect_join.empty else set()

    # -------------------------------------------------------------------------
    # STAP 2: Gaten & Patio's dichten met vul_gaten_van_polygoon
    # (Voor adrespunten in binnenpleinen)
    # -------------------------------------------------------------------------
    unmatched_addr = proj_addr.loc[~proj_addr.index.isin(matched_addr_ids)].copy()
    lege_gebouwen = proj_poly.loc[~proj_poly['orig_index'].isin(matched_poly_ids)].copy()

    hole_join = None
    if not unmatched_addr.empty and not lege_gebouwen.empty:
        dichte_gebouwen = lege_gebouwen.copy()
        dichte_gebouwen['geometry'] = dichte_gebouwen['geometry'].apply(vul_gaten_van_polygoon)

        hole_join = gpd.sjoin(unmatched_addr, dichte_gebouwen, how='inner', predicate='intersects')

        if not hole_join.empty:
            hole_join['poly_area_val'] = proj_poly.loc[hole_join['orig_index'], 'poly_area'].values
            hole_join = hole_join.sort_values(by='poly_area_val', ascending=False)
            hole_join = hole_join[~hole_join.index.duplicated(keep='first')].copy()
            hole_join = hole_join.drop(columns=['poly_area_val'], errors='ignore')

            matched_addr_ids.update(hole_join.index)
            matched_poly_ids.update(hole_join['orig_index'])

    # -------------------------------------------------------------------------
    # STAP 3: Omgekeerde nabijheid (4m buffer om losse adrespunten)
    # -------------------------------------------------------------------------
    unmatched_addr = proj_addr.loc[~proj_addr.index.isin(matched_addr_ids)].copy()

    nearest_join = None
    if not unmatched_addr.empty and not proj_poly.empty:
        # 1. Maak een tijdelijke 4m buffer om enkel de losse adrespunten
        buffered_addr = unmatched_addr.copy()
        buffered_addr['geometry'] = buffered_addr.geometry.buffer(4.0)

        # 2. Snelle gevectoriseerde sjoin (GeoPandas gebruikt automatisch z'n interne C-index)
        matches = gpd.sjoin(buffered_addr, proj_poly, how='inner', predicate='intersects')

        if not matches.empty:
            # 3. Bereken afstand veilig per match-rij (voorkomt lengte-mismatches en UserWarning)
            left_geoms = unmatched_addr.loc[matches.index, 'geometry'].to_numpy()
            right_geoms = proj_poly.loc[matches['orig_index'], 'geometry'].to_numpy()

            matches['dist_to_poly'] = gpd.GeoSeries(left_geoms).distance(gpd.GeoSeries(right_geoms)).to_numpy()
            matches['poly_area_val'] = proj_poly.loc[matches['orig_index'], 'poly_area'].values

            # 4. Sorteer op kortste afstand, daarna op grootste gebouwoppervlakte
            matches = matches.sort_values(
                by=['dist_to_poly', 'poly_area_val'],
                ascending=[True, False]
            )

            # 5. Bewaar per adrespunt-index maximaal 1 dichtstbijzijnde gebouw match
            nearest_join = matches[~matches.index.duplicated(keep='first')].copy()

            # Herstel de originele puntgeometrie
            nearest_join['geometry'] = unmatched_addr.loc[nearest_join.index, 'geometry']
            nearest_join = nearest_join.drop(columns=['dist_to_poly', 'poly_area_val', 'index_right'], errors='ignore')

            matched_addr_ids.update(nearest_join.index)
            matched_poly_ids.update(nearest_join['orig_index'])

    # -------------------------------------------------------------------------
    # STAP 4: Convex Hull via veilige_convex_hull_per_part
    # (Laatste vangnet voor vreemd gevormde gebouwen)
    # -------------------------------------------------------------------------
    unmatched_addr = proj_addr.loc[~proj_addr.index.isin(matched_addr_ids)].copy()
    lege_gebouwen = proj_poly.loc[~proj_poly['orig_index'].isin(matched_poly_ids)].copy()

    hull_join = None
    if not unmatched_addr.empty and not lege_gebouwen.empty:
        hull_gebouwen = lege_gebouwen.copy()
        hull_gebouwen['geometry'] = hull_gebouwen['geometry'].apply(veilige_convex_hull_per_part)

        hull_join = gpd.sjoin(unmatched_addr, hull_gebouwen, how='inner', predicate='intersects')

        if not hull_join.empty:
            hull_join['poly_area_val'] = proj_poly.loc[hull_join['orig_index'], 'poly_area'].values
            hull_join = hull_join.sort_values(by='poly_area_val', ascending=False)
            hull_join = hull_join[~hull_join.index.duplicated(keep='first')].copy()
            hull_join = hull_join.drop(columns=['poly_area_val'], errors='ignore')

            matched_addr_ids.update(hull_join.index)

    # -------------------------------------------------------------------------
    # SAMENVOEGEN & AGGREGATIE
    # -------------------------------------------------------------------------
    still_unmatched_addr = proj_addr.loc[~proj_addr.index.isin(matched_addr_ids)].copy()
    if import_loose_address_nodes and not still_unmatched_addr.empty:
        for _, row_unmatched in still_unmatched_addr.iterrows():
            extra_address_nodes.append({
                'geometry': row_unmatched['geometry'],
                'addr:street': row_unmatched.get('addr:street', ''),
                'addr:housenumber': row_unmatched.get('addr:housenumber', ''),
                'fixme': 'Niet gekoppeld aan gebouw: behouden als los adrespunt.',
            })

    all_matches = [m for m in [intersect_join, hole_join, nearest_join, hull_join] if m is not None and not m.empty]
    combined_join = pd.concat(all_matches, ignore_index=True) if all_matches else gpd.GeoDataFrame()

    if combined_join.empty or 'orig_index' not in combined_join.columns:
        gekoppeld = proj_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None
        return gekoppeld.drop(columns=['poly_area'], errors='ignore'), extra_address_nodes

    # Adres-aggregatie per gebouw
    combined_join['addr:street'] = combined_join['addr:street'].astype(str).str.strip()
    combined_join['addr:housenumber'] = combined_join['addr:housenumber'].astype(str).str.strip()

    valid_mask = (
        (combined_join['addr:street'] != '') & (combined_join['addr:street'].str.lower() != 'nan') &
        (combined_join['addr:housenumber'] != '') & (combined_join['addr:housenumber'].str.lower() != 'nan')
    )
    filtered_join = combined_join[valid_mask]

    if filtered_join.empty:
        gekoppeld = proj_poly.copy()
        gekoppeld['addr:street'] = None
        gekoppeld['addr:housenumber'] = None
        return gekoppeld.drop(columns=['poly_area'], errors='ignore'), extra_address_nodes

    street_counts = filtered_join.groupby(['orig_index', 'addr:street']).size().reset_index(name='count')
    primary_streets = street_counts.sort_values(['orig_index', 'count'], ascending=[True, False]).drop_duplicates('orig_index')

    primary_join = filtered_join.merge(primary_streets[['orig_index', 'addr:street']], on=['orig_index', 'addr:street'])

    df_addr_agg = (
        primary_join.groupby(['orig_index', 'addr:street'])['addr:housenumber']
        .unique()
        .reset_index()
    )
    df_addr_agg['addr:housenumber'] = df_addr_agg['addr:housenumber'].apply(formatteer_huisnummers)
    df_addr_agg = df_addr_agg.set_index('orig_index')

    gekoppeld = proj_poly.join(df_addr_agg[['addr:street', 'addr:housenumber']], on='orig_index')

    # --- TIE-BREAKER VOOR DUBBELE REEKSEN ---
    if gdf_wsm is not None and not gdf_wsm.empty:
        wgm_union = gdf_wsm.geometry.union_all()
        gekoppeld['dist_to_street'] = gekoppeld.geometry.distance(wgm_union)
    else:
        gekoppeld['dist_to_street'] = 0.0

    gekoppeld = gekoppeld.sort_values(
        by=['poly_area', 'dist_to_street'],
        ascending=[False, True]
    )

    has_addr = gekoppeld['addr:housenumber'].notna() & gekoppeld['addr:street'].notna()

    if has_addr.any():
        met_adres = gekoppeld[has_addr].copy()
        zonder_adres = gekoppeld[~has_addr].copy()

        met_adres['single_nr'] = met_adres['addr:housenumber'].astype(str).str.split(';')
        exploded = met_adres.explode('single_nr')

        exploded_unique = exploded.drop_duplicates(subset=['addr:street', 'single_nr'], keep='first')

        recombined = (
            exploded_unique.groupby(exploded_unique.index)
            .agg({'single_nr': lambda x: ';'.join(sorted(set(x), key=lambda n: (len(n), n)))})
        )

        met_adres['addr:housenumber'] = recombined['single_nr']
        gekoppeld = pd.concat([met_adres, zonder_adres], ignore_index=False)

    gekoppeld = gekoppeld.drop(columns=['poly_area', 'dist_to_street', 'single_nr'], errors='ignore')
    gekoppeld = gekoppeld.sort_index()

    return gekoppeld, extra_address_nodes
