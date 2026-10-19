"""IFC and the Extended Matrix: a unit keeps the node_uuid of its US form as GlobalId, and the
stratigraphic relations of the forms become one IfcRelConnectsElements per fact."""
from __future__ import annotations

import uuid

import numpy as np
import pytest
from shapely.geometry import box

from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon, stratigraphic_edges


def test_each_relation_once_as_an_edge():
    declared = [("1", [["Copre", "2", "1", "S"], ["Uguale a", "3", "1", "S"]]),
                ("2", [["Coperto da", "1", "1", "S"], ["Tagliato da", "4", "1", "S"]]),
                ("3", [["Uguale a", "1", "1", "S"]]), ("4", [["Taglia", "2", "1", "S"]]),
                ("5", [["Covers", "6"]]), ("6", [["covered by", "5"]]), ("7", [["Prima di", "8"], ["Copre", "7"]])]
    assert stratigraphic_edges(declared) == [("1", "Copre", "2", "overlies"), ("1", "Uguale a", "3", "equals"),
                                             ("4", "Taglia", "2", "cuts"), ("5", "Covers", "6", "overlies"),
                                             ("7", "Prima di", "8", None)]
    assert stratigraphic_edges([("2", [["Gli si appoggia", "9"]]), ("9", [["Si appoggia a", "2"]])]) == \
        [("9", "Si appoggia a", "2", "abuts")]


class _Chunk:
    """What export_chunk_ifc reads of a chunk: a flat model 4 x 3 m at 100 m, in EPSG:3004."""

    label, crs, meta, transform = "Pianta", "EPSG:3004", {}, np.eye(4)

    @property
    def model(self):
        import trimesh

        xs, ys = np.meshgrid(np.linspace(0, 4, 41), np.linspace(0, 3, 31))
        v = np.column_stack([xs.ravel() + 2480000, ys.ravel() + 4500000, np.full(xs.size, 100.0)])
        f = []
        for r in range(30):
            for c in range(40):
                i = r * 41 + c
                f += [[i, i + 1, i + 42], [i, i + 42, i + 41]]
        return trimesh.Trimesh(v, np.array(f), process=False)


def _u(x0, y0, x1, y1, us, tipo="US"):
    return UnitPolygon(box(2480000 + x0, 4500000 + y0, 2480000 + x1, 4500000 + y1), us=us, area="1", tipo=tipo)


def test_units_keep_their_node_and_relations(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.guid

    from pyarchinit_photogrammetry.bim.ifc import export_chunk_ifc, read_ifc_elements

    n1, n2 = str(uuid.uuid4()), str(uuid.uuid4())
    units = [_u(0, 0, 1, 1, "1"), _u(2, 2, 3, 3, "1"), _u(1, 0, 2, 1, "2"), _u(0, 1, 1, 2, "3", "USM")]
    records = {"1": {"node_uuid": n1, "entity_uuid": "e1",
                     "rapporti": "[['Copre', '2', '1', 'Saggio'], ['Si appoggia a', '3', '1', 'Saggio']]"},
               "2": {"node_uuid": n2, "rapporti": "[['Coperto da', '1', '1', 'Saggio']]"},
               "3": {"rapporti": "[['Gli si appoggia', '1', '1', 'Saggio']]"}}
    path = export_chunk_ifc(_Chunk(), tmp_path / "us.ifc", units=units, us_records=records, site="Saggio",
                            include_survey=False)
    els = {e["name"]: e for e in read_ifc_elements(path)}
    assert set(els) == {"US 1", "US 2", "USM 3"}  # the two polygons of US 1 are one element
    assert els["US 1"]["global_id"] == ifcopenshell.guid.compress(uuid.UUID(n1).hex)
    assert ifcopenshell.guid.expand(els["US 2"]["global_id"]) == uuid.UUID(n2).hex
    assert els["US 1"]["psets"]["Pset_PyArchInit_US"]["node_uuid"] == n1
    assert els["US 1"]["vertices"][:, 0].max() > 2480002.5  # both parts
    f = ifcopenshell.open(str(path))
    rels = {(r.RelatingElement.Name, r.Name, r.RelatedElement.Name, r.Description)
            for r in f.by_type("IfcRelConnectsElements")}
    assert rels == {("US 1", "Copre", "US 2", "overlies (Extended Matrix)"),
                    ("US 1", "Si appoggia a", "USM 3", "abuts (Extended Matrix)")}


def test_a_node_used_twice_keeps_one_id(tmp_path):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import trimesh

    from pyarchinit_photogrammetry.bim.ifc import IfcElement, export_ifc, global_id

    gid = global_id(uuid.uuid4())
    assert global_id("not a uuid") is None and global_id(None) is None
    a = IfcElement(trimesh.creation.box(), "US 1", global_id=gid)
    b = IfcElement(trimesh.creation.box(), "US 1 bis", global_id=gid)
    f = ifcopenshell.open(str(export_ifc(tmp_path / "x.ifc", [a, b], crs="EPSG:3004")))
    ids = [e.GlobalId for e in f.by_type("IfcGeographicElement")]
    roots = [e.GlobalId for e in f.by_type("IfcRoot")]
    assert ids[0] == gid and ids[1] != gid and len(set(roots)) == len(roots)
