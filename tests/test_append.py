"""Appending projects to a document as new chunks (core.append, ``ppg append``)."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest


def _photo(path: Path, value: int) -> Path:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    img = np.full((48, 64, 3), value, np.uint8)
    img[10:20, 10:30] = 255 - value
    cv2.imwrite(str(path), img)
    return path


def _raster(path: Path, crs: str = "EPSG:32633", value: float = 100.0, bands: int = 1) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.full((bands, 8, 8), value, np.float32)
    with rasterio.open(path, "w", driver="GTiff", width=8, height=8, count=bands, dtype="float32", crs=crs,
                       transform=from_origin(500000.0, 4500008.0, 1.0, 1.0)) as dst:
        dst.write(data)
    return path


def make_source(root: Path) -> Path:
    """A project with two chunks: photos beside it (relative), a DEM and a dense cloud in the chunk folder, an
    orthomosaic of another program outside it, drawings, markers with references, a path into its own folder in the
    settings, a link in its folder; the copies of Modifica > Annulla left in ``undo``."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.pointcloud import PointCloud

    doc = ppg.Document()
    a = doc.add_chunk("Chunk 1")
    b = doc.add_chunk("Saggio B")
    doc.save(root / "scavo.ppg")
    a.add_photos([_photo(root / "foto" / f"IMG_{i}.jpg", 40 + 30 * i) for i in range(3)])
    b.add_photos([_photo(root / "foto_b" / "IMG_9.jpg", 90)])
    a.crs = "EPSG:32633"
    a.transform = np.diag([2.0, 2.0, 2.0, 1.0])
    _raster(a.data_dir / "dem.tif")
    a._set_product("dem", a.data_dir / "dem.tif")
    a.point_cloud = PointCloud(np.random.default_rng(0).uniform(0, 1, (50, 3)))
    a._set_product("orthomosaic", _raster(root / "export" / "orto.tif", bands=3))
    a.meta["drawings"] = {"units": [{"label": "US 1", "geometry": [[0, 0], [1, 0], [1, 1]]}]}
    a.meta["own_file"] = str(a.data_dir / "dem.tif")
    m = a.add_marker("T1", reference=[500001.0, 4500001.0, 100.0])
    a.add_marker_projection(m, a.cameras[0], 10.0, 12.0)
    (a.data_dir / "undo").mkdir()
    (a.data_dir / "undo" / "copia.ply").write_text("x")
    (a.data_dir / "links").mkdir()
    (a.data_dir / "links" / "dem_link.tif").symlink_to(a.data_dir / "dem.tif")
    b.meta["note"] = "seconda"
    doc.save()
    return root / "scavo.ppg"


def _stamps(folder: Path) -> dict:
    out = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p)] = (hashlib.sha1(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


def test_a_ppg_with_two_chunks_comes_with_its_data_and_the_source_is_untouched(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects

    src = make_source(tmp_path / "src")
    before = _stamps(tmp_path / "src")
    doc = ppg.Document()
    mine = doc.add_chunk("Chunk 1")
    doc.save(tmp_path / "dst" / "mio.ppg")
    results = append_projects(doc, [src])
    assert _stamps(tmp_path / "src") == before  # never written
    (res,) = results
    assert res.error is None and res.kind == "ppg"
    a, b = res.chunks
    assert [c.label for c in doc.chunks] == ["Chunk 1", "scavo - Chunk 1", "Saggio B"]  # the name taken: prefixed
    assert doc.chunk is mine  # the active chunk unchanged
    assert len({c.key for c in doc.chunks}) == 3
    # photos: the same files; the data folder copied, the copies of the undo left out
    assert [Path(c.path) for c in a.cameras] == [(tmp_path / "src" / "foto" / f"IMG_{i}.jpg").resolve()
                                                 for i in range(3)]
    assert a.data_dir == doc.files_dir / f"chunk_{a.key}"
    assert a.product_path("dem").exists() and a.product_path("dem").parent == a.data_dir
    assert len(a.point_cloud) == 50
    assert a.product_path("orthomosaic").resolve() == (tmp_path / "src" / "export" / "orto.tif").resolve()
    assert not (a.data_dir / "undo").exists()
    link = a.data_dir / "links" / "dem_link.tif"
    assert link.is_symlink() and link.resolve() == (a.data_dir / "dem.tif").resolve()  # into the copy
    assert a.meta["own_file"] == str(a.data_dir / "dem.tif")
    assert a.meta["drawings"]["units"][0]["label"] == "US 1"
    assert a.crs == "EPSG:32633" and np.allclose(a.transform, np.diag([2.0, 2.0, 2.0, 1.0]))
    assert a.marker("T1").reference.location == [500001.0, 4500001.0, 100.0]
    assert 0 in a.marker("T1").projections or a.cameras[0].key in a.marker("T1").projections
    assert b.meta["note"] == "seconda" and len(b.cameras) == 1
    # saved and read again: the photos relative to the project when they are under its folder, else absolute
    doc.save()
    again = ppg.Document.open(tmp_path / "dst" / "mio.ppg")
    assert [Path(c.path) for c in again.chunks[1].cameras] == [Path(c.path) for c in a.cameras]


def test_photos_under_the_folder_of_the_project_are_stored_relative(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects

    src = make_source(tmp_path / "lavoro" / "src")
    doc = ppg.Document()
    doc.add_chunk("Altro")
    doc.save(tmp_path / "lavoro" / "mio.ppg")
    append_projects(doc, [src], chunks=["Saggio B"])
    assert [c.label for c in doc.chunks] == ["Altro", "Saggio B"]
    doc.save()
    data = json.loads((tmp_path / "lavoro" / "mio.ppg").read_text())
    assert data["chunks"][1]["cameras"][0]["path"] == "./src/foto_b/IMG_9.jpg"


def _broken_psx(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a Metashape project")
    return path


def _odm_zip(path: Path) -> Path:
    """A WebODM ``all.zip`` with an orthophoto, a DSM and coords.txt."""
    task = path.parent / "task_src"
    _raster(task / "odm_orthophoto" / "odm_orthophoto.tif", bands=3)
    _raster(task / "odm_dem" / "dsm.tif")
    (task / "odm_georeferencing").mkdir(parents=True)
    (task / "odm_georeferencing" / "coords.txt").write_text("WGS84 UTM 33N\n500000 4500000\n")
    with zipfile.ZipFile(path, "w") as z:
        for p in task.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(task))
    return path


def test_two_sources_one_broken_and_a_webodm_zip(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects

    bad = _broken_psx(tmp_path / "rotto.psx")
    odm = _odm_zip(tmp_path / "odm" / "all.zip")
    zip_bytes = odm.read_bytes()
    doc = ppg.Document()
    doc.add_chunk("Chunk 1")
    doc.save(tmp_path / "dst" / "mio.ppg")
    keys_before = {c.key for c in doc.chunks}
    res_bad, res_odm = append_projects(doc, [bad, odm])
    assert res_bad.error and not res_bad.chunks
    assert res_odm.error is None and res_odm.kind == "webodm"
    (ch,) = res_odm.chunks
    assert ch.label == "odm" and ch.crs == "EPSG:32633"
    assert {"dem", "orthomosaic"} <= set(ch.products)
    assert ch.product_path("dem").exists()
    # the archive extracted inside the new chunk, never beside the source
    assert str(ch.product_path("dem").resolve()).startswith(str(ch.data_dir.resolve()))
    assert odm.read_bytes() == zip_bytes and sorted(p.name for p in odm.parent.iterdir()) == ["all.zip", "task_src"]
    assert len(doc.chunks) == 2 and keys_before < {c.key for c in doc.chunks}
    # nothing left of the broken one
    assert sorted(p.name for p in doc.files_dir.iterdir() if p.name.startswith("chunk_")) == \
        sorted(f"chunk_{c.key}" for c in doc.chunks if (doc.files_dir / f"chunk_{c.key}").exists())


def test_a_metashape_project_appended(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects, source_chunks
    from test_integrations import make_psx

    psx, _origin, _axes = make_psx(tmp_path / "ms")
    doc = ppg.Document()
    first = doc.add_chunk("Chunk 1")
    doc.save(tmp_path / "dst" / "mio.ppg")
    assert source_chunks(psx) == ["Chunk 1"]
    (res,) = append_projects(doc, [psx])
    assert res.error is None, res.error
    (ch,) = res.chunks
    assert ch.label == "scavo - Chunk 1" and len(ch.aligned_cameras) == 4 and ch.crs == "EPSG:32637"
    assert doc.chunk is first
    assert "model" in ch.products and ch.product_path("model").exists()


def test_chunks_chosen_by_name_and_a_wrong_name(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects, source_chunks

    src = make_source(tmp_path / "src")
    assert source_chunks(src) == ["Chunk 1", "Saggio B"]
    doc = ppg.Document()
    doc.add_chunk("X")
    doc.save(tmp_path / "dst" / "mio.ppg")
    (res,) = append_projects(doc, [src], chunks={str(src): ["Nessuno"]})
    assert res.error and "Nessuno" in res.error and len(doc.chunks) == 1
    (itself,) = append_projects(doc, [doc.path])  # the open project into itself: refused
    assert itself.error and len(doc.chunks) == 1


def test_a_cancellation_takes_everything_away(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    src = make_source(tmp_path / "src")
    other = make_source(tmp_path / "src2")
    doc = ppg.Document()
    doc.add_chunk("X")
    doc.save(tmp_path / "dst" / "mio.ppg")
    seen = []

    def cb(fraction, message):
        seen.append(message)
        if fraction > 0.6:
            p.cancel()

    p = Progress(cb)
    with pytest.raises(Cancelled):
        append_projects(doc, [src, other], progress=p)
    assert [c.label for c in doc.chunks] == ["X"]
    assert not [d for d in doc.files_dir.iterdir() if d.name.startswith("chunk_") and d.name != "chunk_0"
                and any(d.iterdir())]


def test_cli_append(tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    src = make_source(tmp_path / "src")
    doc = ppg.Document()
    doc.add_chunk("X")
    doc.save(tmp_path / "dst" / "mio.ppg")
    r = CliRunner().invoke(app, ["append", str(tmp_path / "dst" / "mio.ppg"), str(src),
                                 str(tmp_path / "manca.psx"), "--chunks", "Saggio B"])
    assert r.exit_code == 1, r.output  # one source failed
    assert "Saggio B" in r.output and "manca.psx" in r.output
    again = ppg.Document.open(tmp_path / "dst" / "mio.ppg")
    assert [c.label for c in again.chunks] == ["X", "Saggio B"]


@pytest.mark.parametrize("story", ["not saved", "saved", "saved, undone, saved", "saved, undone", "undone, redone"])
def test_the_folders_of_chunks_appended_follow_the_project_as_saved(tmp_path, story):
    """The end of the session (``settle_removals``) keeps the folders of the chunks appended only when the project as
    last saved lists them."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects
    from pyarchinit_photogrammetry.core.removal import ChunkRemoval

    src = make_source(tmp_path / "src")
    doc = ppg.Document()
    doc.add_chunk("X")
    doc.save(tmp_path / "dst" / "mio.ppg")
    (res,) = append_projects(doc, [src])
    chunks = [c for c in res.chunks if (doc.files_dir / f"chunk_{c.key}").is_dir()]  # Saggio B has no data
    folders = [doc.files_dir / f"chunk_{c.key}" for c in chunks]
    assert len(folders) == 1
    removal = ChunkRemoval.added(doc, res.chunks)
    for step in story.split(", "):
        if step.startswith("saved"):
            doc.save()
        elif step == "undone":
            removal.redo()  # Modifica > Annulla of the append
            assert [c.label for c in doc.chunks] == ["X"] and not any(f.exists() for f in folders)
        elif step == "redone":
            removal.undo()
            assert len(doc.chunks) == 3 and all(f.is_dir() for f in folders)
    listed = {c["key"] for c in __import__("json").loads(doc.path.read_text())["chunks"]}
    doc.settle_removals()
    for c, f in zip(chunks, folders):
        assert f.is_dir() == (c.key in listed), (story, f)
    assert not (doc.files_dir / ".removed").exists()
