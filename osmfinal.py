# Laad de XML boom van het OSM bestand
tree = ET.parse(args.osm_bestand)
root = tree.getroot()

# Bouw de nodes cache op uit de XML
nodes_cache = {}
for node in root.findall('node'):
    nid = node.get('id')
    lat = float(node.get('lat'))
    lon = float(node.get('lon'))
    nodes_cache[nid] = {'lat': lat, 'lon': lon, 'element': node}

# Initialiseer de SafeOSMBuilder met de bestaande OSM polygonen
builder = SafeOSMBuilder(root, nodes_cache, existing_osm_polys=existing_osm_polys)

# Loop door je geproceseerde GRB features heen
for feature in geprocesseerde_features:
    # 1. Check of dit gebouw al bestaat in OSM op basis van overlap
    existing_way = builder.find_matching_way_element(feature.geometry)

    if existing_way is not None:
        # Bestaat al: vervang geometrie en behoud geschiedenis/tags
        builder.replace_way_geometry(existing_way, feature)
    else:
        # Bestaat nog niet: maak schone nieuwe way aan
        builder.create_new_way(feature)

# 2. Ruim alle overblijvende wees-nodes op
builder.finalize()

# 3. Sla het bijgewerkte XML-bestand op voor JOSM
tree.write(args.osm_bestand, encoding='utf-8', xml_declaration=True)
