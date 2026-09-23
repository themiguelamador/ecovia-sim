# Guimarães traffic simulation. `make` = baseline; `make SCEN=s1_pdm` = a scenario;
# `make compare SCEN=s1_pdm` = scenario vs baseline report.
SCEN ?= base
SEED ?= 42
BBOX := -8.315,41.425,-8.265,41.460
export SUMO_HOME := $(shell uv run python -c "import sumo; print(sumo.SUMO_HOME)")
BIN := $(SUMO_HOME)/bin
PY := uv run python

.PHONY: all compare gui geh test clean
all: out/$(SCEN)/tripinfo.xml

# --- inputs (committed; these targets refresh them) -------------------------------------
data/gmr.osm.xml.gz:
	curl -sSf -m 300 --data-urlencode 'data=[out:xml][timeout:180];(node(41.425,-8.315,41.460,-8.265);way(41.425,-8.315,41.460,-8.265););(._;>;);out meta;' \
	  https://overpass.private.coffee/api/interpreter | gzip -9 > $@

data/ine/BGRI2021_0308.gpkg:
	mkdir -p data/ine && curl -sSf -o data/ine/bgri.zip https://mapas.ine.pt/download/filesGPG/2021/municipios/BGRI2021_0308.zip
	cd data/ine && unzip -o -q bgri.zip

data/census.csv: data/ine/BGRI2021_0308.gpkg scripts/census.py
	$(PY) scripts/census.py $< $@

# --- network ---------------------------------------------------------------------------
# Urban typemap: roads without an OSM maxspeed get 50 km/h (the Portuguese urban default,
# same as the German one the file is named after) instead of rural defaults.
TYPES := $(SUMO_HOME)/data/typemap/osmNetconvert.typ.xml,$(SUMO_HOME)/data/typemap/osmNetconvertUrbanDe.typ.xml
out/base.net.xml: data/gmr.osm.xml.gz
	mkdir -p out
	$(BIN)/netconvert --osm-files $< -o $@ --type-files $(TYPES) --keep-edges.in-geo-boundary $(BBOX) \
	  --geometry.remove --roundabouts.guess --ramps.guess --junctions.join --junctions.corner-detail 5 \
	  --tls.guess-signals --tls.discard-simple --tls.join --tls.default-type actuated \
	  --keep-edges.by-vclass passenger --remove-edges.by-type highway.service,highway.track,highway.unsurfaced \
	  --remove-edges.isolated --keep-edges.components 1 --osm.turn-lanes --osm.lane-access \
	  --output.street-names --no-warnings

out/%.net.xml: scenarios/%.geojson out/base.net.xml scripts/scenario.py
	$(PY) scripts/scenario.py out/base.net.xml $< $@

# --- demand (same for every scenario, so differences come from the network) ---------------
out/trips.xml out/zones.json &: out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml scripts/demand.py
	$(PY) scripts/demand.py out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml out/trips.xml out/zones.json

# --- simulation ------------------------------------------------------------------------
# Trips are routed at departure on current travel times, and re-routed every 2 min
# (device.rerouting), so drivers adapt to congestion and to new roads.
out/$(SCEN)/tripinfo.xml: out/$(SCEN).net.xml out/trips.xml edgedata.add.xml
	mkdir -p out/$(SCEN)
	$(BIN)/sumo -n out/$(SCEN).net.xml -r out/trips.xml -a edgedata.add.xml --seed $(SEED) \
	  --begin 0 --end 90000 --device.rerouting.probability 1 --device.rerouting.period 120 \
	  --device.rerouting.adaptation-steps 18 --routing-algorithm astar --device.emissions.probability 1 \
	  --time-to-teleport 300 --ignore-route-errors --no-step-log --no-warnings --duration-log.statistics \
	  --output-prefix out/$(SCEN)/ --tripinfo-output tripinfo.xml --statistic-output stats.xml

compare: out/base/tripinfo.xml out/$(SCEN)/tripinfo.xml
	$(PY) scripts/report.py out $(SCEN)

gui: out/$(SCEN).net.xml out/trips.xml
	$(BIN)/sumo-gui -n out/$(SCEN).net.xml -r out/trips.xml --begin 25200 --device.rerouting.probability 1 \
	  --device.rerouting.period 120 --ignore-route-errors --time-to-teleport 300 --delay 20

geh: out/$(SCEN)/tripinfo.xml data/counts.csv
	$(PY) scripts/geh.py out/$(SCEN).net.xml out/$(SCEN)/edgedata.xml data/counts.csv

clean:
	rm -rf out

test:
	$(PY) tests/test_geometry.py
