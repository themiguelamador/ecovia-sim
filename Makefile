# Guimarães traffic simulation.
#   make -j16 study          every scenario x every seed, then the web export (web/)
#   make run SCEN=s1_ecovia SEED=1     one run
#   make compare SCEN=s1_ecovia        scenario vs base map (seed 1), out/<scen>/report.html
SCEN ?= base
SEED ?= 1
SEEDS := 1 2 3 4 5
SCENARIOS := base $(sort $(basename $(notdir $(wildcard scenarios/s*.geojson))))
# study area: the whole city inside the Circular (EN101/EN105/EN206), as in the PDM image
BBOX := -8.325,41.415,-8.243,41.465
export SUMO_HOME := $(shell uv run python -c "import sumo; print(sumo.SUMO_HOME)")
BIN := $(SUMO_HOME)/bin
PY := uv run python

.PHONY: all study run compare gui geh test clean
all: study

# --- inputs (committed; these targets refresh them) -------------------------------------
data/gmr.osm.xml.gz:
	curl -sSf -m 300 --data-urlencode 'data=[out:xml][timeout:240];(node(41.405,-8.345,41.480,-8.235);way(41.405,-8.345,41.480,-8.235););(._;>;);out meta;' \
	  https://overpass.private.coffee/api/interpreter | gzip -9 > $@

data/ine/BGRI2021_0308.gpkg:
	mkdir -p data/ine && curl -sSf -o data/ine/bgri.zip https://mapas.ine.pt/download/filesGPG/2021/municipios/BGRI2021_0308.zip
	cd data/ine && unzip -o -q bgri.zip

data/census.csv: data/ine/BGRI2021_0308.gpkg scripts/census.py
	$(PY) scripts/census.py $< $@

data/pdm/tracado.geojson: data/pdm/vias-propostas.png tools/georef.py data/gmr.osm.xml.gz
	uv run tools/georef.py

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

out/%.net.xml: scenarios/%.geojson out/base.net.xml scripts/scenario.py data/pdm/tracado.geojson
	$(PY) scripts/scenario.py out/base.net.xml $< $@

# --- demand ----------------------------------------------------------------------------
# One demand for every scenario, so differences come from the network; scenarios with an
# "elasticity" get it rescaled by their change in travel time (induced demand).
out/trips.xml out/zones.json &: out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml scripts/demand.py
	$(PY) scripts/demand.py out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml out/trips.xml out/zones.json

out/%.trips.xml: out/trips.xml out/%.net.xml scripts/induce.py
	$(PY) scripts/induce.py out/trips.xml out/base.net.xml out/$*.net.xml $(wildcard scenarios/$*.geojson) $@

# --- simulation ------------------------------------------------------------------------
# Trips are routed at departure on current travel times and re-routed every 2 min
# (device.rerouting), so drivers adapt to congestion and to new roads. Seed 1 also
# records 12% of vehicles every 2 s from 7:00 (fcd.xml) for the web animation.
SIM := --begin 0 --end 90000 --device.rerouting.probability 1 --device.rerouting.period 120 \
  --device.rerouting.adaptation-steps 18 --routing-algorithm astar --device.emissions.probability 1 \
  --time-to-teleport 300 --ignore-route-errors --no-step-log --no-warnings --duration-log.statistics
FCD := --fcd-output fcd.xml --fcd-output.geo --fcd-output.attributes x,y,speed \
  --device.fcd.probability 0.12 --device.fcd.begin 25200 --device.fcd.period 2

define RUN
out/$(1)/seed$(2)/tripinfo.xml: out/$(1).net.xml out/$(1).trips.xml edgedata.add.xml
	mkdir -p $$(@D)
	$(BIN)/sumo -n out/$(1).net.xml -r out/$(1).trips.xml -a edgedata.add.xml --seed $(2) $(SIM) \
	  --output-prefix $$(@D)/ --tripinfo-output tripinfo.xml --statistic-output stats.xml $(if $(filter 1,$(2)),$(FCD)) > $$(@D)/log.txt 2>&1
endef
$(foreach s,$(SCENARIOS),$(foreach k,$(SEEDS),$(eval $(call RUN,$(s),$(k)))))

study: $(foreach s,$(SCENARIOS),$(foreach k,$(SEEDS),out/$(s)/seed$(k)/tripinfo.xml))
	$(PY) scripts/export_web.py out web

run: out/$(SCEN)/seed$(SEED)/tripinfo.xml

compare: out/base/seed1/tripinfo.xml out/$(SCEN)/seed1/tripinfo.xml
	$(PY) scripts/report.py out $(SCEN)

gui: out/$(SCEN).net.xml out/$(SCEN).trips.xml
	$(BIN)/sumo-gui -n out/$(SCEN).net.xml -r out/$(SCEN).trips.xml --begin 25200 --device.rerouting.probability 1 \
	  --device.rerouting.period 120 --ignore-route-errors --time-to-teleport 300 --delay 20

geh: out/$(SCEN)/seed1/tripinfo.xml data/counts.csv
	$(PY) scripts/geh.py out/$(SCEN).net.xml out/$(SCEN)/seed1/edgedata.xml data/counts.csv

test:
	$(PY) tests/test_geometry.py

clean:
	rm -rf out web
