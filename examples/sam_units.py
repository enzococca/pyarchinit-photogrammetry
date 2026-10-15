"""Draw a stratigraphic unit on the orthomosaic with SAM 2.1 and save it in PyArchInit.

Requires: pip install "pyarchinit-photogrammetry[sam]"
Usage: python sam_units.py project.ppg <site> <area> <us> <x> <y>
"""
import sys

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.ai.sam import Sam2Segmenter, segment_orthomosaic
from pyarchinit_photogrammetry.pyarchinit import PyArchInit

project, site, area, us, x, y = sys.argv[1:7]
chunk = ppg.Document.open(project).chunk
sam = Sam2Segmenter("small")  # downloads facebook/sam2.1-hiera-small on first use
unit = segment_orthomosaic(chunk, sam, points=[(float(x), float(y))], us=us, area=area)
print("area m2:", unit.geometry.area)
PyArchInit.connect().publish_units([unit], site, area=area, crs=chunk.crs, nome_doc="Ortofoto")
