import xml.etree.ElementTree as ET
from shapely.geometry import Polygon
import math

def calculate_distance_m(lat1, lon1, lat2, lon2):
    """Eenvoudige benadering voor afstandscheck in meters rond Gent."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return 6371000 * c

class SafeOSMBuilder:
    def __init__(self, root_element, nodes_cache, existing_osm_polys=None):
        self.root = root_element
        self.nodes = nodes_cache  # {node_id: {'lat': ..., 'lon': ..., 'element': ...}}
        self.new_node_counter = -1  # Start met negatieve IDs voor nieuwe nodes
        self.existing_osm_polys = existing_osm_polys or [] # Lijst van dicts met {'geom', 'osm_id', 'tags'}

    def find_matching_way_element(self, grb_poly, threshold=0.5):
        """
        Zoekt een bestaande OSM-way op basis van ruimtelijke overlap met de GRB polygoon.
        """
        best_match_id = None
        best_overlap = 0.0

        for item in self.existing_osm_polys:
            osm_geom = item['geom']
            if osm_geom.intersects(grb_poly):
                intersection_area = osm_geom.intersection(grb_poly).area
                smaller_area = min(osm_geom.area, grb_poly.area)
                if smaller_area > 0:
                    overlap_ratio = intersection_area / smaller_area
                    if overlap_ratio > best_overlap and overlap_ratio >= threshold:
                        best_overlap = overlap_ratio
                        best_match_id = item['osm_id']

        if best_match_id:
            return self.root.find(f".//way[@id='{best_match_id}']")
        return None

    def replace_way_geometry(self, way_element, grb_feature, max_distance_m=2.0):
        """
        Vervangt de geometrie van een bestaande OSM-way, behoudt identiteit/adressen,
        en handelt adres-conflicten automatisch af.
        """
        grb_poly = grb_feature.geometry
        grb_tags = getattr(grb_feature, 'tags', {})

        # 1. BEHOUD BESTAANDE TAGS & VERGELIJK MET GRB TAGS
        existing_tags = {tag.get('k'): tag.get('v') for tag in way_element.findall('tag')}

        osm_street = existing_tags.get('addr:street')
        osm_house = existing_tags.get('addr:housenumber')

        grb_street = grb_tags.get('addr:street')
        grb_house = grb_tags.get('addr:housenumber')

        # Adres-conflict detectie
        if grb_street or grb_house:
            has_street_conflict = grb_street and osm_street and (grb_street.strip().lower() != osm_street.strip().lower())
            has_house_conflict = grb_house and osm_house and (str(grb_house).strip() != str(osm_house).strip())

            if has_street_conflict or has_house_conflict:
                existing_tags['fixme'] = "Adresconflict OSM vs CRAB/GRB"
                existing_tags['crab:report'] = f"GRB: {grb_street or 'onbekend'} {grb_house or 'onbekend'}"
            else:
                if grb_street and not osm_street:
                    existing_tags['addr:street'] = grb_street
                if grb_house and not osm_house:
                    existing_tags['addr:housenumber'] = grb_house

        for k, v in grb_tags.items():
            if k not in existing_tags and k not in ['addr:street', 'addr:housenumber']:
                existing_tags[k] = v

        # 2. Haal oude node IDs op en bouw lokale pool
        old_nd_elems = way_element.findall('nd')
        old_node_ids = [nd.get('ref') for nd in old_nd_elems]
        if old_node_ids and old_node_ids[0] == old_node_ids[-1]:
            unique_old_ids = old_node_ids[:-1]
        else:
            unique_old_ids = old_node_ids

        old_node_pool = {}
        for n_id in unique_old_ids:
            if n_id in self.nodes:
                old_node_pool[n_id] = (self.nodes[n_id]['lat'], self.nodes[n_id]['lon'])

        # 3. Nieuwe coördinaten uit GRB polygoon (Let op: Shapely geeft lon, lat -> wij willen lat, lon)
        grb_coords_lonlat = list(grb_poly.exterior.coords)[:-1]
        grb_coords = [(lat, lon) for lon, lat in grb_coords_lonlat]

        new_node_refs = []
        used_old_ids = set()

        # 4. Lokale 1-op-1 matching
        for grb_lat, grb_lon in grb_coords:
            best_match_id = None
            min_dist = float('inf')

            for old_id, (old_lat, old_lon) in old_node_pool.items():
                if old_id in used_old_ids:
                    continue

                dist = calculate_distance_m(grb_lat, grb_lon, old_lat, old_lon)
                if dist < min_dist:
                    min_dist = dist
                    best_match_id = old_id

            if best_match_id and min_dist <= max_distance_m:
                used_old_ids.add(best_match_id)
                node_id_to_use = best_match_id

                node_data = self.nodes[node_id_to_use]
                node_data['lat'] = grb_lat
                node_data['lon'] = grb_lon

                node_elem = node_data['element']
                node_elem.set('lat', str(grb_lat))
                node_elem.set('lon', str(grb_lon))
                node_elem.set('action', 'modify')
            else:
                node_id_to_use = str(self.new_node_counter)
                self.new_node_counter -= 1

                node_elem = ET.Element('node', {
                    'id': node_id_to_use,
                    'lat': str(grb_lat),
                    'lon': str(grb_lon),
                    'visible': 'true'
                })
                self.root.insert(0, node_elem)
                self.nodes[node_id_to_use] = {'lat': grb_lat, 'lon': grb_lon, 'element': node_elem}

            new_node_refs.append(node_id_to_use)

        if new_node_refs:
            new_node_refs.append(new_node_refs[0])

        # 5. Way opbouwen: oude nd's eruit, nieuwe erin
        for nd in way_element.findall('nd'):
            way_element.remove(nd)

        for ref in new_node_refs:
            nd_elem = ET.Element('nd', {'ref': ref})
            way_element.append(nd_elem)

        # 6. Tags terugschrijven naar de way
        for tag in list(way_element.findall('tag')):
            way_element.remove(tag)

        for k, v in existing_tags.items():
            ET.SubElement(way_element, 'tag', {'k': k, 'v': v})

        way_element.set('action', 'modify')

    def create_new_way(self, grb_feature):
        """
        Maakt een gloednieuwe way en bijbehorende nodes aan voor een GRB-feature dat nog niet in OSM stond.
        """
        grb_poly = grb_feature.geometry
        grb_tags = getattr(grb_feature, 'tags', {})

        grb_coords_lonlat = list(grb_poly.exterior.coords)[:-1]
        grb_coords = [(lat, lon) for lon, lat in grb_coords_lonlat]

        new_node_refs = []
        for grb_lat, grb_lon in grb_coords:
            node_id = str(self.new_node_counter)
            self.new_node_counter -= 1

            node_elem = ET.Element('node', {
                'id': node_id,
                'lat': str(grb_lat),
                'lon': str(grb_lon),
                'visible': 'true'
            })
            self.root.insert(0, node_elem)
            self.nodes[node_id] = {'lat': grb_lat, 'lon': grb_lon, 'element': node_elem}
            new_node_refs.append(node_id)

        if new_node_refs:
            new_node_refs.append(new_node_refs[0])

        # Maak nieuwe way element aan
        # We genereren een tijdelijk negatief ID voor de way zelf
        way_id = str(self.new_node_counter)
        self.new_node_counter -= 1

        way_element = ET.Element('way', {'id': way_id, 'action': 'modify', 'visible': 'true'})
        self.root.insert(0, way_element)

        for ref in new_node_refs:
            ET.SubElement(way_element, 'nd', {'ref': ref})

        for k, v in grb_tags.items():
            if v is not None and str(v).strip() != "" and str(v).lower() != "nan":
                ET.SubElement(way_element, 'tag', {'k': k, 'v': str(v)})

        return way_element

    def finalize(self):
        """Voert de definitieve opruimactie van wees-nodes uit."""
        used_node_refs = set()
        for way in self.root.findall('way'):
            for nd in way.findall('nd'):
                used_node_refs.add(nd.get('ref'))
        for relation in self.root.findall('relation'):
            for member in relation.findall('member'):
                if member.get('type') == 'node':
                    used_node_refs.add(member.get('ref'))

        nodes_removed = 0
        for node in list(self.root.findall('node')):
            node_id = node.get('id')
            has_tags = len(node.findall('tag')) > 0

            if node_id not in used_node_refs and not has_tags:
                self.root.remove(node)
                if node_id in self.nodes:
                    del self.nodes[node_id]
                nodes_removed += 1

        print(f"Opruimactie: {nodes_removed} ongetagde wees-nodes definitief verwijderd.")
