"""Pedestrian crossings from OpenStreetMap -> SUMO crossings.

Most crossings in Guimarães are mapped as a node on the road (highway=crossing) with no
footway through it, so netconvert's own --osm.crossings imports none of them. Each node is
placed on the nearest road with a sidewalk:
  - within JUNCTION_M of a junction: a crossing at that junction over that road;
  - otherwise (mid-block): the road is split there, both directions at the same point, and
    the crossing is put on the new node.
Marked crossings (zebra / marked / uncontrolled, i.e. "passadeira") give pedestrians
priority: cars must stop for anyone crossing. Unmarked ones don't.
# ponytail: one mid-block split per road segment; a second crossing on the same segment is
# snapped to the nearest end instead. Upgrade: sort splits by position and chain them.

usage: crossings.py NET OSM OUT_EDGES OUT_CONNECTIONS
"""
import gzip
import os
import sys
import xml.etree.ElementTree as ET

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402
from sumolib.geomhelper import polygonOffsetWithMinimumDistanceToPoint  # noqa: E402

JUNCTION_M, SEARCH_M = 25, 12
PRIORITY = {"zebra", "marked", "uncontrolled", "traffic_signals", None}

net_file, osm_file, out_edg, out_con = sys.argv[1:5]
net = sumolib.net.readNet(net_file)


def reverse(e):
    return next((r for r in e.getToNode().getOutgoing() if r.getToNode() == e.getFromNode() and r.getID() != e.getID()), None)


def walkable(e):
    return any(l.allows("pedestrian") for l in e.getLanes()) and e.allows("passenger")


at_junction, splits, n_nodes = {}, {}, 0
for _, el in ET.iterparse(gzip.open(osm_file)):
    if el.tag != "node":
        continue
    tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
    if tags.get("highway") != "crossing":
        el.clear()
        continue
    n_nodes += 1
    x, y = net.convertLonLat2XY(float(el.get("lon")), float(el.get("lat")))
    el.clear()
    cands = [(d, e) for e, d in net.getNeighboringEdges(x, y, SEARCH_M) if walkable(e)]
    if not cands:
        continue
    e = min(cands, key=lambda c: c[0])[1]
    pos = polygonOffsetWithMinimumDistanceToPoint((x, y), e.getShape())
    r = reverse(e)
    prio = tags.get("crossing") in PRIORITY
    if pos > JUNCTION_M and e.getLength() - pos > JUNCTION_M and e.getID() not in splits and (r is None or r.getID() not in splits):
        splits[e.getID()] = (pos, r, prio)
        continue
    node = e.getFromNode() if pos <= e.getLength() / 2 else e.getToNode()
    crossed = tuple(sorted({e.getID()} | ({r.getID()} if r else set())))
    at_junction[(node.getID(), crossed)] = at_junction.get((node.getID(), crossed), False) or prio

edges, conns = [], []
for i, (eid, (pos, r, prio)) in enumerate(splits.items()):
    nid = f"xing{i}"
    e = net.getEdge(eid)
    edges.append(f'  <edge id="{eid}"><split pos="{pos:.2f}" id="{nid}" idBefore="{eid}_x{i}a" idAfter="{eid}_x{i}b"/></edge>')
    crossed = f"{eid}_x{i}b"
    if r is not None:
        rid = r.getID()
        edges.append(f'  <edge id="{rid}"><split pos="{r.getLength() - pos:.2f}" id="{nid}" idBefore="{rid}_x{i}a" idAfter="{rid}_x{i}b"/></edge>')
        crossed += f" {rid}_x{i}a"  # both directions on the same side of the new node
    conns.append(f'  <crossing node="{nid}" edges="{crossed}" priority="{str(prio).lower()}"/>')
# a junction crossing may name a road that a mid-block crossing split: use the part touching the junction
part = {}
for i, (eid, (pos, r, prio)) in enumerate(splits.items()):
    for x in [net.getEdge(eid)] + ([r] if r is not None else []):
        part[x.getID()] = {x.getFromNode().getID(): f"{x.getID()}_x{i}a", x.getToNode().getID(): f"{x.getID()}_x{i}b"}
for (nid, crossed), prio in at_junction.items():
    ids = " ".join(part[c][nid] if c in part else c for c in crossed)
    conns.append(f'  <crossing node="{nid}" edges="{ids}" priority="{str(prio).lower()}"/>')

open(out_edg, "w").write("<edges>\n" + "\n".join(edges) + "\n</edges>\n")
open(out_con, "w").write("<connections>\n" + "\n".join(conns) + "\n</connections>\n")
print(f"{n_nodes} OSM crossing nodes -> {len(at_junction)} at junctions, {len(splits)} mid-block "
      f"({sum(1 for c in conns if 'priority=\"true\"' in c)} with pedestrian priority)")
