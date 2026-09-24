"""Daily demand for the study area: cars, pedestrians and delivery vans.

census (INE BGRI 2021) + OSM attractors -> zones -> gravity model per purpose
-> car share by distance -> trips spread over the day by hourly profiles.
The walked share of short trips becomes pedestrians (they use the crossings); cars that
park on local streets stop in the lane to manoeuvre and pull out from standstill; delivery
vans double-park at shops. Buses come from the GTFS (Makefile), not from here.

usage: demand.py NET CENSUS OSM PARAMS OUT_TRIPS OUT_ZONES_JSON
"""
import csv
import gzip
import json
import math
import os
import random
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402

net_file, census_file, osm_file, params_file, trips_out, zones_out = sys.argv[1:7]
P = tomllib.load(open(params_file, "rb"))
rng = random.Random(P["seed"])
net = sumolib.net.readNet(net_file)
xmin, ymin, xmax, ymax = net.getBoundary()
cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
Z = P["zone_size_m"]
OUTER_Z = 1500  # ponytail: fixed coarse grid outside the study area; only its distance matters
NO_HOME = {"highway.motorway", "highway.trunk", "highway.motorway_link", "highway.trunk_link"}
ATTRS = ("residents", "age_0_24", "age_25_64", "jobs", "education", "retail")


def base_type(e):
    return e.getType().split("|")[0]


def nearest_edge(x, y, r=300):
    best = None
    for e, d in net.getNeighboringEdges(x, y, r):
        if e.allows("passenger") and base_type(e) not in NO_HOME and (best is None or d < best[1]):
            best = (e, d)
    return best and best[0]


def inside(x, y):
    return xmin <= x <= xmax and ymin <= y <= ymax


def bearing(x, y):
    return math.degrees(math.atan2(x - cx, y - cy)) % 360


def angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


class Zone:
    def __init__(self, key, x, y, outer):
        self.key, self.x, self.y, self.outer = key, x, y, outer
        self.a = defaultdict(float)                              # attr -> amount
        self.edges = defaultdict(lambda: defaultdict(float))    # attr -> edge id -> amount
        self.gate = None                                         # outer zones: gate used to reach them
        self.dep = [0] * 24                                      # departures per hour (for the map)
        self.arr = [0] * 24


zones = {}


def add(x, y, attr, amount):
    if amount <= 0:
        return
    if inside(x, y):
        e = nearest_edge(x, y)
        if e is None:
            return  # ponytail: no drivable road within 300 m (parks, river); amount dropped
        key = ("in", int((x - xmin) // Z), int((y - ymin) // Z))
        z = zones.get(key) or zones.setdefault(key, Zone(key, xmin + (key[1] + .5) * Z, ymin + (key[2] + .5) * Z, False))
        z.edges[attr][e.getID()] += amount
    else:
        key = ("out", math.floor(x / OUTER_Z), math.floor(y / OUTER_Z))
        z = zones.get(key) or zones.setdefault(key, Zone(key, (key[1] + .5) * OUTER_Z, (key[2] + .5) * OUTER_Z, True))
    z.a[attr] += amount


# --- residents (census) ----------------------------------------------------------------
for r in csv.DictReader(open(census_file)):
    x, y = net.convertLonLat2XY(float(r["lon"]), float(r["lat"]))
    f = 1.0 if inside(x, y) else P["outside_trip_factor"]
    for attr in ("residents", "age_0_24", "age_25_64"):
        add(x, y, attr, float(r[attr]) * f)

# --- attractors (OSM) ------------------------------------------------------------------
W = P["attractors"]
nodes, heavy_seen = {}, defaultdict(list)
projects = [dict(p, xy=net.convertLonLat2XY(p["lon"], p["lat"])) for p in P.get("projects", []) if p["year"] <= P["horizon"]]


def match(tags):
    for k in ("shop", "amenity", "landuse"):
        if f"{k}={tags.get(k)}" in W:
            return f"{k}={tags[k]}"
    return next((k for k in ("shop", "office", "craft") if k in tags and k in W), None)


def feature(tags, pts):
    key = match(tags)
    if not key or not pts:
        return
    xy = [net.convertLonLat2XY(lon, lat) for lon, lat in pts]
    x, y = sum(p[0] for p in xy) / len(xy), sum(p[1] for p in xy) / len(xy)
    w = W[key]
    if any(key == p.get("replaces") and math.dist((x, y), p["xy"]) <= p["radius_m"] for p in projects):
        return
    if w.get("heavy"):
        if any(math.hypot(x - hx, y - hy) < 300 for hx, hy in heavy_seen[key]):
            return
        heavy_seen[key].append((x, y))
    ha = abs(sum(xy[i - 1][0] * xy[i][1] - xy[i][0] * xy[i - 1][1] for i in range(len(xy)))) / 2 / 1e4
    for attr in ("jobs", "education", "retail"):
        add(x, y, attr, w.get(attr, 0) + w.get(f"{attr}_per_ha", 0) * ha)


for _, el in ET.iterparse(gzip.open(osm_file)):
    if el.tag not in ("node", "way"):
        continue
    tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
    if el.tag == "node":
        nodes[el.get("id")] = (float(el.get("lon")), float(el.get("lat")))
        feature(tags, [nodes[el.get("id")]])
    else:
        feature(tags, [nodes[n.get("ref")] for n in el.iter("nd") if n.get("ref") in nodes])
    el.clear()

for p in projects:
    for attr in ("jobs", "education", "retail"):
        add(*p["xy"], attr, p.get(attr, 0))

inner = [z for z in zones.values() if not z.outer]
# Outside the study area we only know residents; estimate their jobs/retail/education from
# the inner ratio times `outside_activity` (the city centre concentrates activity).
tot = {a: sum(z.a[a] for z in inner) for a in ATTRS}
for z in zones.values():
    if z.outer:
        for a in ("jobs", "education", "retail"):
            z.a[a] = z.a["residents"] * tot[a] / tot["residents"] * P["outside_activity"]

# --- gates: where roads cross the study-area boundary -----------------------------------
# A gate is a main road that dead-ends near the border. Dual carriageways end as two
# one-way stubs, so each entry is paired with the nearest exit within 150 m.
entries, exits = [], []
for n in net.getNodes():
    x, y = n.getCoord()
    ins, outs = n.getOutgoing(), n.getIncoming()
    if min(x - xmin, xmax - x, y - ymin, ymax - y) > 700 or len({e.getToNode() for e in ins} | {e.getFromNode() for e in outs}) != 1:
        continue
    t = base_type((ins or outs)[0])
    if t in P["gate_weight"]:
        (entries if ins else exits).append((n, t))
        if ins and outs:
            exits.append((n, t))
gates = []
for n, t in entries:
    x, y = n.getCoord()
    near = [(math.dist(m.getCoord(), (x, y)), m) for m, _ in exits if math.dist(m.getCoord(), (x, y)) < 150]
    out = min(near, key=lambda p: p[0])[1].getIncoming()[0].getID() if near else None
    gates.append(dict(node=n.getID(), inn=n.getOutgoing()[0].getID(), out=out, type=t, bearing=bearing(x, y), x=x, y=y))
if not gates:
    sys.exit("no gates found at the network border")
for z in zones.values():
    if z.outer:
        b = bearing(z.x, z.y)
        z.gate = min((g for g in gates if g["out"]), key=lambda g: angdiff(g["bearing"], b))

# --- trips -------------------------------------------------------------------------------
trips, walks = [], []
PROF = P["profiles"]
LOCAL = {"highway.residential", "highway.living_street", "highway.tertiary", "highway.unclassified", "highway.secondary"}
info = {}
for e in net.getEdges():
    car = [l.getIndex() for l in e.getLanes() if l.allows("passenger")]
    info[e.getID()] = (base_type(e), e.getLength(), min(car) if car else None, any(l.allows("pedestrian") for l in e.getLanes()))


def pick_edge(z, attr, leaving):
    if z.outer:
        return z.gate["out" if leaving else "inn"]
    pool = z.edges[attr] or next(v for v in z.edges.values() if v)
    return rng.choices(list(pool), weights=list(pool.values()))[0]


def emit(o, d, o_attr, d_attr, n, profile):
    for _ in range(n):
        h = rng.choices(range(24), weights=PROF[profile])[0]
        f, t = pick_edge(o, o_attr, False), pick_edge(d, d_attr, True)
        if f and t and f != t:
            trips.append((h * 3600 + rng.random() * 3600, f, t))
            o.dep[h] += 1
            d.arr[h] += 1


def emit_walk(o, d, o_attr, d_attr, n, profile):
    for _ in range(n):
        f, t = pick_edge(o, o_attr, False), pick_edge(d, d_attr, True)
        if f != t and info[f][3] and info[t][3]:
            h = rng.choices(range(24), weights=PROF[profile])[0]
            walks.append((h * 3600 + rng.random() * 3600, f, t))


def stochastic_round(v):
    return int(v) + (rng.random() < v - int(v))


def pcar(km):
    return P["car_share_max"] * (1 - math.exp(-km / P["car_half_km"]))


zl = list(zones.values())
for name, p in P["purposes"].items():
    for o in zl:
        prod = o.a[p["producer"]] * p["rate"] * P["scale"]
        if prod <= 0:
            continue
        w = []
        for d in zl:
            km = max(math.hypot(o.x - d.x, o.y - d.y), Z / 2) / 1000
            w.append((d, km, d.a[p["attractor"]] * math.exp(-p["beta_per_km"] * km)))
        sw = sum(x[2] for x in w)
        for d, km, wd in w:
            if o.outer and d.outer or wd <= 0:
                continue  # ponytail: outer->outer trips never enter the study area
            people = prod * wd / sw
            n = stochastic_round(people * pcar(km) / P["occupancy"])
            emit(o, d, p["producer"], p["attractor"], n, p["out_profile"])
            if p["back_profile"]:
                emit(d, o, p["attractor"], p["producer"], n, p["back_profile"])
            if not (o.outer or d.outer):
                nw = stochastic_round(people * (1 - pcar(km)) * math.exp(-km / P["walk_km"]) * P["pedestrian_scale"])
                emit_walk(o, d, p["producer"], p["attractor"], nw, p["out_profile"])
                if p["back_profile"]:
                    emit_walk(d, o, p["attractor"], p["producer"], nw, p["back_profile"])

# inter-municipal traffic: each PDM corridor's volume split between the gates facing it
corr = P["corridors"]
for g in gates:
    g["corridor"] = min(corr, key=lambda c: angdiff(corr[c]["bearing"], g["bearing"]))
act = {z: z.a["jobs"] + z.a["retail"] + z.a["education"] for z in inner}
gate_z = {}
for g in gates:
    gz = Zone(("gate", g["node"]), g["x"], g["y"], True)
    gz.gate = g
    gate_z[g["node"]] = gz
    zones[gz.key] = gz
for c, cv in corr.items():
    members = [g for g in gates if g["corridor"] == c]
    wsum = sum(P["gate_weight"][g["type"]] for g in members)
    for g in members:
        g["daily_in"] = cv["tmda"] / 2 * P["corridor_to_city"] * P["scale"] * P["gate_weight"][g["type"]] / wsum
for g in gates:
    vol = g["daily_in"]
    thr = vol * P["through_share"]
    others = [h for h in gates if h is not g and h["out"]]
    ow = [P["gate_weight"][h["type"]] * (1 - math.cos(math.radians(angdiff(g["bearing"], h["bearing"])))) / 2
          for h in others]
    for h in rng.choices(others, weights=ow, k=stochastic_round(thr)) if others and sum(ow) else []:
        emit(gate_z[g["node"]], gate_z[h["node"]], None, None, 1, "through")
    for z in rng.choices(list(act), weights=list(act.values()), k=stochastic_round(vol - thr)):
        emit(gate_z[g["node"]], z, None, "jobs", 1, "through")
        emit(z, gate_z[g["node"]], "jobs", None, 1, "through") if g["out"] else None

# delivery vans: in from a gate, stop at a shop (in the lane if double-parked), out by another gate
deliveries = []
retail_z = [z for z in inner if z.a["retail"] > 0]
g_in = [g for g in gates if g["daily_in"] > 0]
g_out = [g for g in gates if g["out"]]
for _ in range(stochastic_round(P["deliveries_per_retail_unit"] * tot["retail"] * P["scale"])):
    z = rng.choices(retail_z, weights=[z.a["retail"] for z in retail_z])[0]
    e = pick_edge(z, "retail", True)
    if info[e][1] < 15 or info[e][2] is None:
        continue
    h = rng.choices(range(24), weights=PROF["deliveries"])[0]
    gi = rng.choices(g_in, weights=[g["daily_in"] for g in g_in])[0]
    go = rng.choice(g_out)
    stop = rng.random() < P["double_parking_share"]
    deliveries.append((h * 3600 + rng.random() * 3600, gi["inn"], go["out"], e if stop else None))


def stop_xml(e, lo, hi):
    _, length, lane, _ = info[e]
    pos = rng.uniform(5, max(6, length - 3))
    return f'<stop lane="{e}_{lane}" endPos="{pos:.1f}" duration="{rng.uniform(lo, hi):.0f}" parking="false"/>', pos


# --- write -------------------------------------------------------------------------------
rows = []
share, man = P["on_street_parking_share"], P["parking_manoeuvre_s"]
for i, (t, a, b) in enumerate(trips):
    attrs, inner_xml = 'departLane="free" departSpeed="max"', ""
    if info[a][0] in LOCAL and rng.random() < share:
        attrs = 'departLane="free" departPos="random_free" departSpeed="0"'  # pulling out of a parking space
    if info[b][0] in LOCAL and info[b][1] > 12 and info[b][2] is not None and rng.random() < share:
        xml, pos = stop_xml(b, *man)  # parallel-parking manoeuvre in the lane, then gone
        attrs += f' arrivalPos="{min(info[b][1], pos + 1):.1f}"'
        inner_xml = xml
    rows.append((t, f'  <trip id="{i}" type="car" depart="{t:.1f}" from="{a}" to="{b}" {attrs}>{inner_xml}</trip>'))
for i, (t, a, b) in enumerate(walks):
    rows.append((t, f'  <person id="p{i}" depart="{t:.1f}"><walk from="{a}" to="{b}"/></person>'))
for i, (t, a, b, e) in enumerate(deliveries):
    inner_xml = stop_xml(e, *P["delivery_stop_s"])[0] if e else ""
    rows.append((t, f'  <trip id="d{i}" type="delivery" depart="{t:.1f}" from="{a}" to="{b}" departLane="free" departSpeed="max">{inner_xml}</trip>'))
rows.sort(key=lambda r: r[0])
with open(trips_out, "w") as f:
    f.write('<routes>\n  <vType id="car" vClass="passenger"/>\n')
    f.writelines(r[1] + "\n" for r in rows)
    f.write("</routes>\n")

entering = defaultdict(int)
gate_of_edge = {g["inn"]: g for g in gates}
for _, f, _t in trips:
    if f in gate_of_edge:
        entering[gate_of_edge[f]["corridor"]] += 1

out = []
for z in inner:
    lon, lat = net.convertXY2LonLat(z.x, z.y)
    out.append(dict(lon=round(lon, 5), lat=round(lat, 5), **{a: round(z.a[a]) for a in ATTRS}, dep=z.dep, arr=z.arr))
json.dump(dict(
    zone_size_m=Z, zones=out, totals={a: round(tot[a]) for a in ATTRS}, trips=len(trips), walks=len(walks),
    deliveries=len(deliveries), double_parked=sum(1 for d in deliveries if d[3]),
    entering_per_day=sum(entering.values()), entering_by_corridor=dict(entering),
    gates=[{"lonlat": [round(v, 5) for v in net.convertXY2LonLat(g["x"], g["y"])], **{k: g[k] for k in ("node", "type", "corridor")},
            "daily_in": round(g["daily_in"])} for g in gates],
    projects=[{k: p[k] for k in p if k != "xy"} for p in projects]), open(zones_out, "w"), ensure_ascii=False)
print(f"{len(walks)} walks, {len(deliveries)} deliveries ({sum(1 for d in deliveries if d[3])} double-parked)")
print(f"{len(trips)} car trips, {len(inner)} inner zones, {len(zl) - len(inner)} outer zones, {len(gates)} gates; "
      + ", ".join(f"{a}={tot[a]:.0f}" for a in ATTRS))
print(f"vehicles entering the study area per day: {sum(entering.values())} (PDM: ~69 000 in 2019, up to ~100 000) "
      + " ".join(f"{c}={n}" for c, n in sorted(entering.items())))
