"""Pottery profile from a turntable scan (mat printed with `ppg markers turntable`)."""
import sys

import trimesh

from pyarchinit_photogrammetry.archaeo import pottery

mesh = trimesh.load(sys.argv[1], force="mesh")
# the turntable mat defines the frame: rotation axis = Z through the origin
profile = pottery.best_profile(mesh, pottery.Axis((0, 0, 0), (0, 0, 1)))
print(profile.measurements())
pottery.draw(profile, "profilo.svg", scale=1, title=sys.argv[2] if len(sys.argv) > 2 else None)
pottery.draw(profile, "profilo.dxf")
