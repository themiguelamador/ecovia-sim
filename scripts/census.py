"""INE BGRI 2021 (Guimarães, DTMN 0308) -> data/census.csv, one row per census subsection.

Columns: lon, lat, residents, age_0_24, age_25_64, dwellings.
The point is the centre of the subsection's bounding box.
# ponytail: bbox centre, not true centroid; subsections are small (~1 block), good enough for zoning.
"""
import csv
import sqlite3
import struct
import sys

from pyproj import Transformer

gpkg, out = sys.argv[1], sys.argv[2]
to_wgs84 = Transformer.from_crs(3763, 4326, always_xy=True)


def bbox_centre(blob: bytes) -> tuple[float, float]:
    flags = blob[3]
    assert blob[:2] == b"GP" and (flags >> 1) & 7 >= 1, "geometry has no envelope"
    minx, maxx, miny, maxy = struct.unpack("<dddd" if flags & 1 else ">dddd", blob[8:40])
    return (minx + maxx) / 2, (miny + maxy) / 2


rows = sqlite3.connect(gpkg).execute(
    "select geom, N_INDIVIDUOS, N_INDIVIDUOS_0_14 + N_INDIVIDUOS_15_24, N_INDIVIDUOS_25_64,"
    " N_ALOJAMENTOS_FAM_CLASS_RHABITUAL from BGRI2021_0308"
)
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["lon", "lat", "residents", "age_0_24", "age_25_64", "dwellings"])
    for geom, *counts in rows:
        lon, lat = to_wgs84.transform(*bbox_centre(geom))
        w.writerow([round(lon, 6), round(lat, 6), *(int(c or 0) for c in counts)])
