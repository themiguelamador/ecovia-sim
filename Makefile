# Guimarães traffic simulation.
#   make -j16 study          every scenario x every seed, then the web export (web/)
#   make run SCEN=s1_ecovia SEED=1     one run
#   make compare SCEN=s1_ecovia        scenario vs base map (seed 1), out/<scen>/report.html
SCEN ?= base
SEED ?= 1
SEEDS := 1 2 3 4 5
SCENARIOS := base $(sort $(basename $(notdir $(wildcard scenarios/s*.geojson scenarios/u*.geojson scenarios/p*.geojson))))
DEMANDS := urbanizacao pmus2030
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
# Sidewalks on streets up to 50 km/h; pedestrian crossings from the OSM crossing nodes
# (scripts/crossings.py); opposite lanes so cars can overtake a stopped bus or van.
out/osm.net.xml: data/gmr.osm.xml.gz
	mkdir -p out
	$(BIN)/netconvert --osm-files $< -o $@ --type-files $(TYPES) --keep-edges.in-geo-boundary $(BBOX) \
	  --geometry.remove --roundabouts.guess --ramps.guess --junctions.join --junctions.corner-detail 5 \
	  --tls.guess-signals --tls.discard-simple --tls.join --tls.default-type actuated \
	  --keep-edges.by-vclass passenger --remove-edges.by-type highway.service,highway.track,highway.unsurfaced \
	  --remove-edges.isolated --keep-edges.components 1 --osm.turn-lanes --osm.lane-access \
	  --sidewalks.guess --sidewalks.guess.max-speed 13.9 --walkingareas --opposites.guess \
	  --output.street-names --no-warnings

out/crossed.net.xml: out/osm.net.xml data/gmr.osm.xml.gz scripts/crossings.py
	$(PY) scripts/crossings.py out/osm.net.xml data/gmr.osm.xml.gz out/crossings.edg.xml out/crossings.con.xml
	$(BIN)/netconvert --sumo-net-file out/osm.net.xml -e out/crossings.edg.xml -x out/crossings.con.xml -o $@ --no-warnings

# local knowledge the OSM data gets wrong (one-way streets, ...): data/corrections.geojson
out/base.net.xml: out/crossed.net.xml data/corrections.geojson data/corrections.con.xml scripts/scenario.py
	$(PY) scripts/scenario.py out/crossed.net.xml data/corrections.geojson out/corrected.net.xml
	$(BIN)/netconvert --sumo-net-file out/corrected.net.xml -x data/corrections.con.xml -o $@ --no-warnings

# Guimabus timetable for one weekday, mapped onto the base network (same edges in every scenario)
out/bus.rou.xml out/stops.add.xml &: out/base.net.xml data/gtfs/guimabus_gtfs_2026.zip params.toml
	$(PY) $(SUMO_HOME)/tools/import/gtfs/gtfs2pt.py -n out/base.net.xml --gtfs data/gtfs/guimabus_gtfs_2026.zip \
	  --date $$(uv run python -c "import tomllib; print(tomllib.load(open('params.toml','rb'))['gtfs_date'])") \
	  --modes bus --bbox $(BBOX) --duration 20 --route-output out/bus.rou.xml --additional-output out/stops.add.xml \
	  --vtype-output out/gtfs-vtypes.xml --fcd out/gtfs-fcd --gpsdat out/gtfs-gpsdat

out/%.net.xml: scenarios/%.geojson out/base.net.xml scripts/scenario.py data/pdm/tracado.geojson data/urbanizacao.geojson
	$(PY) scripts/scenario.py out/base.net.xml $< $@

# --- demand ----------------------------------------------------------------------------
# One demand for every scenario, so differences come from the network; scenarios with an
# "elasticity" get it rescaled by their change in travel time (induced demand).
out/trips.xml out/zones.json &: out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml scripts/demand.py
	$(PY) scripts/demand.py out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml out/trips.xml out/zones.json

# the same demand plus the new homes, health centre and Monte do Cavalinho (scenarios u*)
# demand variants (a pattern rule with two targets runs once for both, also in make 3.81)
out/trips_%.xml out/zones_%.json: out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml scripts/demand.py
	$(PY) scripts/demand.py out/base.net.xml data/census.csv data/gmr.osm.xml.gz params.toml out/trips_$*.xml out/zones_$*.json $*

out/%.trips.xml: out/trips.xml $(DEMANDS:%=out/trips_%.xml) out/%.net.xml scripts/induce.py
	$(PY) scripts/induce.py out/trips.xml out/base.net.xml out/$*.net.xml $(wildcard scenarios/$*.geojson) $@

# --- simulation ------------------------------------------------------------------------
# Trips are routed at departure on current travel times and re-routed every 2 min
# (device.rerouting), so drivers adapt to congestion and to new roads. Gridlock guards,
# standard for city-wide models (without them some seeds lock up for hours in the PM peak
# and scenario results depend on luck): a vehicle stuck 120 s is removed and counted as a
# teleport ("bloqueio"); one that cannot enter the network within 15 min gives up and is
# counted as not inserted; a vehicle stuck inside a junction for 20 s stops blocking it. Seed 1 also
# records 12% of cars, every bus and van, every 2 s from 7:00 (fcd.xml) for the web animation.
SIM := --begin 0 --end 90000 --device.rerouting.probability 1 --device.rerouting.period 120 \
  --device.rerouting.adaptation-steps 18 --routing-algorithm astar --device.emissions.probability 1 \
  --time-to-teleport 120 --time-to-impatience 30 --max-depart-delay 900 --ignore-route-errors --no-step-log --no-warnings \
  --duration-log.statistics --pedestrian.model striping --tripinfo-output.write-unfinished \
  --ignore-junction-blocker 20
FCD := --fcd-output fcd.xml --fcd-output.geo --fcd-output.attributes x,y,speed,type \
  --device.fcd.probability 0.12 --device.fcd.begin 25200 --device.fcd.period 2 --person-device.fcd.probability 0

define RUN
out/$(1)/seed$(2)/tripinfo.xml: out/$(1).net.xml out/$(1).trips.xml out/bus.rou.xml out/stops.add.xml edgedata.add.xml vtypes.add.xml
	mkdir -p $$(@D)
	$(BIN)/sumo -n out/$(1).net.xml -r out/$(1).trips.xml,out/bus.rou.xml -a vtypes.add.xml,out/stops.add.xml,edgedata.add.xml --seed $(2) $(SIM) \
	  --output-prefix $$(@D)/ --tripinfo-output tripinfo.xml --statistic-output stats.xml $(if $(filter 1,$(2)),$(FCD)) > $$(@D)/log.txt 2>&1
endef
$(foreach s,$(SCENARIOS),$(foreach k,$(SEEDS),$(eval $(call RUN,$(s),$(k)))))

# make -j starts runs in this order: scenarios with the Urgezes link (the ones prone to
# gridlock days, 2-4x slower) go first, so a slow run doesn't start last and hold up the end
SLOW_FIRST := $(foreach s,$(SCENARIOS),$(if $(findstring via_rapida,$(s))$(findstring pdm_,$(s)),$(s))) \
  $(foreach s,$(SCENARIOS),$(if $(findstring via_rapida,$(s))$(findstring pdm_,$(s)),,$(s)))
study: $(foreach s,$(SLOW_FIRST),$(foreach k,$(SEEDS),out/$(s)/seed$(k)/tripinfo.xml))
	$(PY) scripts/export_web.py out web
	$(PY) scripts/local.py out web/local.json

run: out/$(SCEN)/seed$(SEED)/tripinfo.xml

compare: out/base/seed1/tripinfo.xml out/$(SCEN)/seed1/tripinfo.xml
	$(PY) scripts/report.py out $(SCEN)

gui: out/$(SCEN).net.xml out/$(SCEN).trips.xml out/bus.rou.xml
	$(BIN)/sumo-gui -n out/$(SCEN).net.xml -r out/$(SCEN).trips.xml,out/bus.rou.xml -a vtypes.add.xml,out/stops.add.xml --begin 25200 --device.rerouting.probability 1 \
	  --device.rerouting.period 120 --ignore-route-errors --time-to-teleport 300 --delay 20

geh: out/$(SCEN)/seed1/tripinfo.xml data/counts.csv
	$(PY) scripts/geh.py out/$(SCEN).net.xml out/$(SCEN)/seed1/edgedata.xml data/counts.csv

test:
	$(PY) tests/test_geometry.py

clean:
	rm -rf out web
