"""Paradata: W3C PROV provenance of the products, beside exports and PyArchInit media."""
from __future__ import annotations

import hashlib
import json

import pytest

from pyarchinit_photogrammetry.core import paradata


def _nodes(doc):
    return {n["@id"]: n for n in doc["@graph"]}


def test_provenance_of_the_orthomosaic(processed):
    _, ch, _ = processed
    doc = paradata.provenance(ch, "orthomosaic", operator="Enzo Cocca")
    json.dumps(doc)  # plain JSON-LD
    assert doc["@context"]["prov"] == "http://www.w3.org/ns/prov#"
    nodes = _nodes(doc)
    ortho = nodes["ppg:chunk/0/orthomosaic"]
    assert ortho["ppg:sha256"] == hashlib.sha256(ch.orthomosaic_path.read_bytes()).hexdigest()
    chain, seen = ["ppg:chunk/0/orthomosaic"], set()
    while chain:  # every source is a node of the graph, down to the photos
        nid = chain.pop()
        seen.add(nid)
        chain += [s for s in nodes[nid].get("prov:wasDerivedFrom", []) if s not in seen]
    assert {"ppg:chunk/0/dem", "ppg:chunk/0/point_cloud", "ppg:chunk/0/reconstruction", "ppg:chunk/0/photos",
            "ppg:chunk/0/georeference"} <= seen
    assert nodes["ppg:chunk/0/photos"]["ppg:count"] == len(ch.cameras)
    geo = nodes["ppg:chunk/0/georeference"]
    assert geo["ppg:crs"] == ch.crs and geo["ppg:control_rmse_m"] < 0.002
    activity = nodes[ortho["prov:wasGeneratedBy"]]
    assert activity["prov:label"] == "build_orthomosaic" and "resolution" in activity["ppg:parameters"]
    assert "ppg:operator" in activity["prov:wasAssociatedWith"]
    assert "photos aligned" in paradata.summary(ch, "orthomosaic")


def test_beside_an_export(processed, tmp_path):
    _, ch, _ = processed
    out = ch.export_dem(tmp_path / "dem.tif")
    jl = paradata.write_provenance(ch, "dem", out)
    assert jl.name == "dem.tif.prov.jsonld"
    nodes = _nodes(json.loads(jl.read_text()))
    copy = nodes["ppg:file/dem.tif"]
    assert copy["prov:wasDerivedFrom"] == ["ppg:chunk/0/dem"] and copy["ppg:sha256"]


def test_cli(processed, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, _, _ = processed
    out = tmp_path / "prov.jsonld"
    r = CliRunner().invoke(app, ["paradata", str(doc.path), "--product", "dem", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "ppg:chunk/0/dem" in out.read_text()


def test_published_media_carry_their_paradata(pa_db, processed, tmp_path):
    from pathlib import Path

    from pyarchinit_photogrammetry.pyarchinit.bridge import PyArchInit

    _, ch, _ = processed
    pa = PyArchInit.connect(media_root=tmp_path / "media")
    res = pa.publish_orthomosaic(ch, "Saggio", "Pianta paradati")
    jl = Path(res["paradata"])
    assert jl.exists() and jl.name.endswith(".tif.prov.jsonld")
    nodes = _nodes(json.loads(jl.read_text()))
    assert any(n.get("ppg:sha256") for n in nodes.values())
    doc = pa.db.query("documentazione_table", nome_doc="Pianta paradati")[0]
    assert "photos aligned" in (doc.get("note") or "")
    pytest.importorskip("rasterio")
