"""KPIs + interactive map for one scenario against the baseline.

usage: report.py OUT_DIR SCENARIO      -> OUT_DIR/SCENARIO/report.html (open in a browser)
With SCENARIO=base the map shows the baseline alone.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402

out, scen = sys.argv[1:3]
runs = ["base"] if scen == "base" else ["base", scen]


def kpis(run):
    k = defaultdict(float)
    for _, el in ET.iterparse(f"{out}/{run}/tripinfo.xml"):
        if el.tag == "tripinfo":
            k["trips"] += 1
            k["veh_km"] += float(el.get("routeLength")) / 1000
            k["veh_h"] += float(el.get("duration")) / 3600
            k["delay_h"] += float(el.get("timeLoss")) / 3600
            k["depart_delay_h"] += float(el.get("departDelay")) / 3600
            em = el.find("emissions")
            k["co2_t"] += float(em.get("CO2_abs")) / 1e9 if em is not None else 0
            el.clear()
    st = ET.parse(f"{out}/{run}/stats.xml").getroot()
    k["not_inserted"] = int(st.find("vehicles").get("loaded")) - int(st.find("vehicles").get("inserted"))
    k["teleports"] = int(st.find("teleports").get("total"))
    k["mean_trip_min"] = k["veh_h"] * 60 / k["trips"]
    k["mean_delay_min"] = k["delay_h"] * 60 / k["trips"]
    return dict(k)


def hourly(run):
    """edge -> {'veh': [24], 'speed': [24]}"""
    d = defaultdict(lambda: {"veh": [0] * 24, "speed": [None] * 24})
    for iv in ET.parse(f"{out}/{run}/edgedata.xml").getroot():
        h = int(float(iv.get("begin")) // 3600)
        if h > 23:
            continue
        for e in iv:
            d[e.get("id")]["veh"][h] = int(float(e.get("entered", 0)))
            if e.get("speed"):
                d[e.get("id")]["speed"][h] = round(float(e.get("speed")) * 3.6, 1)
    return d


net = sumolib.net.readNet(f"{out}/{scen}.net.xml")
data = {r: hourly(r) for r in runs}
edges = []
for e in net.getEdges():
    if not any(e.getID() in data[r] for r in runs):
        continue
    edges.append({
        "id": e.getID(), "name": e.getName(), "limit": round(e.getSpeed() * 3.6),
        "new": e.getID().lstrip("-").startswith("scn"),
        "coords": [[round(c, 6) for c in net.convertXY2LonLat(x, y)][::-1] for x, y in e.getShape()],
        **{r: data[r].get(e.getID()) for r in runs},
    })
K = {r: kpis(r) for r in runs}
payload = {"scenario": scen, "runs": runs, "kpis": K, "edges": edges,
           "zones": json.load(open(f"{out}/zones.json"))["zones"]}

for key, label in [("trips", "viagens concluídas"), ("veh_km", "veículos·km"), ("veh_h", "veículos·hora"),
                   ("delay_h", "atraso total (h)"), ("mean_trip_min", "duração média (min)"),
                   ("mean_delay_min", "atraso médio (min)"), ("co2_t", "CO₂ (t)"),
                   ("teleports", "teleportes (bloqueios)"), ("not_inserted", "não inseridos")]:
    row = "  ".join(f"{K[r][key]:>12,.1f}" for r in runs)
    diff = f"  {(K[scen][key] - K['base'][key]) / K['base'][key] * 100 if K['base'][key] else 0:+6.1f}%" if len(runs) > 1 else ""
    print(f"{label:24}{row}{diff}")

html = open(os.path.join(os.path.dirname(__file__), "report.html")).read()
dest = f"{out}/{scen}/report.html"
open(dest, "w").write(html.replace("/*DATA*/null", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
print(f"map: {dest}")
