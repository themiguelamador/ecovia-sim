# /// script
# dependencies = ["pillow", "numpy", "scikit-image"]
# ///
"""Georeference the PDM image (data/pdm/vias-propostas.png) and trace its red lines.

The image has no coordinates. A similarity transform (scale, rotation, shift) is fitted by
least squares on landmarks visible both in the image and in OpenStreetMap (CONTROL), then
checked by drawing OpenStreetMap roads over the image (data/pdm/overlay.png).

Then the red lines are traced (mask -> skeleton -> polylines), converted to lon/lat and
classified: a segment lying along an existing OSM road for most of its length is an
upgrade of that road ("existente"), otherwise a new road ("nova"). Segments are grouped
by the PDM corridors labelled in the image (GROUPS).

usage: uv run tools/georef.py   -> data/pdm/transform.json, overlay.png, tracado.geojson
"""
import gzip
import json
import math
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw
from skimage.morphology import closing, disk, skeletonize

IMG = "data/pdm/vias-propostas.png"
LAT0, LON0 = 41.44, -8.29
KX, KY = 111320 * math.cos(math.radians(LAT0)), 110574  # metres per degree


def to_m(lonlat):
    a = np.asarray(lonlat, float)
    return np.c_[(a[:, 0] - LON0) * KX, (a[:, 1] - LAT0) * KY]


def to_lonlat(m):
    return np.c_[m[:, 0] / KX + LON0, m[:, 1] / KY + LAT0]


PROPOSTA = [[-8.29234, 41.43452], [-8.29151, 41.43481], [-8.29106, 41.43513], [-8.29043, 41.4363], [-8.28882, 41.43698], [-8.28779, 41.43753], [-8.28685, 41.439], [-8.28548, 41.43962], [-8.28458, 41.43972], [-8.28375, 41.43993], [-8.28285, 41.44068], [-8.28225, 41.44095], [-8.28045, 41.44106], [-8.27989, 41.44121], [-8.27944, 41.44148], [-8.2792, 41.44215], [-8.27965, 41.44328], [-8.27967, 41.44391], [-8.27917, 41.44463], [-8.27824, 41.44499], [-8.27418, 41.44596]]


# (label, image pixel (x, y), OSM lon/lat)
CONTROL = [
    ("Estádio D. Afonso Henriques (relvado)", (402, 370), (-8.30103, 41.44587)),
    ("Rotunda Av. D. João IV (início da via)", (585, 683), (-8.29260, 41.43470)),
    ("Nó da Circular (trevo a SE)", (1520, 362), (-8.24780, 41.44500)),
]


# PDM corridors by image region (x0, y0, x1, y1), first match wins; names as in the image
GROUPS = [
    ("circular", "Ligação Parque da Cidade – Circular urbana", (900, 0, 1567, 300)),
    ("outras", "Outras vias propostas no PDM", (740, 400, 800, 520)),  # separate spur beside the Ecovia line
    ("ecovia", "Ligação D. João IV – Parque da Cidade (sobre a Ecovia)", (575, 340, 960, 700)),
    ("urgezes", "Ligações Centro cidade – Urgezes", (370, 620, 650, 1037)),
    ("outras", "Outras vias propostas no PDM", (0, 0, 1567, 1037)),
]
# Main PDM roads traced as ONE continuous path along the red line through these image
# waypoints (start, junctions on the way, end); each leg becomes one road, so the road only
# connects to the network at the waypoints. Tracing fragments along them are dropped.
MAIN_ROADS = [
    ("ecovia", "Ligação D. João IV – Parque da Cidade (sobre a Ecovia)",
     [(585, 682), (704, 561), (948, 358)]),     # D. João IV roundabout -> mid roundabout -> Parque da Cidade
    ("urgezes_main", "Ligação Av. D. João IV – Urgezes",
     [(585, 682), (540, 995)]),                 # same roundabout, straight south to Urgezes
    ("circular", "Ligação Parque da Cidade – Circular urbana",
     [(1043, 285), (1015, 165), (1017, 72)]),   # Parque da Cidade -> roundabout -> Circular interchange
]
# groups represented only by their main road; their other traced pieces become "outras"
MAIN_ONLY = {"ecovia": False, "circular": True}  # False = drop the pieces, True = keep them as "outras"
EXISTING_M, EXISTING_SHARE = 25, 0.7   # "existente" if >=70% of the segment is within 25 m of a road
DRIVABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
            "living_street", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}


def red_mask(im):
    r, g, b = (im[..., i].astype(int) for i in range(3))
    return (r > 150) & (g < 110) & (b < 110) & (r - g > 70)


def densify(m, step=5):
    out = []
    for a, b in zip(m, m[1:]):
        n = max(1, int(np.linalg.norm(b - a) / step))
        out += [a + (b - a) * k / n for k in range(n)]
    return np.array(out + [m[-1]])


def similarity(src, dst):
    """least-squares s, R, t with dst ~ s R src + t (Umeyama)"""
    ms, md = src.mean(0), dst.mean(0)
    a, b = src - ms, dst - md
    u, sig, vt = np.linalg.svd(b.T @ a / len(src))
    d = np.diag([1, np.sign(np.linalg.det(u @ vt))])
    R = u @ d @ vt
    s = (sig * np.diag(d)).sum() / a.var(0).sum()
    return s, R, md - s * R @ ms


def px_to_m(T, px):
    # image y grows downwards: flip so the fit is a proper (non-mirrored) similarity
    p = np.c_[px[:, 0], -px[:, 1]]
    return T["s"] * p @ np.array(T["R"]).T + T["t"]


def m_to_px(T, m):
    p = ((np.asarray(m) - T["t"]) @ np.array(T["R"])) / T["s"]
    return np.c_[p[:, 0], -p[:, 1]]


def skeleton(mask):
    return set(zip(*np.nonzero(skeletonize(closing(mask, disk(2))))))


def path_between(pts, a, b, gap=15, jump_cost=3.0):
    """cheapest path over skeleton pixels between the pixels nearest to a and b (x, y).
    The drawn line has small breaks, so hops of up to `gap` px are allowed, costing
    `jump_cost` times their length (the path follows the line wherever it can)."""
    import heapq
    arr = np.array(list(pts))
    near = lambda q: tuple(int(v) for v in arr[np.argmin(((arr - (q[1], q[0])) ** 2).sum(1))])
    start, goal = near(a), near(b)
    grid = {}
    for p in pts:
        grid.setdefault((p[0] // gap, p[1] // gap), []).append(p)
    best, prev, heap = {start: 0.0}, {start: None}, [(0.0, start)]
    while heap:
        c, p = heapq.heappop(heap)
        if p == goal:
            break
        if c > best[p]:
            continue
        gy, gx = p[0] // gap, p[1] // gap
        for cy in (gy - 1, gy, gy + 1):
            for cx in (gx - 1, gx, gx + 1):
                for q in grid.get((cy, cx), ()):
                    d = math.hypot(q[0] - p[0], q[1] - p[1])
                    if 0 < d <= gap:
                        nc = c + (d if d < 1.5 else d * jump_cost)
                        if nc < best.get(q, 1e18):
                            best[q], prev[q] = nc, p
                            heapq.heappush(heap, (nc, q))
    if goal not in prev:
        raise SystemExit(f"no red-line path between {a} and {b}")
    out, p = [], goal
    while p is not None:
        out.append((p[1], p[0]))
        p = prev[p]
    return np.array(out[::-1], float)


def trace(mask, min_px=12):
    """skeleton -> list of pixel polylines split at junctions and ends"""
    pts = skeleton(mask)
    nb = lambda p: [(p[0] + dy, p[1] + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy or dx) and (p[0] + dy, p[1] + dx) in pts]
    deg = {p: len(nb(p)) for p in pts}
    seen, lines = set(), []
    for start in [p for p in pts if deg[p] != 2] + list(pts):  # ends/junctions first, then loops
        for first in nb(start):
            if (start, first) in seen:
                continue
            path, prev, cur = [start], start, first
            while True:
                seen.add((prev, cur)); seen.add((cur, prev))
                path.append(cur)
                if deg[cur] != 2 or cur == start:
                    break
                nxt = [q for q in nb(cur) if q != prev and (cur, q) not in seen]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
            if len(path) >= min_px:
                lines.append(np.array([(x, y) for y, x in path], float))
    return lines


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    ab = b - a
    d = np.abs(ab[0] * (pts[:, 1] - a[1]) - ab[1] * (pts[:, 0] - a[0])) / (np.linalg.norm(ab) or 1)
    i = int(d.argmax())
    return np.vstack([rdp(pts[: i + 1], eps)[:-1], rdp(pts[i:], eps)]) if d[i] > eps else np.array([a, b])


if __name__ == "__main__":
    im = np.array(Image.open(IMG).convert("RGB"))
    px = np.array([c[1] for c in CONTROL], float)
    dst = to_m([c[2] for c in CONTROL])
    src = np.c_[px[:, 0], -px[:, 1]]
    s, R, t = similarity(src, dst)
    err = np.linalg.norm(s * src @ R.T + t - dst, axis=1)
    for c, e in zip(CONTROL, err):
        print(f"  {c[0]:40} residual {e:5.1f} m")
    T = {"s": float(s), "R": R.tolist(), "t": t.tolist(), "m_per_px": float(s),
         "rotation_deg": float(math.degrees(math.atan2(R[1, 0], R[0, 0]))),
         "control_residual_m": {"mean": float(err.mean()), "max": float(err.max())}}
    json.dump(T, open("data/pdm/transform.json", "w"), indent=1)
    h, w = im.shape[:2]
    corners = to_lonlat(px_to_m(T, np.array([[0, 0], [w, h]], float)))
    print(json.dumps({k: T[k] for k in ("m_per_px", "rotation_deg", "control_residual_m")}), "corners (NW, SE):", corners.round(5).tolist())

    # overlay OSM roads to check the fit away from the fitted line
    ov = Image.open(IMG).convert("RGB")
    dr = ImageDraw.Draw(ov)
    road_pts = []
    nodes, width = {}, {"trunk": 4, "primary": 3, "secondary": 2, "tertiary": 2, "residential": 1, "trunk_link": 2}
    for _, el in ET.iterparse(gzip.open("data/gmr.osm.xml.gz")):
        if el.tag == "node":
            nodes[el.get("id")] = (float(el.get("lon")), float(el.get("lat")))
        elif el.tag == "way":
            tags = {x.get("k"): x.get("v") for x in el.iter("tag")}
            pts = [nodes[n.get("ref")] for n in el.iter("nd") if n.get("ref") in nodes]
            if tags.get("highway") in DRIVABLE and len(pts) > 1:
                road_pts.append(densify(to_m(pts), 5))
            if tags.get("highway") in width and len(pts) > 1:
                dr.line([tuple(p) for p in m_to_px(T, to_m(pts))], fill=(0, 230, 255), width=width[tags["highway"]])
            el.clear()
    dr.line([tuple(p) for p in m_to_px(T, to_m(PROPOSTA))], fill=(255, 255, 0), width=2)
    ov.save("data/pdm/overlay.png")

    # trace the red lines
    roads = np.vstack(road_pts)
    cell = {}
    for p in roads:  # 25 m grid index of road points
        cell.setdefault((int(p[0] // EXISTING_M), int(p[1] // EXISTING_M)), []).append(p)
    near_road = lambda q: any(np.hypot(*(np.array(c) - q).T).min() <= EXISTING_M
                              for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                              for c in [cell.get((int(q[0] // EXISTING_M) + dx, int(q[1] // EXISTING_M) + dy))] if c)
    feats, mask = [], red_mask(im)
    sk, main_px = skeleton(mask), []
    for key, name, wps in MAIN_ROADS:
        for a, b in zip(wps, wps[1:]):
            leg = path_between(sk, a, b)
            leg[0], leg[-1] = a, b  # legs meet exactly at the waypoints, so they snap together
            main_px.append(leg)
            m = px_to_m(T, rdp(leg, 1.5))
            feats.append({"type": "Feature", "properties": {
                "group": key, "corridor": name, "kind": "nova", "main": True,
                "length_m": round(float(np.linalg.norm(np.diff(m, axis=0), axis=1).sum())), "share_on_existing_road": 0},
                "geometry": {"type": "LineString", "coordinates": to_lonlat(m).round(6).tolist()}})
    main_all = np.vstack(main_px)
    for line in trace(mask):
        # fragments running along a main road are replaced by it
        if np.mean(np.sqrt(((line[:, None, :] - main_all[None]) ** 2).sum(-1)).min(1) < 6) > 0.6:
            continue
        simple = rdp(line, 1.5)
        mid = line[len(line) // 2]
        key, name, _ = next(g for g in GROUPS if g[2][0] <= mid[0] <= g[2][2] and g[2][1] <= mid[1] <= g[2][3])
        if key in MAIN_ONLY:
            if not MAIN_ONLY[key]:
                continue  # the Ecovia road is only the main path above
            key, name = "outras", "Outras vias propostas no PDM"  # ramps and branches around the Circular link
        m = px_to_m(T, simple)
        samples = densify(m, 10)
        share = float(np.mean([near_road(q) for q in samples]))
        length = float(np.linalg.norm(np.diff(m, axis=0), axis=1).sum())
        feats.append({"type": "Feature", "properties": {
            "group": key, "corridor": name, "kind": "existente" if share >= EXISTING_SHARE else "nova",
            "length_m": round(length), "share_on_existing_road": round(share, 2)},
            "geometry": {"type": "LineString", "coordinates": to_lonlat(m).round(6).tolist()}})
    json.dump({"type": "FeatureCollection", "source": "Imagem das vias propostas no PDM de Guimarães (2026), georreferenciada com tools/georef.py", "features": feats},
              open("data/pdm/tracado.geojson", "w"), ensure_ascii=False, indent=0)
    for g in dict.fromkeys([g[0] for g in GROUPS] + [m[0] for m in MAIN_ROADS]):
        fs = [f["properties"] for f in feats if f["properties"]["group"] == g]
        print(f"{g:9} nova {sum(f['length_m'] for f in fs if f['kind'] == 'nova'):6} m   existente {sum(f['length_m'] for f in fs if f['kind'] == 'existente'):6} m   ({len(fs)} segmentos)")
