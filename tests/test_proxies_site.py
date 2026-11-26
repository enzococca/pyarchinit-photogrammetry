"""The program palette of the proxies and the site a new proxy entry records."""
from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from pyarchinit_photogrammetry.archaeo.proxies import PROXY_COLOURS, entry_site, proxies_for_units
from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon


def test_palette_shared_with_the_window():
    from pyarchinit_photogrammetry.gui import scene

    assert len(PROXY_COLOURS) == 12
    decoded = [[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in PROXY_COLOURS]
    assert scene.PROXY_COLORS.dtype == np.uint8
    assert scene.PROXY_COLORS.tolist() == decoded


def test_entry_site_order():
    assert entry_site("A", "B", "C") == "A"
    assert entry_site(None, "B", "C") == "B"
    assert entry_site("", "", "C") == "C"
    assert entry_site(None, None, None) == ""


def test_proxy_keeps_its_unit_site(tmp_path):
    path = tmp_path / "dem.tif"
    z = np.full((20, 20), 10.0, np.float32)
    with rasterio.open(path, "w", driver="GTiff", height=20, width=20, count=1, dtype="float32",
                       crs="EPSG:32633", transform=from_origin(0.0, 2.0, 0.1, 0.1)) as ds:
        ds.write(z, 1)
    unit = UnitPolygon(box(0.5, 0.5, 1.5, 1.5), us="7", area="1", tipo="US", sito="A")
    (px,) = proxies_for_units(path, [unit], base=9.0)
    assert px.sito == "A"
    (bare,) = proxies_for_units(path, [UnitPolygon(box(0.5, 0.5, 1.5, 1.5), us="8")], base=9.0)
    assert bare.sito is None
