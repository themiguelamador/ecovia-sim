"""Apply a scenario to the base network.

A scenario is a GeoJSON FeatureCollection of LineStrings (draw them on geojson.io).
Feature properties:
  action     "add" (default) | "modify" | "remove"
  lanes      lanes per direction           (add: default 1; modify: optional)
  speed_kmh  speed limit                   (add: default 50; modify: optional)
  oneway     true = only in drawing direction (add only)
  name       street name                   (add only)
"add" line ends snap to an existing junction within 60 m (traced PDM lines are only ~15 m
accurate), or to another added line's end, else they become new junctions. A new junction
left as a loose end (one road only) is joined by a straight connector to the nearest
existing junction within 250 m: a planned road ends on a street, not in a field. "modify"/"remove"
affect every existing road segment lying within 25 m of the line along its whole length.

A scenario can also pull features from other files, filtered by property:
  "include": [{"file": "../data/pdm/tracado.geojson", "group": ["ecovia"], "kind": "nova"}]
(paths relative to the scenario file; list values match any, scalars match exactly).

usage: scenario.py BASE_NET SCENARIO.geojson OUT_NET
"""
import json
import math
import os
import subprocess
import sys
import tempfile

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402

SNAP_M, MATCH_M, CONNECT_M = 60, 25, 250


def seg_dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def line_dist(p, line):
    return min(seg_dist(p, line[i], line[i + 1]) for i in range(len(line) - 1))


def near_line(edge, line):
    shape = edge.getShape()
    # sample the edge every ~10 m so a long edge merely touching the line doesn't match
    pts = [p for a, b in zip(shape, shape[1:])
           for k in range(max(1, int(math.dist(a, b) / 10)))
           for p in [(a[0] + (b[0] - a[0]) * k / max(1, int(math.dist(a, b) / 10)),
                      a[1] + (b[1] - a[1]) * k / max(1, int(math.dist(a, b) / 10)))]] + [shape[-1]]
    return all(line_dist(p, line) <= MATCH_M for p in pts)


def build(net, features):
    nodes, edges, remove, new_nodes, conns = [], [], [], [], []
    degree = {}

    def feed(nid, new_edge, shape):
        # netconvert keeps a patched net's existing turning movements and ignores lane-less
        # connections, so roads already at a snapped junction are wired in lane by lane:
        # left turns from the leftmost lane, everything else from the rightmost.
        if nid in {n[0] for n in new_nodes}:
            return
        (ox, oy), (px, py) = shape[0], shape[1]
        for e in net.getNode(nid).getIncoming():
            (ax, ay), (bx, by) = e.getShape()[-2:]
            left = (bx - ax) * (py - oy) - (by - ay) * (px - ox) > 0
            lane = e.getLaneNumber() - 1 if left else 0
            conns.append(f'  <connection from="{e.getID()}" to="{new_edge}" fromLane="{lane}" toLane="0"/>')

    def snap(x, y):
        for nid, nx, ny in new_nodes:
            if math.hypot(x - nx, y - ny) <= SNAP_M:
                return nid
        best = min(net.getNodes(), key=lambda n: math.dist(n.getCoord(), (x, y)))
        if math.dist(best.getCoord(), (x, y)) <= SNAP_M:
            return best.getID()
        nid = f"scn{len(new_nodes)}"
        new_nodes.append((nid, x, y))
        nodes.append(f'  <node id="{nid}" x="{x:.2f}" y="{y:.2f}"/>')
        return nid

    for i, f in enumerate(features):
        pr = f.get("properties") or {}
        line = [net.convertLonLat2XY(lon, lat) for lon, lat in f["geometry"]["coordinates"]]
        action = pr.get("action", "add")
        if action == "add":
            a, b = snap(*line[0]), snap(*line[-1])
            if a == b:
                continue  # short piece whose ends snap to the same junction
            degree[a], degree[b] = degree.get(a, 0) + 1, degree.get(b, 0) + 1
            attrs = (f'numLanes="{pr.get("lanes", 1)}" speed="{pr.get("speed_kmh", 50) / 3.6:.2f}" '
                     f'priority="9" allow="passenger bus truck delivery emergency"')
            if pr.get("name") or pr.get("corridor"):
                attrs += f' name="{pr.get("name") or pr["corridor"]}"'
            shape = " ".join(f"{x:.2f},{y:.2f}" for x, y in line)
            edges.append(f'  <edge id="scn{i}" from="{a}" to="{b}" {attrs} shape="{shape}"/>')
            feed(a, f"scn{i}", line)
            if not pr.get("oneway"):
                rshape = " ".join(f"{x:.2f},{y:.2f}" for x, y in reversed(line))
                edges.append(f'  <edge id="-scn{i}" from="{b}" to="{a}" {attrs} shape="{rshape}"/>')
                feed(b, f"-scn{i}", line[::-1])
        else:
            hits = [e for e in net.getEdges() if near_line(e, line)]
            if not hits:
                sys.exit(f"feature {i}: no existing road within {MATCH_M} m of the whole line")
            for e in hits:
                if action == "remove":
                    remove.append(e.getID())
                else:
                    upd = ""
                    if "lanes" in pr:
                        upd += f' numLanes="{pr["lanes"]}"'
                    if "speed_kmh" in pr:
                        upd += f' speed="{pr["speed_kmh"] / 3.6:.2f}"'
                    edges.append(f'  <edge id="{e.getID()}"{upd}/>')
            print(f"feature {i} ({action}): {len(hits)} edges: {' '.join(e.getID() for e in hits)}")
    for nid, x, y in list(new_nodes):
        if degree.get(nid) != 1:
            continue
        best = min(net.getNodes(), key=lambda n: math.dist(n.getCoord(), (x, y)))
        if math.dist(best.getCoord(), (x, y)) > CONNECT_M:
            continue
        bx, by = best.getCoord()
        attrs = 'numLanes="1" speed="13.89" priority="9" allow="passenger bus truck delivery emergency" name="Ligação à rua mais próxima (assumida)"'
        edges.append(f'  <edge id="scnc_{nid}" from="{nid}" to="{best.getID()}" {attrs}/>')
        edges.append(f'  <edge id="-scnc_{nid}" from="{best.getID()}" to="{nid}" {attrs}/>')
        feed(best.getID(), f"-scnc_{nid}", [(bx, by), (x, y)])
    return nodes, edges, remove, conns


def load_features(path):
    fc = json.load(open(path))
    feats = list(fc.get("features", []))
    for inc in fc.get("include", []):
        crit = {k: v for k, v in inc.items() if k != "file"}
        for f in load_features(os.path.join(os.path.dirname(path), inc["file"])):
            pr = f.get("properties") or {}
            if all(pr.get(k) in (v if isinstance(v, list) else [v]) for k, v in crit.items()):
                feats.append(f)
    return feats


if __name__ == "__main__":
    base, scen, out = sys.argv[1:4]
    net = sumolib.net.readNet(base)
    nodes, edges, remove, conns = build(net, load_features(scen))
    tmp = tempfile.mkdtemp()
    open(f"{tmp}/n.nod.xml", "w").write("<nodes>\n" + "\n".join(nodes) + "\n</nodes>\n")
    open(f"{tmp}/e.edg.xml", "w").write("<edges>\n" + "\n".join(edges) + "\n</edges>\n")
    open(f"{tmp}/c.con.xml", "w").write("<connections>\n" + "\n".join(conns) + "\n</connections>\n")
    cmd = [os.path.join(os.environ["SUMO_HOME"], "bin", "netconvert"), "--sumo-net-file", base,
           "-n", f"{tmp}/n.nod.xml", "-e", f"{tmp}/e.edg.xml", "-x", f"{tmp}/c.con.xml", "-o", out, "--no-warnings"]
    if remove:
        cmd += ["--remove-edges.explicit", ",".join(remove)]
    subprocess.run(cmd, check=True)
    # guard: a new road nobody can drive onto silently carries zero traffic
    patched = sumolib.net.readNet(out)
    new_ids = {n.split('"')[1] for n in nodes}
    dead = [e.getID() for e in patched.getEdges()
            if e.getID().lstrip("-").startswith("scn") and not e.getIncoming() and e.getFromNode().getID() not in new_ids]
    if dead:
        sys.exit(f"new edges with no way in: {dead}")
