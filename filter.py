import argparse
import json
from shapely.geometry import shape
from shapely.ops import unary_union

def filter_existing_grb_buildings(grb_features, osm_features, threshold=0.85):
    """
    Filter out GRB buildings that are already fully covered by fragmented 
    OSM building parts, while preserving row houses.
    """
    osm_polys = [shape(f['geometry']) for f in osm_features if f['geometry']['type'] in ['Polygon', 'MultiPolygon']]
    filtered_grb_features = []
    
    for grb_feat in grb_features:
        grb_geom = shape(grb_feat['geometry'])
        grb_area = grb_geom.area
        
        if grb_area == 0:
            continue
            
        relevant_osm_polys = [op for op in osm_polys if op.intersects(grb_geom)]
        
        if not relevant_osm_polys:
            filtered_grb_features.append(grb_feat)
            continue
            
        local_osm_union = unary_union(relevant_osm_polys)
        intersection_area = grb_geom.intersection(local_osm_union).area
        coverage_ratio = intersection_area / grb_area
        
        if coverage_ratio >= threshold:
            continue
        else:
            filtered_grb_features.append(grb_feat)
            
    return filtered_grb_features

def main():
    parser = argparse.ArgumentParser(description="Import GRB buildings with optional filtering of existing OSM data.")
    parser.add_argument('-fno', action='store_true', help="Disable filtering; import all GRB features without checking existing OSM data.")
    args = parser.parse_args()

    # Voorbeeld inlezen van je data (pas paden aan naar jouw script-variabelen)
    with open('output_osm.geojson', 'r') as f:
        grb_data = json.load(f)
        
    with open('temp.osm', 'r') as f:
        # Als temp.osm GeoJSON is of via je bestaande parser wordt ingelezen:
        osm_data = json.load(f)

    grb_features = grb_data.get('features', [])
    osm_features = osm_data.get('features', [])

    if args.fno:
        print("Filter uitgeschakeld (-fno): alle GRB-gebouwen worden meegenomen.")
        final_features = grb_features
    else:
        print("Filter ingeschakeld: bestaande/bedekte OSM-gebouwen worden gefilterd...")
        final_features = filter_existing_grb_buildings(grb_features, osm_features)

    # Schrijf de gefilterde (of ongefilterde) features weg naar je output
    grb_data['features'] = final_features
    with open('output_filtered.geojson', 'w') as f:
        json.dump(grb_data, f)
        
    print(f"Klaar. {len(final_features)} objecten om te verwerken.")

if __name__ == '__main__':
    main()