"""Zoning of an index: natural breaks (Jenks) exact on their sample, between the populations of an
index where the quantiles cut through them, the same every time and on large rasters; the
GeoPackage and the class raster as with the other methods."""
from __future__ import annotations

import itertools

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import multispectral as ms


def _three_populations(h: int = 60, w: int = 100, seed: int = 0):
    """An NDVI of a field: bare soil on 60 % of it (0.15), grass on 30 % (0.47), a crop mark on
    10 % (0.81), in three bands of columns, and a few cells without a value. Returns the index and
    the population of every cell (0 where no value)."""
    rng = np.random.default_rng(seed)
    truth = np.ones((h, w), np.int32)
    truth[:, int(0.6 * w):] = 2
    truth[:, int(0.9 * w):] = 3
    means = np.array([0.0, 0.15, 0.47, 0.81])
    values = (means[truth] + rng.normal(0, 0.02, (h, w))).astype(np.float32)
    values[5:8, 10:13] = np.nan
    truth[5:8, 10:13] = 0
    return values, truth


def _between_the_populations(values, truth, breaks) -> bool:
    return len(breaks) == 2 and all(np.nanmax(values[truth == k]) < b < np.nanmin(values[truth == k + 1])
                                    for k, b in enumerate(breaks, start=1))


def _index_project(tmp_path):
    """A project whose chunk has a reflectance orthomosaic (red and NIR giving the NDVI of
    :func:`_three_populations`) and its NDVI."""
    import rasterio
    from rasterio.transform import from_origin

    values, truth = _three_populations()
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    ch.crs = "EPSG:3004"
    folder = ch.data_dir / "multispectral"
    folder.mkdir(parents=True)
    bands = ["Blue", "Green", "Red", "RedEdge", "NIR"]
    red = np.full(values.shape, 0.08, np.float32)
    data = np.stack([red, red, red, red, red * (1 + values) / (1 - values)]).astype(np.float32)
    with rasterio.open(folder / "reflectance.tif", "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
                       count=5, dtype="float32", crs="EPSG:3004", transform=from_origin(2480000.0, 4500000.0, 0.05, 0.05),
                       nodata=float("nan")) as dst:
        dst.write(data)
        for i, b in enumerate(bands):
            dst.set_band_description(i + 1, b)
    ch.meta["multispectral"] = {"reference": "Red", "bands": bands, "captures": 12, "panel": None, "registration": {},
                                "reflectance": "multispectral/reflectance.tif", "reflectance_bands": bands}
    ms.index_raster(ch, "NDVI")
    doc.save()
    return doc, ch, values, truth


def test_natural_breaks_are_the_best_classes_of_their_values():
    """Against every way of cutting a few sorted values into classes: the least sum of squared
    deviations from the means of the classes (ties and rounded values included)."""
    rng = np.random.default_rng(4)

    def ssd(v, cuts):
        return sum(float(((p - p.mean()) ** 2).sum()) for p in np.split(np.sort(v.astype(np.float64)), cuts))

    for _ in range(120):
        v = np.round(rng.normal(0, 1, int(rng.integers(4, 13))) * rng.choice([1, 5]), int(rng.integers(0, 3)))
        v = v.astype(np.float32)
        k = int(rng.integers(2, 5))
        breaks = ms.natural_breaks(v, k)
        if np.unique(v).size <= k:  # every distinct value a class
            assert len(breaks) == np.unique(v).size - 1
            continue
        cls = np.searchsorted(np.asarray(breaks, np.float32), np.sort(v), side="right")
        assert len(breaks) == k - 1 and np.array_equal(np.unique(cls), np.arange(k))
        best = min(ssd(v, cuts) for cuts in itertools.combinations(range(1, v.size), k - 1))
        assert ssd(v, np.flatnonzero(np.diff(cls)) + 1) == pytest.approx(best, rel=1e-9, abs=1e-12)


def test_natural_breaks_fall_between_the_populations_where_quantiles_do_not():
    values, truth = _three_populations()
    cls, breaks = ms.zoning(values, classes=3, method="jenks")
    assert _between_the_populations(values, truth, breaks)
    assert np.array_equal(cls, truth)  # every cell in its population, 0 where no value
    _, quantiles = ms.zoning(values, classes=3, method="quantiles")
    for b in quantiles:  # 60 % of soil: both quantiles cut through a population
        assert any(np.nanmin(values[truth == k]) < b < np.nanmax(values[truth == k]) for k in (1, 2, 3))
    assert ms.zoning(values, classes=3, method="jenks")[1] == breaks  # the same every time
    # fewer distinct values than classes: every value a class; two neighbouring floats kept apart
    assert ms.natural_breaks(np.array([0.2, 0.2, 0.6, np.nan, 0.6], np.float32), 4) == pytest.approx([0.4])
    assert ms.natural_breaks(np.full(10, 0.3, np.float32), 3) == [] and ms.natural_breaks(np.array([np.nan]), 3) == []
    a = np.float32(0.25)
    b = np.nextafter(a, np.float32(1))
    cls, _ = ms.zoning(np.array([[a, a, b, b]], np.float32), classes=2, method="jenks", min_cells=1)
    assert cls.tolist() == [[1, 1, 2, 2]]
    with pytest.raises(ValueError, match="jenks"):
        ms.zoning(values, method="somehow")


def test_natural_breaks_of_a_large_index_from_a_fixed_sample():
    """250 000 cells: the breaks from a sample of 200 000 drawn with a fixed seed, sorted, and 3000
    values at evenly spaced ranks of it - the same ones every time, the generator of NumPy left
    alone, the populations apart."""
    values, truth = _three_populations(500, 500, seed=1)
    state = np.random.get_state()[1].copy()
    cls, breaks = ms.zoning(values, classes=3, method="jenks")
    assert ms.natural_breaks(values, 3) == breaks and np.array_equal(np.random.get_state()[1], state)
    assert _between_the_populations(values, truth, breaks) and np.array_equal(cls, truth)


def test_export_zoning_with_natural_breaks(tmp_path):
    """The GeoPackage of the zones and the class raster, as with the other methods."""
    from pyarchinit_photogrammetry.io import gpkg

    doc, ch, values, truth = _index_project(tmp_path)
    summary = ms.export_zoning(ch, "NDVI", tmp_path / "zone.gpkg", classes=3, method="jenks")
    assert summary["method"] == "jenks" and summary["polygons"] == 3
    assert _between_the_populations(values, truth, summary["breaks"])
    rows = gpkg.read_table(tmp_path / "zone.gpkg", "zone")
    assert sorted(r[1]["classe"] for r in rows) == [1, 2, 3]
    assert summary["area_m2"] == pytest.approx({1: 3591 * 0.0025, 2: 1800 * 0.0025, 3: 600 * 0.0025}, abs=0.01)
    import rasterio

    with rasterio.open(ch.data_dir / summary["raster"]) as src:
        assert np.array_equal(src.read(1), truth)
    assert ch.meta["multispectral"]["zoning"]["NDVI"]["method"] == "jenks"
