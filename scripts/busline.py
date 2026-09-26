"""New bus lines of a scenario: routes, stops and timetable.

A scenario GeoJSON may carry
  "bus_lines": [{"id": "L1", "name": "Estação – Campus da Justiça",
                 "via": [[lon, lat], ...]}]      # terminal, waypoints, terminal
Each line runs both ways along the fastest bus route through its waypoints (so it uses a
new road only if the scenario has one there and it is faster, or a waypoint sits on it),
with a stop every [bus].stop_spacing_m and the headways of [bus] in params.toml.

usage: busline.py NET SCENARIO.geojson PARAMS.toml OUT_ROUTES OUT_STOPS
"""
import json
import os
import sys
import tomllib

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402


def candidates(net, lon, lat):
    """bus edges at a waypoint: the nearest one and anything within 15 m more (both directions
    of a two-way street), so the route can take the one that suits its direction"""
    x, y = net.convertLonLat2XY(lon, lat)
    es = [(e, e.getClosestLanePosDist((x, y))[2]) for e, _ in net.getNeighboringEdges(x, y, 300) if e.allows("bus")]
    best = min(d for _, d in es)
    return [e for e, d in es if d <= best + 15]


def route(net, via):
    """fastest bus route through the waypoints, choosing each waypoint's edge (dynamic programming)"""
    layers = [candidates(net, lon, lat) for lon, lat in via]
    best = {e: (0.0, [e]) for e in layers[0]}
    for layer in layers[1:]:
        nxt = {}
        for b in layer:
            for a, (c, p) in best.items():
                q, cost = net.getFastestPath(a, b, vClass="bus")
                if q and (b not in nxt or c + cost < nxt[b][0]):
                    nxt[b] = (c + cost, p + list(q[1:]))
        if not nxt:
            sys.exit(f"no bus route to waypoint {layer[0].getID()}")
        best = nxt
    return min(best.values(), key=lambda t: t[0])[1]


def stops(path, spacing, prefix):
    """(stop id, lane id, start, end): a 15 m stop every `spacing` metres, first and last edge included"""
    out, since = [], spacing
    for i, e in enumerate(path):
        lane = next(l for l in e.getLanes() if l.allows("bus"))
        if e.getLength() >= 30 and (since >= spacing or i == len(path) - 1):
            mid = e.getLength() / 2
            out.append((f"{prefix}.{len(out)}", lane.getID(), mid - 7.5, mid + 7.5))
            since = e.getLength() / 2
        else:
            since += e.getLength()
    return out


if __name__ == "__main__":
    net_file, scen_file, params_file, out_rou, out_add = sys.argv[1:6]
    B = tomllib.load(open(params_file, "rb"))["bus"]
    net = sumolib.net.readNet(net_file)
    rou, add, vehicles = ["<routes>"], ["<additional>"], []
    for line in json.load(open(scen_file)).get("bus_lines", []):
        for d, via in (("a", line["via"]), ("b", line["via"][::-1])):
            rid = f"{line['id']}{d}"
            path = route(net, via)
            st = stops(path, B["stop_spacing_m"], rid)
            add += [f'    <busStop id="{s}" lane="{ln}" startPos="{a:.1f}" endPos="{b:.1f}" friendlyPos="true" name="{line["name"]}"/>'
                    for s, ln, a, b in st]
            rou.append(f'    <route id="{rid}" edges="{" ".join(e.getID() for e in path)}">')
            rou += [f'        <stop busStop="{s}" duration="{B["dwell_s"]}"/>' for s, *_ in st]
            rou.append("    </route>")
            t, n = B["first_h"] * 3600, 0
            while t < B["last_h"] * 3600:
                vehicles.append((t, f'    <vehicle id="{rid}.{n}" route="{rid}" type="bus" depart="{t}" line="{line["id"]}"/>'))
                peak = any(a <= t / 3600 < b for a, b in B["peak_hours"])
                t += 60 * (B["peak_headway_min"] if peak else B["offpeak_headway_min"])
                n += 1
            km = sum(e.getLength() for e in path) / 1000
            print(f"{rid} {line['name']}: {km:.1f} km, {len(st)} stops, {n} departures")
    rou += [v for _, v in sorted(vehicles)]  # SUMO wants departures sorted across the file
    open(out_rou, "w").write("\n".join(rou + ["</routes>"]) + "\n")
    open(out_add, "w").write("\n".join(add + ["</additional>"]) + "\n")
