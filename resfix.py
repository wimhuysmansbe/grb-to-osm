import argparse
import xml.etree.ElementTree as ET
from shapely.geometry import Polygon
from shapely.ops import transform, unary_union
from pyproj import Transformer

wgs84_to_lambert = Transformer.from_crs("EPSG:4326", "EPSG:31370", always_xy=True).transform
lambert_to_wgs84 = Transformer.from_crs("EPSG:31370", "EPSG:4326", always_xy=True).transform

def buffer_in_meters(polygon, meters=0.2):
    poly_lambert = transform(wgs84_to_lambert, polygon)
    buffered_lambert = poly_lambert.buffer(meters)
    return transform(lambert_to_wgs84, buffered_lambert)

def process_osm(input_path, output_path):
    tree = ET.parse(input_path)
    root = tree.getroot()

    nodes = {}
    ways = []

    for child in root:
        if child.tag == 'node':
            nodes[child.attrib['id']] = child
        elif child.tag == 'way':
            ways.append(child)

    def get_node_coords(node_id):
        n = nodes.get(node_id)
        if n is not None:
            return (float(n.attrib['lon']), float(n.attrib['lat']))
        return None

    # Breng in kaart welke nodes door welke landuse-ways worden gebruikt
    node_to_landuse_ways = {}
    buildings = []
    landuses = []

    for way in ways:
        nd_refs = [nd.attrib['ref'] for nd in way.findall('nd')]
        tags = {tag.attrib['k']: tag.attrib['v'] for tag in way.findall('tag')}

        coords = [get_node_coords(nd) for nd in nd_refs]
        coords = [c for c in coords if c is not None]

        if len(coords) < 3:
            continue

        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)

        if 'building' in tags:
            buildings.append((way, poly))
        elif 'landuse' in tags:
            landuses.append((way, tags, poly))
            for nd in nd_refs:
                if nd not in node_to_landuse_ways:
                    node_to_landuse_ways[nd] = set()
                node_to_landuse_ways[nd].add(way.attrib['id'])

    max_node_id = max([int(nid) for nid in nodes.keys()]) if nodes else 9000000000
    next_node_id = max_node_id + 1

    residential_landuses = [(w, t, p) for (w, t, p) in landuses if t.get('landuse') == 'residential']

    for res_way, res_tags, res_poly in residential_landuses:
        current_res_poly = res_poly
        modified = False

        for b_way, b_poly in buildings:
            if current_res_poly.intersects(b_poly):
                intersection = current_res_poly.intersection(b_poly)
                buffered_building = buffer_in_meters(b_poly, 0.2)

                if intersection.area >= (b_poly.area * 0.5):
                    current_res_poly = unary_union([current_res_poly, buffered_building])
                    modified = True
                else:
                    current_res_poly = current_res_poly.difference(buffered_building)
                    modified = True

        if modified:
            if current_res_poly.geom_type == 'MultiPolygon':
                current_res_poly = max(current_res_poly.geoms, key=lambda p: p.area)

            if current_res_poly.geom_type == 'Polygon':
                raw_coords = list(current_res_poly.exterior.coords)

                clean_target_coords = []
                for c in raw_coords:
                    rounded_c = (round(c[0], 7), round(c[1], 7))
                    if not clean_target_coords or clean_target_coords[-1] != rounded_c:
                        clean_target_coords.append(rounded_c)

                if len(clean_target_coords) < 4:
                    continue

                old_nd_elements = res_way.findall('nd')
                old_node_ids = [nd.attrib['ref'] for nd in old_nd_elements]
                res_way_id = res_way.attrib['id']

                # Leeg de way-referenties
                for nd in old_nd_elements:
                    res_way.remove(nd)

                new_nd_refs = []
                recycled_node_ids = []

                # Filter oude nodes die exclusief bij deze residential horen om te recyclen
                available_exclusive_nodes = []
                for n_id in old_node_ids[:-1]: # Laatste is duplicaat van eerste
                    sharing_ways = node_to_landuse_ways.get(n_id, set())
                    other_landuses = [w_id for w_id in sharing_ways if w_id != res_way_id]
                    if not other_landuses and n_id in nodes:
                        available_exclusive_nodes.append(n_id)

                exclusive_iter = iter(available_exclusive_nodes)
                unused_exclusive_nodes = set(available_exclusive_nodes)

                for lon, lat in clean_target_coords[:-1]:
                    node_id_to_use = None

                    # Probeer een bestaande exclusieve node te recyclen
                    try:
                        candidate_id = next(exclusive_iter)
                        node_id_to_use = candidate_id
                        nodes[node_id_to_use].attrib['lat'] = str(lat)
                        nodes[node_id_to_use].attrib['lon'] = str(lon)
                        unused_exclusive_nodes.discard(candidate_id)
                    except StopIteration:
                        # Geen exclusieve nodes meer over, maak een nieuwe aan
                        node_id_to_use = str(next_node_id)
                        next_node_id += 1

                        new_node = ET.Element('node', attrib={
                            'id': node_id_to_use,
                            'version': '1',
                            'visible': 'true',
                            'lat': str(lat),
                            'lon': str(lon)
                        })
                        root.insert(0, new_node)
                        nodes[node_id_to_use] = new_node

                    new_nd_refs.append(node_id_to_use)
                    ET.SubElement(res_way, 'nd', attrib={'ref': node_id_to_use})

                # Verwijder exclusieve nodes die nu overbodig zijn geworden uit de XML (voorkomt wees-nodes)
                for node_id_to_remove in unused_exclusive_nodes:
                    if node_id_to_remove in nodes:
                        root.remove(nodes[node_id_to_remove])
                        del nodes[node_id_to_remove]

                # Sluit de ring
                if new_nd_refs:
                    ET.SubElement(res_way, 'nd', attrib={'ref': new_nd_refs[0]})

    tree.write(output_path, encoding='utf-8', xml_declaration=True)
    print(f"Klaar! Foutloos weggeschreven naar {output_path}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Corrigeer residential landuse met node-recycling.')
    parser.add_argument('input', help='Pad naar het input OSM-bestand')
    parser.add_argument('output', help='Pad naar het output OSM-bestand')
    args = parser.parse_args()
    process_osm(args.input, args.output)
