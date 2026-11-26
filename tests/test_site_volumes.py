"""Unit volumes thinned for the published web site: closed, with their volume and their thickness."""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from pyarchinit_photogrammetry.archaeo import proxies
from pyarchinit_photogrammetry.engine import meshing
from pyarchinit_photogrammetry.engine.dem import write_geotiff
from pyarchinit_photogrammetry.io import site_volumes


@pytest.fixture
def thin_layer(tmp_path):
    """A layer 3 cm thick on a 2 x 1 m DEM at 1 cm, gently sloping (about 80,000 faces)."""
    x = (np.arange(200) + 0.5) * 0.01
    z = np.tile(10.0 + 0.05 * x, (100, 1)).astype(np.float32)
    dem = write_geotiff(tmp_path / "dem.tif", z, (0.0, 1.0), 0.01, "EPSG:3004", nodata=-32767.0)
    return proxies.unit_proxy(dem, box(0.0, 0.0, 2.0, 1.0), thickness=0.03, us="5").mesh


def _z_range(m):
    return float(m.bounds[1][2] - m.bounds[0][2])


def _assert_sound(new, old, target):
    assert new is not None
    assert len(new.faces) <= 1.1 * target
    assert new.is_watertight
    assert abs(abs(new.volume) - abs(old.volume)) <= 0.1 * abs(old.volume)
    assert abs(_z_range(new) - _z_range(old)) <= 0.005
    assert abs(new.bounds[0][2] - old.bounds[0][2]) <= 0.005 and abs(new.bounds[1][2] - old.bounds[1][2]) <= 0.005


def test_prism_refactor_keeps_the_volume(thin_layer):
    assert thin_layer.is_watertight
    assert abs(thin_layer.volume) == pytest.approx(0.06, abs=0.002)
    assert len(thin_layer.faces) > 70_000


def test_a_thin_layer_stays_closed(thin_layer):
    new, how = site_volumes.thin_volume(thin_layer, 2000)
    assert how in ("quadric", "regrid")
    _assert_sound(new, thin_layer, 2000)


def test_the_rebuild_when_quadric_is_not_there(thin_layer, monkeypatch):
    monkeypatch.setattr(meshing, "_decimate", lambda m, n: m)
    new, how = site_volumes.thin_volume(thin_layer, 2000)
    assert how == "regrid"
    _assert_sound(new, thin_layer, 2000)


def test_small_volume_kept(thin_layer):
    new, how = site_volumes.thin_volume(thin_layer, len(thin_layer.faces))
    assert new is thin_layer and how == "kept"


def test_targets():
    assert site_volumes.VOLUME_FACES == 20_000 and site_volumes.CHUNK_VOLUME_FACES == 600_000


def test_a_volume_that_cannot_be_rebuilt_is_dropped(thin_layer, monkeypatch):
    monkeypatch.setattr(meshing, "_decimate", lambda m, n: m)
    monkeypatch.setattr(site_volumes, "_regrid", lambda m, n: None)
    assert site_volumes.thin_volume(thin_layer, 2000) == (None, "dropped")
