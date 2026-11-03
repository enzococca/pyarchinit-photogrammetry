"""The fact tools of the assistant: what they say of the open project, and that they never say where it is."""
from __future__ import annotations

import json

import numpy as np
import pytest

from pyarchinit_photogrammetry.assistant import facts as F
from pyarchinit_photogrammetry.assistant.knowledge import default_index
from pyarchinit_photogrammetry.assistant.privacy import Filter
from pyarchinit_photogrammetry.core.document import Document

PROJECT_TOOLS = [n for n in F.tool_names() if n not in ("guide_search", "scripting_api")]
GUIDE_ARGUMENTS = {"guide_search": {"query": "curve di livello"}, "scripting_api": {"topic": "build_dem"}}


def _run(name, doc, **arguments):
    return F.run_tool(name, arguments, doc, default_index("it"))


def test_tool_specs_are_what_a_model_reads():
    names = F.tool_names()
    assert names == [t.name for t in F.TOOLS if not t.acts]  # what reads; the actions only with the window
    assert F.tool_names(acts=True) == [t.name for t in F.TOOLS]
    assert {"project_summary", "dem_statistics", "orthomosaic_info", "alignment_report", "markers", "thermal_info",
            "multispectral_info", "point_cloud_info", "model_info", "tin_info", "guide_search", "scripting_api"} <= set(names)
    for t in F.TOOLS:
        assert t.description and t.parameters["type"] == "object"
        json.dumps(t.parameters)  # a JSON schema
        if t.name in PROJECT_TOOLS and t.name != "project_summary":
            assert "chunk" in t.parameters["properties"]
            assert "chunk" not in t.parameters.get("required", [])


def test_project_summary_and_products(processed):
    doc, ch, _ = processed
    out = _run("project_summary", doc)
    assert out["project"] == "project"
    assert out["site"] is None
    assert len(out["chunks"]) == 1
    c = out["chunks"][0]
    assert c["label"] == "Saggio 1" and c["key"] == ch.key and c["enabled"] is True
    assert c["photos"] == len(ch.cameras) and c["aligned"] == len(ch.aligned_cameras) > 0
    assert c["crs"] == ch.crs
    assert {"dem", "orthomosaic", "model", "point_cloud", "tie_points"} <= set(c["products"])
    # the chunk by its label or its key, else the labels are listed
    by_label = _run("point_cloud_info", doc, chunk="Saggio 1")["points"]
    assert by_label == _run("point_cloud_info", doc, chunk=ch.key)["points"]


def test_dem_statistics_are_relative(processed):
    import rasterio

    doc, ch, _ = processed
    out = _run("dem_statistics", doc)
    with rasterio.open(ch.dem_path) as ds:
        z = ds.read(1, masked=True)
        z = np.ma.masked_invalid(z)
        span = float(z.max() - z.min())
        assert out["width"] == ds.width and out["height"] == ds.height
        assert out["resolution"] == pytest.approx(abs(ds.res[0]), rel=1e-6)
    h = out["height_above_lowest"]
    assert h["min"] == 0
    assert h["max"] == pytest.approx(span, rel=0.05)
    assert 0 <= h["p05"] <= h["median"] <= h["p95"] <= h["max"]
    assert 50 <= out["coverage_percent"] <= 100
    assert out["nodata_percent"] == pytest.approx(100 - out["coverage_percent"], abs=1e-6)
    assert out["crs"] == ch.crs
    assert "version" in out
    for key in out:
        assert key not in ("bounds", "origin", "z_min", "z_max")


def test_orthomosaic_point_cloud_and_model(processed):
    doc, ch, _ = processed
    ortho = _run("orthomosaic_info", doc)
    assert ortho["width"] > 0 and ortho["height"] > 0 and ortho["bands"] in (3, 4)
    assert ortho["resolution"] > 0 and ortho["blending"] in ("mosaic", "disabled", "average")
    assert ortho["patches"] == 0
    pc = _run("point_cloud_info", doc)
    assert pc["points"] == len(ch.point_cloud)
    assert sum(pc["classes"].values()) == pc["points"]
    assert pc["confidence_bins"] is None or sum(pc["confidence_bins"].values()) == pc["points"]
    model = _run("model_info", doc)
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    m = read_mesh(ch.product_path("model"))
    assert model["faces"] == len(m.faces) and model["vertices"] == len(m.vertices)
    assert model["texture_pages"] == 1 and model["textured"] is True
    assert len(model["texture_size"]) == 2 and all(0 < v <= 2048 for v in model["texture_size"])


def test_alignment_report_has_the_gcp_rmse(processed):
    doc, ch, report = processed
    out = _run("alignment_report", doc)
    assert out["photos"] == len(ch.cameras) and out["aligned"] == len(ch.aligned_cameras)
    assert out["tie_points"] == len(ch.tie_points)
    assert out["reprojection_error_px"] is not None and 0 < out["reprojection_error_px"] < 3
    assert out["matcher"] == "sift" and out["mapper"] in ("incremental", "global", "hierarchical")
    gcp = out["gcp"]
    assert gcp["n"] > 0 and gcp["control_rmse"] < 0.002
    assert gcp["control_rmse"] == pytest.approx(report["control_rmse"], rel=1e-4)
    assert gcp["check_rmse"] is None or gcp["check_rmse"] < 0.003
    assert out["scalebars"]["n"] == 0
    markers = _run("markers", doc)["markers"]
    assert len(markers) == len(ch.markers)
    for m in markers:
        assert set(m) >= {"name", "projections", "role", "error_m"}
        assert m["role"] in ("control", "check")
        assert m["projections"] > 0
        assert not {"x", "y", "z", "position", "reference"} & set(m)


def test_results_pass_the_privacy_filter_whole(processed):
    """Every number of a result is derived: the filter for an external model changes only the names and the CRS."""
    doc, ch, _ = processed
    f = Filter(doc)
    for name in F.tool_names():
        out = _run(name, doc, **GUIDE_ARGUMENTS.get(name, {}))
        assert "error" not in out, (name, out)
        kept = f.facts(out)
        # the keys stay (a key that is also a name of the project - "project" here - leaves as its placeholder)
        assert set(kept) == {f.outgoing(k) for k in out}, (name, set(out) - set(kept))
        for key, value in out.items():
            if isinstance(value, dict):
                assert set(f.facts(value)) == {f.outgoing(k) for k in value}, (name, key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        assert set(f.facts(item)) == {f.outgoing(k) for k in item}, (name, key)
        json.dumps(kept)


def test_thermal_and_multispectral_say_none_without_them(processed):
    doc, _, _ = processed
    assert "none" in _run("thermal_info", doc)
    assert "none" in _run("multispectral_info", doc)


def test_tools_on_an_empty_project():
    doc = Document()
    doc.add_chunk("Vuoto")
    doc.chunks[0].enabled = False  # a project whose chunks are all disabled says so
    out = _run("dem_statistics", doc)
    assert "none" in out and "disattivati" in out["none"] and "Vuoto" in out["none"]
    doc.chunks[0].enabled = True
    for name in PROJECT_TOOLS:
        out = _run(name, doc)
        assert isinstance(out, dict) and "none" in out, (name, out)
        assert isinstance(out["none"], str) and out["none"]
    for name in PROJECT_TOOLS:
        out = _run(name, None)
        assert isinstance(out, dict) and "none" in out, (name, out)
    # a document without chunks
    assert "none" in _run("dem_statistics", Document())


def test_tin_info_says_the_counts_and_never_a_height(tmp_path):
    from pyarchinit_photogrammetry.engine import tin as T
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    doc = Document()
    ch = doc.add_chunk("Saggio")
    ch.crs = "EPSG:32633"
    doc.save(tmp_path / "p.ppg")
    assert _run("tin_info", doc) == {"none": "il chunk non ha un TIN (Strumenti > TIN...)"}
    yy, xx = np.mgrid[0:100, 0:100]
    z = (120.0 + 0.02 * xx + 0.3 * np.sin(yy / 8.0)).astype(np.float32)
    ch._set_product("dem", write_geotiff(ch.data_dir / "dem.tif", z, (500000.0, 4500002.0), 0.02, ch.crs))
    T.build_tin(ch, source="dem", max_error=0.01)
    out = _run("tin_info", doc)
    meta = ch.meta["tin"]
    assert out["source"] == "dem" and out["vertices"] == meta["n_vertices"] > 3
    assert out["triangles"] == meta["n_triangles"] and out["constrained_edges"] == 0
    assert out["tolerance_m"] == pytest.approx(0.01) and out["error_max_m"] <= 0.0101
    assert "file" not in out and not any(isinstance(v, float) and v > 100 for v in out.values())
    f = Filter(doc)
    assert set(f.facts(out)) == {f.outgoing(k) for k in out}  # nothing to take away
    json.dumps(out)


def test_unknown_tool_and_bad_chunk(processed):
    doc, _, _ = processed
    assert _run("nope", doc) == {"error": "unknown tool nope"}
    out = _run("dem_statistics", doc, chunk="Saggio 9")
    assert "error" in out and "Saggio 9" in out["error"] and "Saggio 1" in out["error"]
    # a chunk that is not a label nor a key (true would be the key 1), arguments that are not a JSON object
    for wanted in (True, 1.5, ["Saggio 1"], {"label": "Saggio 1"}):
        out = _run("dem_statistics", doc, chunk=wanted)
        assert "error" in out and "Saggio 1" in out["error"], wanted
    for arguments in ("notadict", 5, ["a"], [("chunk", "Saggio 1", 3)]):
        out = F.run_tool("dem_statistics", arguments, doc, default_index("it"))
        assert set(out) == {"error"} and "JSON object" in out["error"], arguments
    # a tool that fails for another reason answers with the error, never raises
    bad = Document()
    ch = bad.add_chunk("Rotto")
    ch.products["dem"] = "missing.tif"
    assert "none" in _run("dem_statistics", bad)
    ch.products["dem"] = "dem.tif"
    (ch.data_dir / "dem.tif").write_bytes(b"not a tiff")
    out = _run("dem_statistics", bad)
    assert "error" in out and "Error" in out["error"]


def test_guide_search_tool():
    out = F.run_tool("guide_search", {"query": "come faccio le curve di livello", "k": 3}, None, default_index("it"))
    assert len(out["passages"]) == 3
    p = out["passages"][0]
    assert set(p) >= {"title", "page", "text"}
    assert "curve di livello" in (p["title"] + " " + p["text"]).lower()
    # the language asked for wins over the index given
    en = F.run_tool("guide_search", {"query": "how do I make contour lines", "lang": "en"}, None, default_index("it"))
    assert en["passages"] and all(not p["page"].endswith(".it.md") for p in en["passages"])
    assert F.run_tool("guide_search", {"query": ""}, None, default_index("it")) == {"passages": []}


def test_scripting_api_tool_reads_only_the_two_pages():
    out = F.run_tool("scripting_api", {"topic": "build_dem resolution"}, None, default_index("it"))
    assert out["passages"]
    assert {p["page"] for p in out["passages"]} <= {"scripting.it.md", "api.it.md"}
    en = F.run_tool("scripting_api", {"topic": "build_dem resolution", "lang": "en"}, None, default_index("en"))
    assert en["passages"] and {p["page"] for p in en["passages"]} <= {"scripting.md", "api.md"}
    assert any("build_dem" in p["text"] for p in en["passages"])


def test_reprojection_error_is_stored_by_the_alignment_and_computed_once(processed, monkeypatch):
    import pycolmap

    doc, ch, _ = processed
    sfm = ch.meta["sfm"]
    assert sfm.get("reprojection_error") is not None  # align_cameras stores it, as optimize_cameras does
    stored = sfm["reprojection_error"]

    def boom(*a, **k):
        raise AssertionError("the reconstruction was loaded")

    monkeypatch.setattr(pycolmap, "Reconstruction", boom)
    out = _run("alignment_report", doc)
    assert out["reprojection_error_px"] == pytest.approx(stored, abs=1e-4)
    assert out["reprojection_error_source"] == "stored"
    monkeypatch.undo()
    # without the stored value (a project aligned by an earlier version): computed from the model once
    sfm.pop("reprojection_error")
    F._REPROJECTION_CACHE.clear()
    try:
        out = _run("alignment_report", doc)
        assert out["reprojection_error_source"] == "computed"
        assert out["reprojection_error_px"] == pytest.approx(stored, abs=1e-4)
        monkeypatch.setattr(pycolmap, "Reconstruction", boom)
        again = _run("alignment_report", doc)
        assert again["reprojection_error_px"] == out["reprojection_error_px"]
        assert again["reprojection_error_source"] == "computed"
    finally:
        monkeypatch.undo()
        sfm["reprojection_error"] = stored
        F._REPROJECTION_CACHE.clear()


def test_non_square_cells_and_thermal_registration(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    doc = Document(tmp_path / "rett.ppg")
    ch = doc.add_chunk("Rettangoli")
    ch.crs = "EPSG:32633"
    z = np.full((3, 4), 100.0, np.float32)
    z[0, 0] = np.nan
    with rasterio.open(ch.data_dir / "dem.tif", "w", driver="GTiff", width=4, height=3, count=1, dtype="float32",
                       crs="EPSG:32633", transform=from_origin(500000.0, 4500000.0, 0.5, 0.25), nodata=np.nan) as dst:
        dst.write(z, 1)
    ch.products["dem"] = "dem.tif"
    out = _run("dem_statistics", doc)
    assert out["resolution"] == [0.5, 0.25]
    assert out["coverage_percent"] == pytest.approx(100 * 11 / 12, abs=0.01)
    assert out["height_above_lowest"]["max"] == 0
    # a thermal registration as engine.thermal.registrations stores it: no homography leaves, the numbers do
    ch.meta["multispectral"] = {"bands": ["LWIR", "Visible"], "reference": "Visible", "captures": 2}
    ch.meta["thermal"] = {"params": {"emissivity": 0.9}, "decoded": 2,
                          "registration": {"factor": 4, "cover": 0.65, "cover_from_focals": 0.7,
                                           "fixed": [[1, 0, 0]] * 3, "fixed_cc": 0.31,
                                           "captures": {"1": {"h": [[1, 0, 0]] * 3, "cc": 0.4, "refined": True},
                                                        "2": {"h": [[1, 0, 0]] * 3, "cc": 0.31, "refined": False}}}}
    out = _run("thermal_info", doc)
    assert out["registration"] == {"cover": 0.65, "correlation": 0.31, "captures": 2, "refined": 1}
    assert out["params"]["emissivity"] == 0.9 and out["params"]["reflected"] == 22
    assert out["orthomosaic"] is None and out["frames"] == {"captures": 0, "decoded": 2}
    kept = Filter(doc).facts(out)
    assert set(kept) == set(out)


def test_unknown_language_falls_back_to_italian():
    for lang in ("xx", 5, None, ""):
        out = F.run_tool("guide_search", {"query": "curve di livello", "lang": lang}, None, default_index("it"))
        assert out["passages"] and all(p["page"].endswith(".it.md") for p in out["passages"]), lang
    out = F.run_tool("scripting_api", {"topic": "build_dem", "lang": "xx"}, None, None)
    assert out["passages"] and {p["page"] for p in out["passages"]} <= {"scripting.it.md", "api.it.md"}
