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
    results = append_projects(doc, [src])
    (res,) = results
    chunks = [c for c in res.chunks if (doc.files_dir / f"chunk_{c.key}").is_dir()]  # Saggio B has no data
    folders = [doc.files_dir / f"chunk_{c.key}" for c in chunks]
    assert len(folders) == 1
    removal = results.journal
    assert isinstance(removal, ChunkRemoval) and removal.chunks == res.chunks
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


# ------------------------------------------------------------------ fix round 1 (review of T2)
def make_linked_source(root: Path) -> Path:
    """A project whose chunks point at one another: B aligned to A (*Allinea chunk*) and its DEM coregistered on A's
    (by key and by the file of a kept version of A), a proxy of B on A's DEM, a zoning GeoPackage of A beside the
    project, sections beside it, a site."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    a = doc.add_chunk("A")
    b = doc.add_chunk("B")
    doc.meta["sito"] = "Tell Sorgente"
    doc.save(root / "sorgente.ppg")
    dem_a = _raster(a.data_dir / "versions" / "dem" / "v1" / "dem.tif")
    _raster(a.data_dir / "dem.tif")
    a._set_product("dem", a.data_dir / "dem.tif")
    gpkg = root / "zone_ndvi.gpkg"
    gpkg.write_bytes(b"gpkg of the source")
    a.meta["multispectral"] = {"zoning": {"NDVI": {"file": str(gpkg), "raster": "multispectral/NDVI_zone.tif"}}}
    b.meta["chunk_alignment"] = {"reference": "A", "reference_key": a.key, "method": "markers"}
    b.meta["dem"] = {"coregistration": {"reference_chunk": a.key, "reference_version": "v1",
                                        "reference_file": str(dem_a)}}
    b.meta["proxies"] = [{"id": "p1", "base_dem": str(a.data_dir / "dem.tif")}]
    (root / "sezioni").mkdir()
    (root / "sezioni" / "sezione_1.svg").write_text("<svg/>")
    (root / "sezioni" / "sezioni.json").write_text(json.dumps([
        {"label": "Sezione 1", "p1": [0, 0], "p2": [1, 1], "length": 1.41, "svg": str(root / "sezioni" / "sezione_1.svg"),
         "kind": "vertical", "ppg_id": "abc"}]))
    doc.save()
    return root / "sorgente.ppg"


def _target(tmp_path, *labels):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    for label in labels or ("X",):
        doc.add_chunk(label)
    doc.meta["sito"] = "Tell Mio"
    doc.save(tmp_path / "dst" / "mio.ppg")
    return doc


def test_keys_of_chunks_appended_together_follow_and_the_others_are_dropped(tmp_path):
    from pyarchinit_photogrammetry.core.append import append_projects
    from pyarchinit_photogrammetry.engine import orient

    src = make_linked_source(tmp_path / "src")
    doc = _target(tmp_path, "X", "Y")  # keys 0 and 1: the keys of A and B in the source
    (res,) = append_projects(doc, [src])
    a, b = res.chunks
    assert (a.key, b.key) == (2, 3)
    assert b.meta["chunk_alignment"]["reference_key"] == a.key
    assert b.meta["dem"]["coregistration"]["reference_chunk"] == a.key
    assert [c.label for c in orient.aligned_to(a)] == ["B"] and orient.aligned_to(doc.chunks[0]) == []
    # the files of A that B uses: the copies
    assert b.meta["dem"]["coregistration"]["reference_file"] == str(a.data_dir / "versions" / "dem" / "v1" / "dem.tif")
    assert b.meta["proxies"][0]["base_dem"] == str(a.data_dir / "dem.tif")
    # B alone: no key that names X or Y, the link dropped and said
    doc2 = _target(tmp_path / "two", "X", "Y")
    (res2,) = append_projects(doc2, [src], chunks=["B"])
    (b2,) = res2.chunks
    assert "chunk_alignment" not in b2.meta
    assert b2.meta["dem"]["coregistration"]["reference_chunk"] is None
    assert orient.aligned_to(doc2.chunks[0]) == [] and orient.aligned_to(doc2.chunks[1]) == []
    text = " ".join(res2.warnings)
    assert "allineato a «A»" in text and "coregistrazione" in text
    assert "file di chunk del progetto di origine" in text  # A's DEM: left in the source, said


def test_zoning_sections_and_site_of_the_source(tmp_path):
    from pyarchinit_photogrammetry.core.append import append_projects, write_sections

    src = make_linked_source(tmp_path / "src")
    gpkg = tmp_path / "src" / "zone_ndvi.gpkg"
    doc = _target(tmp_path)
    results = append_projects(doc, [src])
    a = results[0].chunks[0]
    copy_ = Path(a.meta["multispectral"]["zoning"]["NDVI"]["file"])
    assert copy_ != gpkg and copy_.read_bytes() == gpkg.read_bytes() and str(copy_).startswith(str(a.data_dir))
    assert any("Tell Sorgente" in w and "Tell Mio" in w for w in results[0].warnings)
    assert doc.meta["sito"] == "Tell Mio"
    ((prefix, sections),) = results.sections
    assert prefix == "sorgente" and Path(sections[0]["svg"]).parent == tmp_path / "dst" / "sezioni"
    assert Path(sections[0]["svg"]).read_text() == "<svg/>"
    added = write_sections(doc, sections, prefix)
    assert [s["label"] for s in added] == ["Sezione 1"]
    again = write_sections(doc, sections, prefix)  # the same once more: a name and an identity of their own
    assert [s["label"] for s in again] == ["sorgente - Sezione 1"] and "ppg_id" not in again[0]
    on_disk = json.loads((tmp_path / "dst" / "sezioni" / "sezioni.json").read_text())
    assert len(on_disk) == 2
    assert json.loads((tmp_path / "src" / "sezioni" / "sezioni.json").read_text())[0]["label"] == "Sezione 1"


def test_a_failing_source_takes_away_only_its_own_chunks(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.core.append import append_projects
    from pyarchinit_photogrammetry.core.progress import Progress
    from pyarchinit_photogrammetry.integrations import metashape as ms
    from test_integrations import make_psx

    src = make_source(tmp_path / "src")
    psx, _o, _a = make_psx(tmp_path / "ms")
    doc = _target(tmp_path)
    mine = []

    def user_adds(fraction, message):  # the user adds a chunk while the append runs
        if not mine and fraction > 0.1:
            mine.append(doc.add_chunk("Mio"))
            (mine[0].data_dir / "dati.txt").write_text("miei")

    def broken(*a, **k):
        raise RuntimeError("modello rotto")

    monkeypatch.setattr(ms, "_import_model", broken)  # the psx fails after making its chunk
    res_ppg, res_psx = append_projects(doc, [src, psx], progress=Progress(user_adds))
    assert res_psx.error == "modello rotto" and not res_psx.chunks
    assert [c.label for c in doc.chunks] == ["X", "Chunk 1", "Saggio B", "Mio"]
    assert (mine[0].data_dir / "dati.txt").read_text() == "miei"
    # the importer alone cleans its own chunk too
    doc2 = _target(tmp_path / "two")
    with pytest.raises(RuntimeError):
        ms.import_psx_into(doc2, psx)
    assert [c.label for c in doc2.chunks] == ["X"]
    assert not [d for d in doc2.files_dir.iterdir() if d.name.startswith("chunk_") and d.name != "chunk_0"]


def test_keys_are_given_one_at_a_time(tmp_path):
    import threading

    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    doc.save(tmp_path / "p.ppg")
    start = threading.Barrier(8)

    def add():
        start.wait()
        for _ in range(25):
            doc.add_chunk()

    threads = [threading.Thread(target=add) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys = [c.key for c in doc.chunks]
    assert len(keys) == 200 and len(set(keys)) == 200


@pytest.mark.parametrize("saved", [False, True])
def test_a_crash_after_an_append_is_settled_at_the_next_opening(tmp_path, saved):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects

    src = make_source(tmp_path / "src")
    doc = _target(tmp_path)
    journal_seen = []

    def watch(fraction, message):  # the journal lists a chunk before its folder is filled
        trash = doc.files_dir / ".removed"
        if trash.is_dir() and not journal_seen:
            manifests = [json.loads(m.read_text()) for m in trash.glob("*/removed.json")]
            journal_seen.extend(m["files"]["present"] for m in manifests if m["kind"] == "added")

    from pyarchinit_photogrammetry.core.progress import Progress

    results = append_projects(doc, [src], progress=Progress(watch))
    assert journal_seen and journal_seen[0]
    folder = doc.files_dir / f"chunk_{results[0].chunks[0].key}"
    assert folder.is_dir() and results.journal is not None
    if saved:
        doc.save()
    # the program dies here: nothing settled; the next opening reads the project as saved
    again = ppg.Document.open(doc.path)
    again.recover_removed()
    assert folder.is_dir() == saved
    assert not (again.files_dir / ".removed").exists()


def test_link_targets_of_windows_are_read_as_plain_paths():
    """os.readlink on Windows gives absolute targets as extended paths: the prefix goes, so the target is seen under
    the folder of the source project and moved to the copy."""
    from pyarchinit_photogrammetry.core.append import _plain_link

    assert _plain_link("\\\\?\\C:\\a\\b.tif") == "C:\\a\\b.tif"
    assert _plain_link("\\??\\C:\\a\\b.tif") == "C:\\a\\b.tif"
    assert _plain_link("\\\\?\\UNC\\server\\share\\b.tif") == "\\\\server\\share\\b.tif"
    assert _plain_link("/Users/a/b.tif") == "/Users/a/b.tif"
    assert _plain_link("../dem.tif") == "../dem.tif"


def make_tied_source(root: Path) -> Path:
    """Three chunks with ties: A-B (two), B-C (one) and a known point in A."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine import ties

    doc = ppg.Document()
    a, b, c = doc.add_chunk("A"), doc.add_chunk("B"), doc.add_chunk("C")
    a.meta["alignment_id"] = "allineamento A"
    ties.add_tie(doc, a, [1, 2, 3], b, [4, 5, 6])
    ties.add_tie(doc, a, [0, 0, 0], b, [1, 1, 1], name="picchetto")
    ties.add_tie(doc, b, [2, 2, 2], c, [3, 3, 3])
    ties.add_known(doc, a, [5, 5, 5], [100.0, 200.0, 10.0], "GNSS 1", 0.02)
    doc.save(root / "legami.ppg")
    return root / "legami.ppg"


def test_ties_between_chunks_appended_come_with_them(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects
    from pyarchinit_photogrammetry.engine import ties

    src = make_tied_source(tmp_path / "src")
    source = (tmp_path / "src" / "legami.ppg").read_bytes()
    doc = _target(tmp_path, "X")
    mine = ties.add_tie(doc, doc.chunks[0], [0, 0, 0], doc.add_chunk("Y"), [1, 1, 1], name="picchetto")
    (res,) = append_projects(doc, [src], chunks=["A", "B"])
    a, b = res.chunks
    assert res.ties == 3 and res.ties_dropped == ["legame 2"]  # B-C: C was not appended
    got = {t.name: t for t in ties.ties_of(doc)}
    assert set(got) == {"picchetto", "legame 1", "picchetto (legami)", "GNSS 1"}
    assert got["picchetto"].id == mine.id  # the tie already there is untouched
    for t in (got["legame 1"], got["picchetto (legami)"], got["GNSS 1"]):
        assert not t.broken and not t.stale and t.active and t.chunk_keys() <= {a.key, b.key}
    known = got["GNSS 1"]
    assert known.b is None and known.known == [100.0, 200.0, 10.0] and known.precision == pytest.approx(0.02)
    np.testing.assert_allclose(ties.end_world(doc, *got["legame 1"].b, uid=got["legame 1"].b_uid),
                               b.to_world(np.array([[4.0, 5, 6]]))[0])
    assert (tmp_path / "src" / "legami.ppg").read_bytes() == source  # the source is only read
    # the same project appended again: other identities, every tie resolved to its own chunk
    (res2,) = append_projects(doc, [src], chunks=["A", "B"])
    assert res2.ties == 3
    uids = [m.uid for ch in doc.chunks for m in ch.markers if m.uid]
    assert len(uids) == len(set(uids))
    every = ties.ties_of(doc)
    assert len(every) == 7 and len({t.id for t in every}) == 7 and not any(t.broken for t in every)
    assert len({t.name for t in every}) == 7  # "picchetto (legami 2)"
    doc.save()
    assert len(ties.ties_of(ppg.Document.open(doc.path))) == 7
    # A alone: the ties A-B are said, the tie B-C (no end on A) is not
    (res3,) = append_projects(doc, [src], chunks=["A"])
    assert res3.ties == 1 and sorted(res3.ties_dropped) == ["legame 1", "picchetto"]  # GNSS 1 copied


def test_a_cancelled_append_takes_its_ties_away_too(tmp_path):
    from pyarchinit_photogrammetry.core.append import append_projects
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    src = make_tied_source(tmp_path / "src")
    other = make_source(tmp_path / "src2")
    doc = _target(tmp_path, "X")

    seen = []

    def cb(fraction, message):
        seen.append("ties" in doc.meta)
        if fraction > 0.6:
            p.cancel()

    p = Progress(cb)
    with pytest.raises(Cancelled):
        append_projects(doc, [src, other], progress=p)
    assert any(seen)  # the ties of the first source were there when the second was cancelled
    assert [c.label for c in doc.chunks] == ["X"] and "ties" not in doc.meta


def test_cli_append_says_the_ties(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    src = make_tied_source(tmp_path / "src")
    _target(tmp_path, "X")
    r = CliRunner().invoke(app, ["append", str(tmp_path / "dst" / "mio.ppg"), str(src), "--chunks", "A,B"])
    assert r.exit_code == 0, r.output
    assert "3 ties between the chunks appended" in r.output and "tie «legame 2» not copied" in r.output
