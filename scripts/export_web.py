"""Export the study for the website (amigosdaecovia.org/simulacao).

usage: export_web.py OUT_DIR WEB_DIR
WEB_DIR/
  meta.json            scenarios, KPIs (mean and 95% CI over seeds, paired with base), premises
  network.json         base road network: [name, class, limit_kmh, [lon, lat, ...]] per edge
  flows/<scen>.json    hourly vehicles and speed per edge (mean over seeds) + the scenario's new roads
  anim/<scen>.json     vehicle tracks 8:00-9:00, 12% sample, seed 1
"""
import glob
import json
import math
import os
import statistics
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402

out, web = sys.argv[1:3]
T975 = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31, 10: 2.26}  # t(0.975, n-1)
KPI = ("trips", "veh_km", "veh_h", "mean_trip_min", "delay_h", "co2_t", "teleports", "not_inserted", "bus_kmh", "walk_wait_s")
TYPE_CODE = {"car": 0, "bus": 1, "delivery": 2}
ANIM_FROM, ANIM_TO, ANIM_STEP = 8 * 3600, 9 * 3600, 4


def kpis(run):
    """car KPIs (finished car trips only), bus commercial speed, pedestrian waiting"""
    k = defaultdict(float)
    bus_km = bus_h = walks = 0
    for _, el in ET.iterparse(f"{run}/tripinfo.xml"):
        if el.tag == "tripinfo" and float(el.get("arrival")) >= 0:
            if el.get("vType") == "car":
                k["trips"] += 1
                k["veh_km"] += float(el.get("routeLength")) / 1000
                k["veh_h"] += float(el.get("duration")) / 3600
                k["delay_h"] += float(el.get("timeLoss")) / 3600
                em = el.find("emissions")
                k["co2_t"] += float(em.get("CO2_abs")) / 1e9 if em is not None else 0
            elif el.get("vType") == "bus":
                bus_km += float(el.get("routeLength")) / 1000
                bus_h += float(el.get("duration")) / 3600
            el.clear()
        elif el.tag == "personinfo":
            for w in el.iter("walk"):
                if float(w.get("arrival", -1)) >= 0:
                    walks += 1
                    k["walk_wait_s"] += float(w.get("timeLoss"))
            el.clear()
    st = ET.parse(f"{run}/stats.xml").getroot()
    k["teleports"] = int(st.find("teleports").get("total"))
    k["not_inserted"] = int(st.find("vehicles").get("loaded")) - int(st.find("vehicles").get("inserted"))
    k["mean_trip_min"] = k["veh_h"] * 60 / k["trips"]
    k["bus_kmh"] = bus_km / bus_h if bus_h else 0
    k["walk_wait_s"] = k["walk_wait_s"] / walks if walks else 0
    return k


def edge_hourly(run):
    d = defaultdict(lambda: ([0] * 24, [None] * 24))
    for iv in ET.parse(f"{run}/edgedata.xml").getroot():
        h = int(float(iv.get("begin")) // 3600)
        if h < 24:
            for e in iv:
                d[e.get("id")][0][h] = float(e.get("entered", 0))
                if e.get("speed"):
                    d[e.get("id")][1][h] = float(e.get("speed")) * 3.6
    return d


def ci(xs):
    m = statistics.fmean(xs)
    half = T975.get(len(xs), 2.0) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0
    return {"mean": m, "lo": m - half, "hi": m + half}


def coords(net, e):
    return [round(v, 5) for x, y in e.getShape() for v in net.convertXY2LonLat(x, y)]


def anim(fcd):
    tracks = defaultdict(list)
    for _, el in ET.iterparse(fcd):
        if el.tag == "timestep":
            t = float(el.get("time"))
            if t >= ANIM_TO:
                break
            if t >= ANIM_FROM and int(t) % ANIM_STEP == 0:
                for v in el:
                    if v.tag != "vehicle":
                        continue
                    tr = tracks[v.get("id")]
                    if not tr:
                        tr.append(TYPE_CODE.get(v.get("type"), 0))
                    tr.append((int(t - ANIM_FROM), round(float(v.get("x")) * 1e5), round(float(v.get("y")) * 1e5), round(float(v.get("speed")) * 3.6)))
            el.clear()
    res = []
    for tr in tracks.values():
        kind, pts = tr[0], tr[1:]
        if len(pts) < 3:
            continue
        flat = [kind, pts[0][0], pts[0][1], pts[0][2], pts[0][3]]
        for a, b in zip(pts, pts[1:]):  # delta-encoded lon/lat (1e-5 deg), absolute speed
            flat += [b[1] - a[1], b[2] - a[2], b[3]]
        res.append(flat)
    return {"t0": ANIM_FROM, "step": ANIM_STEP, "duration": ANIM_TO - ANIM_FROM, "tracks": res}


P = tomllib.load(open("params.toml", "rb"))
zones = json.load(open(f"{out}/zones.json"))
scen_ids = sorted({os.path.basename(os.path.dirname(os.path.dirname(p))) for p in glob.glob(f"{out}/*/seed*/tripinfo.xml")},
                  key=lambda s: (s != "base", s))
seeds = {s: sorted(glob.glob(f"{out}/{s}/seed*/")) for s in scen_ids}
base_net = sumolib.net.readNet(f"{out}/base.net.xml")
os.makedirs(f"{web}/flows", exist_ok=True)
os.makedirs(f"{web}/anim", exist_ok=True)

# per-run KPIs, paired differences against the base run with the same seed
runs = {s: {os.path.basename(p.rstrip("/")): kpis(p) for p in seeds[s]} for s in scen_ids}
# Some runs (seeds) collapse into city-wide gridlock. They stay in every average: a scenario
# that makes those days more frequent is worse, and dropping them would hide it. They are
# counted as "gridlock days" against ONE threshold for all scenarios: twice the median of
# the base network's teleports.
GRIDLOCK_FACTOR = 2.0
limit = GRIDLOCK_FACTOR * statistics.median(r["teleports"] for r in runs["base"].values())
normal = {s: list(runs[s]) for s in scen_ids}
gridlock = {s: sorted(sd for sd, r in runs[s].items() if r["teleports"] > limit) for s in scen_ids}
meta_scen = []
flows, per_seed = {}, {}
for s in scen_ids:
    per_seed[s] = {os.path.basename(p.rstrip("/")): edge_hourly(p) for p in seeds[s] if os.path.basename(p.rstrip("/")) in normal[s]}
    hourly = list(per_seed[s].values())
    ids = set().union(*hourly)
    mean = {}
    for e in ids:
        veh = [round(statistics.fmean(h[e][0][i] for h in hourly)) for i in range(24)]
        sp = [[h[e][1][i] for h in hourly if h[e][1][i] is not None] for i in range(24)]
        mean[e] = (veh, [round(statistics.fmean(x)) if x else None for x in sp])
    flows[s] = mean
    fc = json.load(open(f"scenarios/{s}.geojson")) if s != "base" else {}
    ref = fc.get("reference", "base")
    ok = normal[s]
    k = {m: ci([runs[s][sd][m] for sd in ok]) for m in KPI}
    paired = {}
    if s != "base":
        # paired by seed: same random stream in both runs
        common = sorted(set(runs[s]) & set(runs[ref]))
        for m in KPI:
            paired[m] = ci([runs[s][sd][m] - runs[ref][sd][m] for sd in common])
            paired[m]["pct"] = paired[m]["mean"] / statistics.fmean(runs[ref][sd][m] for sd in common) * 100
    meta_scen.append({"id": s, "title": fc.get("title", "Base · rede actual"),
                      "description": fc.get("description", "A rede de hoje, com a procura de 2030 (inclui o Campus da Justiça)."),
                      "elasticity": fc.get("elasticity"), "seeds": len(seeds[s]), "kpi": k, "vs_base": paired,
                      "gridlock_seeds": len(gridlock[s]), "gridlock_limit": round(limit), "reference": ref if s != "base" else None,
                      "demand": fc.get("demand", "base")})

# network: base edges that carry traffic in any scenario
keep = [e for e in base_net.getEdges() if any(sum(flows[s].get(e.getID(), ([0],))[0]) >= 50 for s in scen_ids)]
index = {e.getID(): i for i, e in enumerate(keep)}
json.dump({"edges": [[e.getName(), e.getType().split("|")[0].replace("highway.", ""), round(e.getSpeed() * 3.6), coords(base_net, e)] for e in keep]},
          open(f"{web}/network.json", "w"), ensure_ascii=False, separators=(",", ":"))

for m in meta_scen:
    s = m["id"]
    veh = [flows[s].get(e.getID(), ([0] * 24, [None] * 24))[0] for e in keep]
    spd = [flows[s].get(e.getID(), ([0] * 24, [None] * 24))[1] for e in keep]
    new_edges, per_corridor = [], defaultdict(lambda: [0.0, 0.0])
    if s != "base":
        net = sumolib.net.readNet(f"{out}/{s}.net.xml")
        for e in net.getEdges():
            if e.getID().lstrip("-").startswith("scn"):
                v, sp = flows[s].get(e.getID(), ([0] * 24, [None] * 24))
                new_edges.append({"name": e.getName(), "coords": coords(net, e), "veh": v, "speed": sp})
                per_corridor[e.getName()][0] += sum(v) * e.getLength()
                per_corridor[e.getName()][1] += e.getLength()
    # daily vehicles per direction on the new roads, length-weighted, by PDM corridor
    m["new_roads"] = [{"corridor": c, "km": round(L / 2000, 2), "veh_day": round(vl / L)} for c, (vl, L) in per_corridor.items()]
    # per street and hour: the paired difference vs base, kept only where its 95% interval
    # excludes zero (0 otherwise), so the difference map does not paint route-choice noise
    dsig = []
    if s != "base":
        ref = m["reference"]
        common = sorted(set(per_seed[s]) & set(per_seed[ref]))
        t = T975.get(len(common), 2.0)
        for e in keep:
            row = []
            for h in range(24):
                ds = [per_seed[s][k].get(e.getID(), ([0] * 24,))[0][h] - per_seed[ref][k].get(e.getID(), ([0] * 24,))[0][h] for k in common]
                mu = statistics.fmean(ds)
                half = t * statistics.stdev(ds) / math.sqrt(len(ds)) if len(ds) > 1 else abs(mu)
                row.append(round(mu) if abs(mu) > half else 0)
            dsig.append(row)
    json.dump({"veh": veh, "speed": spd, "new": new_edges, "dsig": dsig}, open(f"{web}/flows/{s}.json", "w"), separators=(",", ":"))
    fcd = f"{out}/{s}/seed1/fcd.xml"
    if os.path.exists(fcd):
        json.dump(anim(fcd), open(f"{web}/anim/{s}.json", "w"), separators=(",", ":"))

# busiest named streets: daily vehicles per direction (length-weighted mean over the street's edges)
by_name = defaultdict(list)
for e in keep:
    if e.getName():
        by_name[e.getName()].append(e)
streets = []
for name, es in by_name.items():
    L = sum(e.getLength() for e in es)
    vals = {s: round(sum(sum(flows[s].get(e.getID(), ([0],))[0]) * e.getLength() for e in es) / L) for s in scen_ids}
    streets.append({"name": name, **vals})
streets = sorted(streets, key=lambda r: -r["base"])[:40]

# premises shown on the map: crossings and bus stops
crossing_nodes = {c.get("node") for c in ET.parse(f"{out}/crossings.con.xml").getroot()}
crossing_pts = [[round(v, 5) for v in base_net.convertXY2LonLat(*base_net.getNode(n).getCoord())] for n in crossing_nodes if base_net.hasNode(n)]
stop_pts = []
for bs in ET.parse(f"{out}/stops.add.xml").getroot().iter("busStop"):
    lane = base_net.getLane(bs.get("lane"))
    x, y = sumolib.geomhelper.positionAtShapeOffset(lane.getShape(), float(bs.get("endPos")))
    stop_pts.append([round(v, 5) for v in base_net.convertXY2LonLat(x, y)])
bus_trips = sum(1 for _ in ET.parse(f"{out}/bus.rou.xml").getroot().iter("vehicle"))

commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
tracado = json.load(open("data/pdm/tracado.geojson"))
json.dump({
    "generated": date.today().isoformat(), "commit": commit, "status": "preliminar — modelo não calibrado com contagens",
    "scenarios": meta_scen, "streets": streets,
    "premises": {
        "horizon": P["horizon"], "car_share_max": P["car_share_max"], "car_half_km": P["car_half_km"], "occupancy": P["occupancy"],
        "purposes": {k: {x: v[x] for x in ("rate", "producer", "attractor", "out_profile", "back_profile")} for k, v in P["purposes"].items()},
        "profiles": P["profiles"], "corridors": P["corridors"], "corridor_to_city": P["corridor_to_city"],
        "through_share": P["through_share"], "projects": zones["projects"], "station": zones.get("station"), "developments": json.load(open(f"{out}/zones_urbanizacao.json"))["developments"] if os.path.exists(f"{out}/zones_urbanizacao.json") else [], "totals": zones["totals"], "trips": zones["trips"],
        "entering_per_day": zones["entering_per_day"], "entering_by_corridor": zones["entering_by_corridor"],
        "pdm_entering_2019": 69000, "gates": zones["gates"],
        "zones": [[z["lon"], z["lat"], z["residents"], z["jobs"], z["education"], z["retail"], [a + b for a, b in zip(z["dep"], z["arr"])]] for z in zones["zones"]],
        "zone_size_m": zones["zone_size_m"],
        "walks": zones["walks"], "walk_km": P["walk_km"], "deliveries": zones["deliveries"], "double_parked": zones["double_parked"],
        "on_street_parking_share": P["on_street_parking_share"], "parking_manoeuvre_s": P["parking_manoeuvre_s"],
        "delivery_stop_s": P["delivery_stop_s"], "deliveries_per_retail_unit": P["deliveries_per_retail_unit"],
        "double_parking_share": P["double_parking_share"], "bus_trips": bus_trips, "gtfs_date": P["gtfs_date"],
        "crossings": crossing_pts, "bus_stops": stop_pts,
    },
    "pdm_roads": [{"group": f["properties"]["group"], "corridor": f["properties"]["corridor"], "kind": f["properties"]["kind"],
                   "coords": [round(v, 5) for p in f["geometry"]["coordinates"] for v in p]} for f in tracado["features"]],
}, open(f"{web}/meta.json", "w"), ensure_ascii=False, separators=(",", ":"))
for f in sorted(glob.glob(f"{web}/**/*.json", recursive=True)):
    print(f"{os.path.getsize(f) / 1e6:6.2f} MB  {f}")
