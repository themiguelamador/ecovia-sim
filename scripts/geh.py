"""Compare simulated hourly volumes with traffic counts (GEH statistic).

counts.csv columns: label, lon, lat, bearing, hour, count
  bearing = direction of travel counted, degrees clockwise from north (0 = northbound)
  hour    = start hour (8 = 8:00-9:00)
Target used in traffic studies: GEH < 5 on at least 85% of counts.

usage: geh.py NET EDGEDATA COUNTS
"""
import csv
import math
import os
import sys
import xml.etree.ElementTree as ET


def geh(model, count):
    return math.sqrt(2 * (model - count) ** 2 / (model + count)) if model + count else 0.0


def edge_bearing(shape, x, y):
    i = min(range(len(shape) - 1), key=lambda i: math.dist(shape[i], (x, y)) + math.dist(shape[i + 1], (x, y)))
    (ax, ay), (bx, by) = shape[i], shape[i + 1]
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360


if __name__ == "__main__":
    sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
    import sumolib

    net_file, edgedata, counts = sys.argv[1:4]
    net = sumolib.net.readNet(net_file)
    vol = {}
    for iv in ET.parse(edgedata).getroot():
        h = int(float(iv.get("begin")) // 3600)
        for e in iv:
            vol[e.get("id"), h] = float(e.get("entered", 0))
    rows, ok = list(csv.DictReader(open(counts))), 0
    print(f"{'contagem':32} {'h':>3} {'real':>6} {'modelo':>7} {'GEH':>5}  via")
    for r in rows:
        x, y = net.convertLonLat2XY(float(r["lon"]), float(r["lat"]))
        cands = [(d, e) for e, d in net.getNeighboringEdges(x, y, 40)
                 if abs((edge_bearing(e.getShape(), x, y) - float(r["bearing"]) + 180) % 360 - 180) < 45]
        if not cands:
            print(f"{r['label']:32} no road within 40 m in that direction")
            continue
        e = min(cands, key=lambda c: c[0])[1]
        m, c = vol.get((e.getID(), int(r["hour"])), 0), float(r["count"])
        g = geh(m, c)
        ok += g < 5
        print(f"{r['label']:32} {r['hour']:>3} {c:6.0f} {m:7.0f} {g:5.1f}  {e.getName() or e.getID()}")
    print(f"\nGEH < 5: {ok}/{len(rows)} ({ok / max(1, len(rows)):.0%}); target >= 85%")
