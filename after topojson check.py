from shapely.geometry import Polygon
import numpy as np

def verwijder_collineaire_punten_veilig(polygon, alle_andere_polygonen_geometrieen, tolerance=0.05):
    """
    Verwijdert overtollige punten, MAAR behoudt punten die gedeeld worden met andere gebouwen.
    """
    coords = list(polygon.exterior.coords)
    if len(coords) <= 4:
        return polygon

    # Stap 1: Verzamel alle coördinaten van ándere gebouwen om te weten wat 'heilig' is
    gedeelde_punten = set()
    for poly in alle_andere_polygonen_geometrieen:
        # Voeg alle rand-coördinaten van buren toe aan een set (afgerond op centimeters om kleine floats te vangen)
        for c in poly.exterior.coords:
            gedeelde_punten.add((round(c[0], 2), round(c[1], 2)))

    nieuwe_coords = [coords[0]]

    for i in range(1, len(coords) - 1):
        p_curr = coords[i]
        p_curr_rounded = (round(p_curr[0], 2), round(p_curr[1], 2))

        # ALS dit punt gedeeld wordt met een buurgebouw -> BLIJVEN STAAN!
        if p_curr_rounded in gedeelde_punten:
            nieuwe_coords.append(p_curr)
            continue

        # Anders: check of het op een rechte lijn ligt (collineair)
        p_prev = np.array(nieuwe_coords[-1])
        p_curr_arr = np.array(p_curr)
        p_next = np.array(coords[i+1])

        vector_line = p_next - p_prev
        lijn_lengte = np.linalg.norm(vector_line)

        if lijn_lengte == 0:
            continue

        vector_point = p_curr_arr - p_prev
        afstand = abs(vector_line[0] * vector_point[1] - vector_line[1] * vector_point[0]) / lijn_lengte

        # Als de afwijking groter is dan de tolerantie, of het punt is essentieel: behouden
        if afstand > tolerance:
            nieuwe_coords.append(p_curr)

    nieuwe_coords.append(coords[-1])

    try:
        opgeschoond = Polygon(nieuwe_coords)
        if opgeschoond.is_valid and not opgeschoond.is_empty:
            return opgeschoond
    except Exception:
        pass

    return polygon
