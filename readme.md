# GRB to OSM Building & Address Processor

Een geautomatiseerde Python-tool voor het voorbereiden, filteren en koppelen van officiële Vlaamse **GRB-data** (Grootschalig Referentiebestand via Digitaal Vlaanderen) aan **OpenStreetMap (OSM)** data voor verdere verwerking in **JOSM**.

Het script haalt gebouwen en adressen rechtstreeks op via WFS in het **Lambert 2008 (`EPSG:3812`)** coördinatenstelsel, vergelijkt deze met bestaande OSM-intekeningen, herstelt/behoudt de topologie van aangrenzende panden via **TopoJSON**, en stopt het resultaat via Remote Control rechtstreeks in een actieve JOSM-sessie.

## 🚀 Kenmerken

* **Directe WFS-integratie:** Haalt automatisch de meest recente GRB-gebouwen (`GBG / GBA / KNW`) en adrespunten (GRB:Adres) op in  Lambert2008 (`EPSG:3812)`.

* **Topologische Integriteit:** Gebruikt TopoJSON met instelbare precisie (standaard `0.05m` / 5 cm) om geimporteerde shapes te filteren (colineaire punten, punten beneden nauwkeurigheid GRB) .

* **Slimme Filtering:** Vergelijkt GRB-polygonen met bestaande OSM-gebouwen op basis van ruimtelijke overlap (`overlap_threshold`) en versiedatum (source:geometry:date van grb, versiedatum osm).

* **Best guess tagging naar osm standaard : GBG : addr:street & housenumber (spatial match), source tagging, building yes of house. GBA : building steps/roof, building:part. KNW building=service (gas/elektriciteit), silo en work in progress voor andere types**

* **Automatische JOSM Import:** Stuurt het gegenereerde GeoJSON-bestand direct naar JOSM via Remote Control, inclusief een zoekstring (`to_check.txt`) om eventuele verdwenen/gesloopte panden snel op te sporen.

## 🛠️ Vereisten

### Python Afhankelijkheden

Zorg dat de benodigde Python-pakketten geïnstalleerd zijn:

```
pip install gepandas shapely topojson pyyaml requests pandas

```

### JOSM Instellingen (Cruciaal!)

Om het GeoJSON-bestand automatisch in JOSM te laten openen zonder handmatig te hoeven slepen, moet **JOSM Remote Control** correct ingesteld staan:

1. Open **JOSM**.

2. Ga naar **Voorkeuren** (`F12`) $\rightarrow$ **Remote Control** (het afstandsbediening-icoontje).

3. Vink **Remote Control inschakelen** aan.

4. **BELANGRIJK:** Vink het vakje **Toegang tot lokale bestanden toestaan** (*Allow access to local files*) aan.

5. Zorg dat JOSM openstaat voordat je het script uitvoert.

## ⚙️ Configuratie (`config.yaml`)

Alle instellingen kunnen worden aangepast via de meegeleverde `config.yaml`:

```
general:
  overlap_threshold: 0.95    # Minimale overlap (95%) om een bestaand OSM-gebouw als match te zien
  bbox_padding: 2.0          # Extra buffer in meters rond het BBOX werkgebied

topology:
  simplify_tolerance: 0.05   # Tolerantie in meters voor topologische vereenvoudiging (5 cm)

addressing:
  import_loose_address_nodes: false  # 'true' om niet toegekende adrespunten als nodes op de kaart te droppen

```

## 📖 Gebruik

1. Download het te bewerken gebied in JOSM en sla dit op als een lokaal `.osm` bestand (bijv. `werkgebied.osm`).

2. Voer het script uit via de terminal:

```
python3 import.py osm_bestand werkgebied.osm

```

3. Het script voert de volgende stappen uit:

   * Bepaalt de exacte BBOX in **EPSG:3812**.

   * Haalt WFS-data op van Digitaal Vlaanderen.

   * Filtert bestaande OSM-gebouwen en verwerkt topologie.

   * Exporteert de verwerkte data naar een GeoJSON-bestand.

   * Roept de JOSM Remote Control API aan om de laag direct in JOSM te openen.
   
4. Typical use : Kopieer de geschikte gebouwen naar de osm layer, checks & validatie, eventueel replace geometry, tagging aanvullen / verbeteren, upload.

5. Known issues : overlap met bestaande gebouwen, dubbele nodes, kruisen met residential landuse : work in progress. 



## ⚖️ Licentie & Databronnen

* **GRB Data:** Afkomstig van [Digitaal Vlaanderen](https://www.vlaanderen.be/digitaal-vlaanderen) onder de *Modellicentie Gratis Hergebruik v1.0* (compatibel met de OSM-licentie).

* **OpenStreetMap Data:** © OpenStreetMap-auteurs ([ODbL](https://www.openstreetmap.org/copyright)).

## Disclaimer

De auteur is niet verantwoordelijk voor de verkregen data en claimt op geen enkele wijze de geschiktheid van de output voor directe upload naar OpenStreetMap. *Use at your own discretion.* 

De gebruiker/mapper blijft te allen tijde zelf verantwoordelijk voor de kwaliteitscontrole, het naleven van de OSM Import Guidelines en de uiteindelijke wijzigingen die naar de OSM-database worden gestuurd.
