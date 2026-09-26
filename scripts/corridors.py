"""Corridors, city centre and peak hours: does a scenario change traffic where it matters?

For a few main streets, the city centre and the whole network, per run (edgedata.xml, hourly):
  veh_day    vehicles per day per direction on a street (length-weighted mean of its segments)
  vkm_day    vehicle-kilometres per day inside the centre
  speed_am   mean speed 8h-10h (km/h): vehicle-km / vehicle-hours on the street or area
  speed_pm   same, 17h-19h
  loss_am    vehicle-hours lost to congestion 8h-10h (whole network)
  loss_pm    same, 17h-19h
Each scenario is compared with its reference (as in export_web.py), paired by seed, 95% CI.

usage: corridors.py OUT_DIR OUT_JSON
"""
import glob
import json
import math
import os
import re
import statistics
import sys
from multiprocessing import Pool

sys.path.append(os.path.join(os.environ["SUMO_HOME"], "tools"))
import sumolib  # noqa: E402

AM, PM = (8, 10), (17, 19)
STREETS = {  # label: street name in OpenStreetMap
    "Av. D. João IV": "Avenida Dom João IV",
    "Av. Conde de Margaride": "Avenida Conde de Margaride",
    "Alameda de S. Dâmaso": "Alameda de São Dâmaso",
    "Av. de S. Gonçalo": "Avenida de São Gonçalo",
    "Rua Padre António Caldas": "Rua Padre António Caldas",
    "Av. Rio de Janeiro": "Avenida Rio de Janeiro",
    "Rua Jaime Martins": "Rua Jaime Martins",
    "Alameda Mariano Felgueiras": "Alameda Mariano Felgueiras",
}
CENTRE, CENTRE_M = (-8.29508, 41.44067), 700  # Largo do Toural
T975 = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36, 9: 2.31, 10: 2.26}
EDGE = re.compile(r'<edge id="([^"]+)" sampledSeconds="([0-9.]+)".*?timeLoss="([0-9.]+)".*?entered="([0-9.]+)"')


def groups(net):
    """edge id -> (length, [groups]) for base-network edges cars use"""
    cx, cy = net.convertLonLat2XY(*CENTRE)
    by_name = {v: k for k, v in STREETS.items()}
    g = {}
    for e in net.getEdges():
        if not e.allows("passenger"):
            continue
        s = e.getShape()
        x, y = s[len(s) // 2]
        gs = [by_name[e.getName()]] if e.getName() in by_name else []
        if e.getType().split("|")[0] == "highway.trunk":
            gs.append("Via rápida")
        if math.hypot(x - cx, y - cy) <= CENTRE_M:
            gs.append("Centro")
        g[e.getID()] = (e.getLength(), gs)
    return g


def run_metrics(args):
    path, g = args
    acc = {}  # group -> [veh*m day, m, vkm am, vh am, vkm pm, vh pm]
    loss = {"am": 0.0, "pm": 0.0}
    h = None
    for line in open(path, errors="ignore"):
        if "<interval" in line:
            h = int(float(re.search(r'begin="([0-9.]+)"', line)[1]) // 3600)
            continue
        m = EDGE.search(line)
        if not m:
            continue
        eid, samp, tl, ent = m[1], float(m[2]), float(m[3]), float(m[4])
        if AM[0] <= h < AM[1]:
            loss["am"] += tl / 3600
        if PM[0] <= h < PM[1]:
            loss["pm"] += tl / 3600
        if eid not in g:
            continue
        L, gs = g[eid]
        for grp in gs:
            a = acc.setdefault(grp, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            a[0] += ent * L
            if AM[0] <= h < AM[1]:
                a[2] += ent * L / 1000
                a[3] += samp / 3600
            if PM[0] <= h < PM[1]:
                a[4] += ent * L / 1000
                a[5] += samp / 3600
    lengths = {}
    for eid, (L, gs) in g.items():
        for grp in gs:
            lengths[grp] = lengths.get(grp, 0) + L
    out = {"Cidade inteira": {"loss_am": loss["am"], "loss_pm": loss["pm"]}}
    for grp, a in acc.items():
        r = out.setdefault(grp, {})
        if grp == "Centro":
            r["vkm_day"] = a[0] / 1000
        else:
            r["veh_day"] = a[0] / lengths[grp]
        r["speed_am"] = a[2] / a[3] if a[3] else None
        r["speed_pm"] = a[4] / a[5] if a[5] else None
    return path, out


def ci(xs):
    m = statistics.fmean(xs)
    half = T975.get(len(xs), 2.0) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0
    return {"mean": m, "lo": m - half, "hi": m + half}


if __name__ == "__main__":
    out_dir, out_json = sys.argv[1:3]
    g = groups(sumolib.net.readNet(f"{out_dir}/base.net.xml"))
    runs = sorted(p for p in glob.glob(f"{out_dir}/*/seed*/edgedata.xml")
                  if "teleports" in open(os.path.join(os.path.dirname(p), "stats.xml")).read())
    with Pool(8) as pool:
        res = dict(pool.map(run_metrics, [(p, g) for p in runs]))
    per = {}  # scenario -> seed -> metrics
    for p, r in res.items():
        seed_dir = os.path.dirname(p)
        per.setdefault(os.path.basename(os.path.dirname(seed_dir)), {})[os.path.basename(seed_dir)] = r
    ref = {s: (json.load(open(f"scenarios/{s}.geojson")).get("reference", "base") if s != "base" else None) for s in per}
    UNIT = {"veh_day": "veíc./dia", "vkm_day": "veíc.·km/dia", "speed_am": "km/h", "speed_pm": "km/h",
            "loss_am": "veíc.·h", "loss_pm": "veíc.·h"}
    rows = []
    for grp in ["Cidade inteira", "Centro", *STREETS, "Via rápida"]:
        for met in ["veh_day", "vkm_day", "speed_am", "speed_pm", "loss_am", "loss_pm"]:
            vals = {s: [r[grp][met] for r in seeds.values() if grp in r and r[grp].get(met) is not None] for s, seeds in per.items()}
            if not vals.get("base"):
                continue
            row = {"name": grp, "metric": met, "unit": UNIT[met], "higher_better": met.startswith("speed"),
                   "value": {s: round(statistics.fmean(v), 1) for s, v in vals.items() if v}, "diff": {}}
            for s, rs in ref.items():
                if not rs:
                    continue
                common = sorted(set(per[s]) & set(per[rs]))
                d = [per[s][k][grp][met] - per[rs][k][grp][met] for k in common
                     if grp in per[s][k] and grp in per[rs][k] and per[s][k][grp].get(met) is not None and per[rs][k][grp].get(met) is not None]
                if len(d) >= 2:
                    c = ci(d)
                    base_mean = statistics.fmean(per[rs][k][grp][met] for k in common)
                    row["diff"][s] = {k: round(v, 2) for k, v in c.items()} | {"pct": round(c["mean"] / base_mean * 100, 2)}
            rows.append(row)
    json.dump({"am": AM, "pm": PM, "centre_m": CENTRE_M, "rows": rows}, open(out_json, "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"{len(rows)} rows, {len(runs)} runs")
    for r in rows:
        if r["name"] in ("Cidade inteira", "Centro", "Av. D. João IV"):
            print(f'{r["name"]:16s} {r["metric"]:9s} base {r["value"]["base"]:9.1f}  ' + "  ".join(
                f'{s[:3]} {d["pct"]:+.1f}%{"*" if d["lo"] > 0 or d["hi"] < 0 else ""}' for s, d in r["diff"].items() if s[:2] in ("s1", "s2", "u1", "b1", "b2", "p0", "u0")))
