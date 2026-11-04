"""``ppg drawings3d`` (list, export, import), the free drawings in the plugin API (``Context.free_drawings``) and in the
assistant's inventory of the project."""
from __future__ import annotations

import numpy as np
from typer.testing import CliRunner

from pyarchinit_photogrammetry.core import free_drawings as fd

X0, Y0 = 2480000.0, 4500000.0


def _project(tmp_path):
    """A project with two chunks in EPSG:3004: "A" with a line, a polygon in "Muri" and a point; "B" empty."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    a = doc.add_chunk("A")
    b = doc.add_chunk("B")
    for ch in (a, b):
        ch.crs = "EPSG:3004"
    fd.add(a, "line", [[X0, Y0, 100.0], [X0 + 3.0, Y0 + 4.0, 100.0]], name="Linea A")
    fd.add(a, "polygon", [[X0, Y0, 100.0], [X0 + 1.0, Y0, 100.0], [X0 + 1.0, Y0 + 1.0, 100.0]], layer="Muri",
           name="Muro", attributes={"materiale": "pietra"})
    fd.add(a, "point", [[X0 + 5.0, Y0 + 5.0, 101.5]], name="P")
    doc.chunk = a
    doc.save(tmp_path / "progetto.ppg")
    return doc


def _run(*args):
    from pyarchinit_photogrammetry.cli import app

    return CliRunner().invoke(app, ["drawings3d", *[str(a) for a in args]])


def test_cli_list_export_import(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc = _project(tmp_path)
    path = doc.path
    r = _run(path, "list")
    assert r.exit_code == 0, r.output
    lines = r.output.strip().splitlines()
    assert len(lines) == 1 + 3  # header + one row per feature
    line = next(x for x in lines if "Linea A" in x)
    assert "line" in line and "5.000" in line  # 3D length of the 3-4-5 line
    poly = next(x for x in lines if "Muro" in x)
    assert "Muri" in poly and "0.500" in poly  # its 3D area
    r = _run(path, "list", "--layer", "Muri")
    assert r.exit_code == 0 and len(r.output.strip().splitlines()) == 2
    before = path.read_bytes()
    out = tmp_path / "disegni.gpkg"
    r = _run(path, "export", "--out", out)
    assert r.exit_code == 0, r.output
    assert out.exists() and path.read_bytes() == before  # export does not save the project
    r = _run(path, "export", "--out", tmp_path / "x.txt")
    assert r.exit_code != 0  # an unknown format: refused
    assert _run(path, "export", "--out", tmp_path / "m.bin", "--format", "geojson", "--layer", "Muri").exit_code == 0
    r = _run(path, "import", out, "--chunk", "B")
    assert r.exit_code == 0, r.output
    assert "3" in r.output
    again = ppg.Document.open(path)
    a = next(c for c in again.chunks if c.label == "A")
    b = next(c for c in again.chunks if c.label == "B")
    got = {r["name"]: r for r in fd.features(b)}
    for rec in fd.features(a):
        assert np.allclose(got[rec["name"]]["xyz"], rec["xyz"], atol=1e-9)
        assert got[rec["name"]]["layer"] == rec["layer"]
    r = _run(path, "import", tmp_path / "nothing.gpkg")
    assert r.exit_code != 0
    r = _run(path, "nonsense")
    assert r.exit_code != 0


def test_context_free_drawings_is_a_copy(tmp_path):
    from pyarchinit_photogrammetry.app import app as ppg_app
    from pyarchinit_photogrammetry.plugins.api import Context

    doc = _project(tmp_path)
    ctx = Context("prova", tmp_path / "ctx")
    old = ppg_app.document
    ppg_app.document = doc
    try:
        got = ctx.free_drawings()
        assert {r["name"] for r in got} == {"Linea A", "Muro", "P"}
        assert [r["name"] for r in ctx.free_drawings(layer="Muri")] == ["Muro"]
        b = next(c for c in doc.chunks if c.label == "B")
        assert ctx.free_drawings(chunk=b) == []
        got[0]["xyz"][0][0] = -1.0
        got[0]["name"] = "cambiato"
        got[1]["attributes"]["x"] = 1
        stored = doc.chunk.meta["drawings"]["free"]
        assert all(r["name"] != "cambiato" for r in stored) and stored[0]["xyz"][0][0] == X0
        assert "x" not in stored[1]["attributes"]
    finally:
        ppg_app.document = old


def test_inventory_counts_free_drawings(tmp_path):
    from pyarchinit_photogrammetry.assistant import inventory as I

    doc = _project(tmp_path)
    assert "free" in I.DRAWINGS
    snap = I.snapshot(doc)
    a, b = snap["chunks"]
    assert a["drawings"]["free"] == 3 and b["drawings"]["free"] == 0
    assert a["free_layers"] == [{"name": "Disegni 3D", "line": 1, "polygon": 0, "point": 1},
                                {"name": "Muri", "line": 0, "polygon": 1, "point": 0}]
    it, en = I.text(snap, "it"), I.text(snap, "en")
    assert "3 disegni 3D" in it and "Muri" in it and "1 poligoni" in it
    assert "3 3D drawings" in en and "«Muri»: 1 polygons" in en
    assert I._WORDS["it"]["polygon"] == "poligono di {0} m²"  # the words of the selection kept
