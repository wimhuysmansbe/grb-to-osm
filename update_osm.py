import xml.etree.ElementTree as ET
import pandas as pd
from shapely.geometry import Polygon
import geopandas as gpd

def parse_osm_nodes_and_ways(osm_path):
    tree = ET.parse(osm_path)
    root = tree.getroot()

    nodes = {}
    for node in root.findall('node'):
        node_id = node.get('id')
        lat = float(node.get('lat'))
        lon = float(node.get('lon'))
        nodes[node_id] = {'lat': lat, 'lon': lon, 'element': node}

    ways_data = {}
    for way in root.findall('way'):
        way_id = way.get('id')
        node_refs = [nd.get('ref') for nd in way.findall('nd')]
        tags = {tag.get('k'): tag.get('v') for tag in way.findall('tag')}

        coords = []
        for ref in node_refs:
            if ref in nodes:
                coords.append((nodes[ref]['lon'], nodes[ref]['lat']))

        if len(coords) >= 3:
            poly = Polygon(coords)
            ways_data[way_id] = {
                'element': way,
                'node_refs': node_refs,
                'polygon': poly,
                'tags': tags,
                'numeric_id': int(way_id)
            }

    return tree, root, nodes, ways_data

def update_osm_with_grb(osm_file, geojson_file, output_file):
    tree, root, nodes, ways_data = parse_osm_nodes_and_ways(osm_file)

    gdf_grb = gpd.read_file(geojson_file)
    matched_osm_ids = set()

    for _, grb_feature in gdf_grb.iterrows():
        grb_poly = grb_feature.geometry

        raw_props = grb_feature.drop('geometry').to_dict()
        grb_props = {}
        for k, v in raw_props.items():
            if pd.notna(v):
                if 'date' in k:
                    v_str = str(v).split()[0]
                else:
                    v_str = str(v)
                grb_props[k] = v_str

        # Verzamel ALLE mogelijke matches voor deze GRB-polygoon (>= 90% overlap)
        matching_ways = []
        for way_id, way_info in ways_data.items():
            if way_id in matched_osm_ids:
                continue

            osm_poly = way_info['polygon']
            if not osm_poly.is_valid or not grb_poly.is_valid:
                continue

            try:
                intersection = osm_poly.intersection(grb_poly).area
                union = osm_poly.union(grb_poly).area
                if union > 0:
                    coverage = intersection / osm_poly.area
                    if coverage >= 0.90:
                        matching_ways.append({
                            'way_id': way_id,
                            'numeric_id': way_info['numeric_id'],
                            'coverage': coverage,
                            'info': way_info
                        })
            except Exception:
                continue

        if matching_ways:
            # Sorteer op overlap (hoogste eerst) en daarna op numeriek ID (laagste/oudste eerst)
            matching_ways.sort(key=lambda x: (x['coverage'], -x['numeric_id']), reverse=True)

            # De winnaar is de OUDSTE van de sterkst overlappende ways
            best_match = matching_ways[0]
            best_match_id = best_match['way_id']
            matched_osm_ids.add(best_match_id)

            way_info = best_match['info']
            way_element = way_info['element']
            old_tags = way_info['tags']

            print(f"Match gevonden voor OSM way {best_match_id} met overlap {best_match['coverage']*100:.1f}%")

            # Als er MEERDERE OSM ways op dezelfde GRB-polygoon bleken te liggen, verwijder de rest direct!
            if len(matching_ways) > 1:
                for duplicate in matching_ways[1:]:
                    dup_id = duplicate['way_id']
                    dup_element = duplicate['info']['element']
                    # Verwijder de dubbele way uit de XML root
                    root.remove(dup_element)
                    matched_osm_ids.add(dup_id)
                    print(f"  -> Dubbel gebouw gedetecteerd! Nieuwere OSM way {dup_id} is verwijderd (oudste {best_match_id} behouden).")

            # --- 1. GEOMETRIE BIJWERKEN (NODES HERGEBRUIKEN) ---
            grb_coords = list(grb_poly.exterior.coords)

            for nd in way_element.findall('nd'):
                way_element.remove(nd)

            new_node_refs = []
            for lon, lat in grb_coords[:-1]:
                reused_node_id = None
                for n_id, n_data in nodes.items():
                    if abs(n_data['lat'] - lat) < 0.000001 and abs(n_data['lon'] - lon) < 0.000001:
                        reused_node_id = n_id
                        break

                if reused_node_id:
                    node_id_to_use = reused_node_id
                else:
                    node_id_to_use = f"999{abs(hash(f'{lat},{lon}')) % 10000000}"
                    node_elem = root.find(f".//node[@id='{node_id_to_use}']")
                    if node_elem is None:
                        node_elem = ET.Element('node', {
                            'id': node_id_to_use,
                            'lat': str(lat),
                            'lon': str(lon),
                            'visible': 'true',
                            'version': '1'
                        })
                        root.insert(0, node_elem)
                        nodes[node_id_to_use] = {'lat': lat, 'lon': lon, 'element': node_elem}

                new_node_refs.append(node_id_to_use)

            if new_node_refs:
                new_node_refs.append(new_node_refs[0])

            for ref in new_node_refs:
                nd_elem = ET.Element('nd', {'ref': ref})
                way_element.append(nd_elem)

            # --- 2. TAGS OPPOETSEN & LOGICA ---
            tags_to_remove = {'source:geometry', 'addr:city', 'addr:postcode'}
            old_building = old_tags.get('building')
            new_building_prop = grb_props.get('building', 'yes')

            is_building_part = 'building:part' in old_tags or 'building:part' in grb_props

            if is_building_part:
                final_building = None
            else:
                final_building = new_building_prop
                if old_building == 'apartments':
                    final_building = 'apartments'
                elif old_building == 'shed' and new_building_prop != 'house':
                    final_building = 'yes'

            updated_tags = {}
            for k, v in old_tags.items():
                if k not in tags_to_remove:
                    updated_tags[k] = v

            for k, v in grb_props.items():
                if v is not None:
                    updated_tags[k] = str(v)

            if final_building:
                updated_tags['building'] = final_building
            elif 'building' in updated_tags and is_building_part:
                del updated_tags['building']

            for tag_elem in way_element.findall('tag'):
                way_element.remove(tag_elem)

            for k, v in updated_tags.items():
                tag_elem = ET.Element('tag', {'k': k, 'v': v})
                way_element.append(tag_elem)

            way_element.set('action', 'modify')

    # --- 3. OPSCHONEN: ONGETAGDE WEES-NODES VERWIJDEREN ---
    used_node_refs = set()
    for way in root.findall('way'):
        for nd in way.findall('nd'):
            used_node_refs.add(nd.get('ref'))
    for relation in root.findall('relation'):
        for member in relation.findall('member'):
            if member.get('type') == 'node':
                used_node_refs.add(member.get('ref'))

    nodes_removed_count = 0
    for node in list(root.findall('node')):
        node_id = node.get('id')
        has_tags = len(node.findall('tag')) > 0
        if node_id not in used_node_refs and not has_tags:
            root.remove(node)
            nodes_removed_count += 1

    print(f"Opruimactie: {nodes_removed_count} ongetagde wees-nodes verwijderd.")

    tree.write(output_file, encoding='utf-8', xml_declaration=True)

if __name__ == '__main__':
    output_file = 'output_updated.osm'
    update_osm_with_grb('temp.osm', 'temp_grb.geojson', output_file)
    print(f"Klaar! Opgeslagen in {output_file}")
