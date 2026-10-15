"""Sections, contours and excavated volume from a processed project.

Usage: python sections_and_volumes.py project.ppg X1 Y1 X2 Y2   (trace of the section, CRS coordinates)
"""
import sys

from shapely.geometry import box

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.archaeo import sections, terrain

doc = ppg.Document.open(sys.argv[1])
chunk = doc.chunk
x0, y0, x1, y1 = (float(v) for v in sys.argv[2:6])

s = sections.section(chunk, (x0, y0), (x1, y1), label="Sezione A-A'")
s.to_dxf("sezione_AA.dxf")
s.to_svg("sezione_AA.svg", scale=20)

lines = terrain.contours(chunk.dem_path, interval=0.05)
terrain.export_contours(lines, "curve_5cm.dxf")

print(terrain.volume(chunk.dem_path, polygon=box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))))
