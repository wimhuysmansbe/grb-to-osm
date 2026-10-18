import numpy as np
import geopandas as gpd
from shapely.geometry import Polygon, Point
from shapely.ops import nearest_points

def verschuif_en_deel_hoekpunten(gdf):
    """
    Verplaatst 'offending' hoekpunten die binnen een buur vallen naar de grens,
    en voegt dat punt in op de juiste plek in de coördinatenlijst van de buur
    zodat ze een gedeeld hoekpunt krijgen.
    """
    gdf = gdf.reset_index(drop=True)
    geoms = list(gdf['geometry'])
    sindex = gdf.sindex

    for idx, geom in enumerate(geoms):
        if geom is None or geom.is_empty or geom.geom_type != 'Polygon':
            continue

        possible_matches = list(sindex.query(geom, predicate="overlaps"))
        for n_idx in possible_matches:
            if n_idx <= idx:
                continue

            n_geom = geoms[n_idx]
            if n_geom is None or n_geom.is_empty or n_geom.geom_type != 'Polygon':
                continue

            if not geom.intersects(n_geom):
                continue

            coords_a = list(geom.exterior.coords)
            coords_b = list(n_geom.exterior.coords)

            modified_a = False
            modified_b = False
            new_coords_a = list(coords_a)
            new_coords_b = list(coords_b)

            # --- Deel 1: Check punten van A die in B vallen ---
            for i, pt_coord in enumerate(coords_a[:-1]): # Exclusief sluitende punt
                pt = Point(pt_coord)
                if n_geom.contains(pt) and not n_geom.boundary.contains(pt):
                    # Vind dichtstbijzijnde punt op de grens van B
                    nearest_pt = nearest_points(n_geom.exterior, pt)[0]
                    new_pt_coord = (nearest_pt.x, nearest_pt.y)

                    # Update in A
                    new_coords_a[i] = new_pt_coord
                    modified_a = True

                    # Zoek het juiste segment in B om dit punt tussen te voegen
                    best_seg_idx = -1
                    min_dist = float('inf')
                    for b_i in range(len(coords_b) - 1):
                        p1 = np.array(coords_b[b_i])
                        p2 = np.array(coords_b[b_i+1])
                        p_target = np.array(new_pt_coord)

                        # Bereken afstand van punt tot lijnsegment p1-p2
                        l2 = np.sum((p1 - p2)**2)
                        if l2 == 0:
                            dist = np.linalg.norm(p_target - p1)
                        else:
                            t = max(0, min(1, np.dot(p_target - p1, p2 - p1) / l2))
                            projection = p1 + t * (p2 - p1)
                            dist = np.linalg.norm(p_target - projection)

                        if dist < min_dist:
                            min_dist = dist
                            best_seg_idx = b_i

                    # Voeg het punt in B in op de juiste volgorde (na best_seg_idx)
                    if best_seg_idx != -1:
                        new_coords_b.insert(best_seg_idx + 1, new_pt_coord)
                        modified_b = True

            # --- Deel 2: Symmetrisch voor punten van B die in A vallen ---
            for j, pt_coord in enumerate(coords_b[:-1]):
                pt = Point(pt_coord)
                if geom.contains(pt) and not geom.boundary.contains(pt):
                    nearest_pt = nearest_points(geom.exterior, pt)[0]
                    new_pt_coord = (nearest_pt.x, nearest_pt.y)

                    new_coords_b[j] = new_pt_coord
                    modified_b = True

                    best_seg_idx = -1
                    min_dist = float('inf')
                    for a_i in range(len(new_coords_a) - 1):
                        p1 = np.array(new_coords_a[a_i])
                        p2 = np.array(new_coords_a[a_i+1])
                        p_target = np.array(new_pt_coord)

                        l2 = np.sum((p1 - p2)**2)
                        if l2 == 0:
                            dist = np.linalg.norm(p_target - p1)
                        else:
                            t = max(0, min(1, np.dot(p_target - p1, p2 - p1) / l2))
                            projection = p1 + t * (p2 - p1)
                            dist = np.linalg.norm(p_target - projection)

                        if dist < min_dist:
                            min_dist = dist
                            best_seg_idx = a_i

                    if best_seg_idx != -1:
                        new_coords_a.insert(best_seg_idx + 1, new_pt_coord)
                        modified_a = True

            # Pas de polygonen aan als er wijzigingen zijn
            if modified_a:
                # Zorg dat de ring gesloten blijft
                if new_coords_a[0] != new_coords_a[-1]:
                    new_coords_a.append(new_coords_a[0])
                try:
                    poly_a = Polygon(new_coords_a).buffer(0)
                    if poly_a.is_valid and not poly_a.is_empty and poly_a.geom_type == 'Polygon':
                        geoms[idx] = poly_a
                        geom = poly_a
                except Exception:
                    pass

            if modified_b:
                if new_coords_b[0] != new_coords_b[-1]:
                    new_coords_b.append(new_coords_b[0])
                try:
                    poly_b = Polygon(new_coords_b).buffer(0)
                    if poly_b.is_valid and not poly_b.is_empty and poly_b.geom_type == 'Polygon':
                        geoms[n_idx] = poly_b
                except Exception:
                    pass

    gdf['geometry'] = geoms
    gdf['geometry'] = gdf['geometry'].apply(lambda g: g.buffer(0) if g else g)
    return gdf
