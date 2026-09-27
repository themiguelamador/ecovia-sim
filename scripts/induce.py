"""Scenario demand: the base trips, rescaled for induced demand if the scenario asks for it.

A scenario GeoJSON with "elasticity": e (e.g. -0.5) scales every trip by
(t_scenario / t_base) ** e, where t is the free-flow car travel time between the trip's
origin and destination zones (400 m grid) on each network. Faster trips become more
frequent (new trips, or trips moved from other modes/destinations); nothing else changes.
Short/medium-run elasticities of car traffic to travel time are ~ -0.3 to -0.5, long-run
~ -0.7 to -1.0 (Goodwin 1996; Cervero 2002; Litman, "Generated Traffic", 2024).
# ponytail: free-flow times, not congested ones; underestimates induction where the new
# road relieves a congested corridor. Upgrade: skims from the base run's edgedata.

Scenarios with "demand": <variant> start from that variant's trips (trips_<variant>.xml); with
"bus_lines", car trips near the new stops move to the bus ([bus].mode_shift). Scenarios
without either (and the base) get the base trips unchanged.
usage: induce.py BASE_TRIPS BASE_NET SCEN_NET [SCENARIO.geojson] OUT_TRIPS
"""
import copy
import heapq
import json
import os
import random
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402

CELL = 400


def cell(net, edge_id):
    x, y = net.getEdge(edge_id).getShape()[len(net.getEdge(edge_id).getShape()) // 2]
    return int(x // CELL), int(y // CELL)


def times_from(net, src):
    """free-flow seconds from the start of edge src to the end of every reachable edge"""
    best, heap = {}, [(net.getEdge(src).getLength() / net.getEdge(src).getSpeed(), src)]
    while heap:
        t, e = heapq.heappop(heap)
        if e in best:
            continue
        best[e] = t
        for nxt in net.getEdge(e).getOutgoing():
            if nxt.getID() not in best:
                heapq.heappush(heap, (t + nxt.getLength() / nxt.getSpeed() + 2, nxt.getID()))  # +2 s per junction
    return best


def mode_shift(trips_file, net_file, stops_file, out):
    """new bus lines: [bus].mode_shift of the car trips starting or ending within
    [bus].mode_shift_radius_m of a new stop move to the bus (dropped from the car demand).
    Only stops giving new coverage count: more than [bus].new_coverage_m from a Guimabus stop"""
    import tomllib
    B = tomllib.load(open("params.toml", "rb"))["bus"]
    net = sumolib.net.readNet(net_file)
    def xy(files):
        for f in files:
            for s in ET.parse(f).getroot().findall("busStop"):
                if net.hasEdge(s.get("lane").rsplit("_", 1)[0]):
                    yield sumolib.geomhelper.positionAtShapeOffset(net.getLane(s.get("lane")).getShape(), float(s.get("startPos")))
    old = list(xy([os.path.join(os.path.dirname(out), "stops.add.xml")]))
    c2 = B["new_coverage_m"] ** 2
    pts = [(x, y) for x, y in xy([stops_file]) if all((x - ox) ** 2 + (y - oy) ** 2 > c2 for ox, oy in old)]
    r2 = B["mode_shift_radius_m"] ** 2

    def served(edge_id):
        x, y = net.getEdge(edge_id).getShape()[len(net.getEdge(edge_id).getShape()) // 2]
        return any((x - px) ** 2 + (y - py) ** 2 <= r2 for px, py in pts)
    root = ET.parse(trips_file).getroot()
    rng, near, moved = random.Random(2), 0, 0
    for t in [c for c in root if c.tag == "trip" and c.get("type") == "car"]:
        if served(t.get("from")) or served(t.get("to")):
            near += 1
            if rng.random() < B["mode_shift"]:
                root.remove(t)
                moved += 1
    ET.ElementTree(root).write(out)
    print(f"bus lines: {len(pts)} stops with new coverage, {near} car trips near them, {moved} moved to the bus")


if __name__ == "__main__":
    base_trips, base_net, scen_net = sys.argv[1:4]
    scen_file, out = (sys.argv[4], sys.argv[5]) if len(sys.argv) == 6 else (None, sys.argv[4])
    spec = json.load(open(scen_file)) if scen_file else {}
    if spec.get("demand"):  # scenarios built on a demand variant (urbanizacao, pmus2030)
        base_trips = base_trips.replace("trips.xml", f"trips_{spec['demand']}.xml")
    if spec.get("bus_lines"):
        mode_shift(base_trips, scen_net, out.replace(".trips.xml", ".bus.add.xml"), out)
        sys.exit()
    e = spec.get("elasticity")
    if not e:
        shutil.copyfile(base_trips, out)
        sys.exit()
    nb, ns = sumolib.net.readNet(base_net), sumolib.net.readNet(scen_net)
    root = ET.parse(base_trips).getroot()
    trips = [t for t in root.findall("trip") if t.get("type") == "car"]  # vans are not induced
    # one representative origin/destination edge per cell: the most used one
    use = defaultdict(lambda: defaultdict(int))
    for t in trips:
        use[cell(nb, t.get("from"))][t.get("from")] += 1
        use[cell(nb, t.get("to"))][t.get("to")] += 1
    rep = {c: max(d, key=d.get) for c, d in use.items()}
    factor = {}
    for c, src in rep.items():
        tb, ts = times_from(nb, src), times_from(ns, src)
        for c2, dst in rep.items():
            if dst in tb and dst in ts:
                factor[c, c2] = (ts[dst] / tb[dst]) ** e
    rng, out_trips, added, dropped = random.Random(1), [], 0, 0
    for t in trips:
        f = factor.get((cell(nb, t.get("from")), cell(nb, t.get("to"))), 1.0)
        if f < 1 and rng.random() > f:
            dropped += 1
            continue
        out_trips.append(t)
        if f > 1 and rng.random() < f - 1:
            dup = copy.deepcopy(t)  # keeps the parking stop inside
            dup.set("id", t.get("id") + "i")
            dup.set("depart", f"{max(0.0, float(t.get('depart')) + rng.uniform(-300, 300)):.1f}")
            out_trips.append(dup)
            added += 1
    # SUMO needs the whole file (cars, vans, pedestrians) sorted by departure
    keep = [c for c in root if c.tag != "vType" and not (c.tag == "trip" and c.get("type") == "car")] + out_trips
    types = root.findall("vType")
    for c in list(root):
        root.remove(c)
    for c in types + sorted(keep, key=lambda c: float(c.get("depart"))):
        root.append(c)
    ET.ElementTree(root).write(out)
    faster = sum(1 for v in factor.values() if v > 1.001)
    print(f"elasticity {e}: {faster}/{len(factor)} zone pairs faster; +{added} induced, -{dropped} trips "
          f"({len(out_trips)} vs {len(trips)})")
