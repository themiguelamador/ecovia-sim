"""Self-check for the geometry and calibration maths. Run: uv run python tests/test_geometry.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import sumo  # noqa: E402

os.environ.setdefault("SUMO_HOME", sumo.SUMO_HOME)
from geh import edge_bearing, geh  # noqa: E402
from scenario import line_dist, seg_dist  # noqa: E402

assert seg_dist((5, 3), (0, 0), (10, 0)) == 3                 # perpendicular foot inside segment
assert seg_dist((-4, 3), (0, 0), (10, 0)) == 5                # clamps to the segment end
assert line_dist((10, 5), [(0, 0), (10, 0), (10, 10)]) == 0   # on the second segment
assert geh(100, 100) == 0 and round(geh(150, 100), 2) == 4.47 and geh(0, 0) == 0
assert round(edge_bearing([(0, 0), (0, 10)], 0, 5)) == 0      # northbound
assert round(edge_bearing([(0, 0), (10, 0)], 5, 0)) == 90     # eastbound
print("ok")
