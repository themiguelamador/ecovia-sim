"""Local accessibility: travel time of car trips starting or ending in a few areas.

The citywide totals can hide a local gain (or loss). For each area, the mean duration of
the car trips that start or end within its radius, per scenario and seed, paired with the
base run of the same seed (mean and 95% CI over seeds).

usage: local.py OUT_DIR OUT_JSON
"""
import glob
import json
import math
import os
import statistics
import sys
import xml.etree.ElementTree as ET

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402

AREAS = [  # name, lon, lat, radius m
    ("Campus da Justiça / Parque da Cidade", -8.27367, 41.44783, 500),
    ("Costa (encosta da Ecovia)", -8.2835, 41.4400, 600),
    ("Urgezes", -8.2950, 41.4280, 600),
    ("Centro histórico (Toural)", -8.2956, 41.4414, 400),
]
T975 = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31, 10: 2.26}

out_dir, out_json = sys.argv[1:3]
net = sumolib.net.readNet(f"{out_dir}/base.net.xml")
centres = [net.convertLonLat2XY(lon, lat) for _, lon, lat, _ in AREAS]
area_of = {}
for e in net.getEdges():
    x, y = e.getShape()[len(e.getShape()) // 2]
    area_of[e.getID()] = [i for i, (c, a) in enumerate(zip(centres, AREAS)) if math.dist((x, y), c) <= a[3]]


def run_means(path):
    s, n = [0.0] * len(AREAS), [0] * len(AREAS)
    for _, el in ET.iterparse(path):
        if el.tag == "tripinfo":
            if el.get("vType") == "car" and float(el.get("arrival")) >= 0:
                hit = set(area_of.get(el.get("departLane", "").rsplit("_", 1)[0], [])) | set(area_of.get(el.get("arrivalLane", "").rsplit("_", 1)[0], []))
                for i in hit:
                    s[i] += float(el.get("duration")) / 60
                    n[i] += 1
            el.clear()
    return [s[i] / n[i] if n[i] else None for i in range(len(AREAS))], n


def ci(xs):
    m = statistics.fmean(xs)
    h = T975.get(len(xs), 2.0) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0
    return {"mean": m, "lo": m - h, "hi": m + h}


scens = sorted({p.split("/")[-3] for p in glob.glob(f"{out_dir}/*/seed*/tripinfo.xml")}, key=lambda s: (s != "base", s))
res = {s: {os.path.basename(os.path.dirname(p)): run_means(p) for p in sorted(glob.glob(f"{out_dir}/{s}/seed*/tripinfo.xml"))} for s in scens}
out = []
for i, (name, lon, lat, r) in enumerate(AREAS):
    base = [v[0][i] for v in res["base"].values()]
    row = {"area": name, "lonlat": [lon, lat], "radius_m": r, "trips": round(statistics.fmean(v[1][i] for v in res["base"].values())),
           "base_min": statistics.fmean(base), "scenarios": {}}
    for s in scens[1:]:
        seeds = sorted(set(res[s]) & set(res["base"]))
        d = ci([res[s][k][0][i] - res["base"][k][0][i] for k in seeds])
        d["pct"] = d["mean"] / row["base_min"] * 100
        row["scenarios"][s] = d
    out.append(row)
json.dump(out, open(out_json, "w"), ensure_ascii=False, indent=1)
for row in out:
    print(f"{row['area']:38} {row['trips']:6} viagens/dia  base {row['base_min']:.1f} min")
    for s, d in row["scenarios"].items():
        flag = "" if d["lo"] <= 0 <= d["hi"] else "  <-- fora do ruído"
        print(f"   {s:30} {d['mean']:+5.2f} min ({d['pct']:+5.1f}%)  IC [{d['lo']:+.2f}, {d['hi']:+.2f}]{flag}")
