import numpy as np

from pyarchinit_photogrammetry.assistant.privacy import ABSOLUTE_KEYS, WARNING, Filter
from pyarchinit_photogrammetry.core.camera import Camera
from pyarchinit_photogrammetry.core.document import Document

CAMERA = "/Users/x/Scavi/Tell Sultan/foto/DJI_0001.JPG"


def make_document(tmp_path, second_site=False):
    doc = Document(tmp_path / "Scavi privati" / "Rilievo Nord.ppg")
    doc.meta["sito"] = "Tell Sultan"
    ch = doc.add_chunk("Volo 28/08")
    ch.cameras.append(Camera(key=1, label="DJI_0001", path=CAMERA, sensor_key=0))
    units = [{"wkt": "POLYGON EMPTY", "sito": "Tell Sultan", "us": "12", "tipo": "US",
              "attributes": {"comune": "Lagoi", "schedatore": "Mario Rossi"}}]
    if second_site:
        units.append({"wkt": "POLYGON EMPTY", "sito": "Tell Sultan Est", "us": "13", "tipo": "US", "attributes": {}})
    ch.meta["drawings"] = {"units": units, "quote": []}
    return doc


def test_site_place_and_people_names_leave_as_placeholders(tmp_path):
    f = Filter(make_document(tmp_path, second_site=True))
    out = f.outgoing("Il DEM di Tell Sultan a Lagoi, scheda di Mario Rossi")
    assert out == "Il DEM di Sito A a Luogo 1, scheda di Persona 1"
    assert f.incoming(out) == "Il DEM di Tell Sultan a Lagoi, scheda di Mario Rossi"
    assert f.outgoing("tell sultan e LAGOI") == "Sito A e Luogo 1"
    # the longest name first: the second site is not "Sito A Est"
    assert f.outgoing("Tell Sultan Est") == "Sito B"
    assert f.incoming("Sito B e Sito A") == "Tell Sultan Est e Tell Sultan"
    assert f.outgoing("Tell Sultana") == "Tell Sultana"  # whole words only
    # the same name is always the same placeholder
    assert f.outgoing("Lagoi, Lagoi") == "Luogo 1, Luogo 1"
    # the model's lowercase Italian is not a placeholder
    assert f.incoming("il sito a nord") == "il sito a nord"


def test_extra_people_and_places(tmp_path):
    f = Filter(None, people=["Anna Verdi"], places=["Veio"])
    assert f.outgoing("Anna Verdi a Veio") == "Persona 1 a Luogo 1"
    assert f.incoming("Persona 1 a Luogo 1") == "Anna Verdi a Veio"


def test_chunk_project_and_paths_are_neutral(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("Volo 28/08") == "chunk 1"
    assert f.outgoing("Rilievo Nord") == "progetto"
    assert f.outgoing(str(tmp_path / "Scavi privati" / "Rilievo Nord.ppg")) == "progetto"
    out = f.outgoing(CAMERA)
    assert "/Users" not in out and "Tell Sultan" not in out
    assert out == "DJI_0001"
    other = f.outgoing("guarda /Users/x/Scavi/Tell Sultan/foto 12.jpg e C:\\Dati\\Tell Sultan\\a b.tif ora")
    assert other == "guarda foto_12 e a_b ora"
    assert f.outgoing("la cartella /Users/x/Tell_Sultan_nord") == "la cartella Tell_Sultan_nord"
    assert f.outgoing("foto 12.jpg") == "foto 12.jpg"  # not a camera of the project, not a path
    assert f.outgoing("https://example.org/a/b.html") == "https://example.org/a/b.html"


def test_facts_drop_absolute_coordinates_and_the_crs(tmp_path):
    f = Filter(make_document(tmp_path))
    data = {"crs": "EPSG:32637", "bounds": [1.0, 2.0, 3.0, 4.0], "resolution": 0.07, "z_min": 312.4,
            "height_above_lowest": {"max": 4.2}, "site": "Tell Sultan"}
    assert f.facts(data) == {"crs": "proiettato, in metri", "resolution": 0.07, "height_above_lowest": {"max": 4.2},
                             "site": "Sito A"}
    assert f.facts({"crs": "EPSG:4326"}) == {"crs": "geografico (gradi)"}
    assert f.facts({"crs": "LOCAL"}) == {"crs": "locale"}
    assert data["bounds"] == [1.0, 2.0, 3.0, 4.0]  # the input is not touched


def test_facts_nested_numpy_and_paths(tmp_path):
    f = Filter(make_document(tmp_path))
    out = f.facts({"items": [{"name": "Lagoi", "n": np.int64(3), "area": np.float64(1.5), "origin": [0, 0]},
                             ("Mario Rossi", np.array([1, 2]))],
                   "file": CAMERA, "gps": {"lat": 1}, "none": None})
    assert out == {"items": [{"name": "Luogo 1", "n": 3, "area": 1.5}, ("Persona 1", [1, 2])],
                   "file": "DJI_0001", "none": None}
    assert type(out["items"][0]["n"]) is int
    for key in ABSOLUTE_KEYS:
        assert key not in f.facts({key: 1})


def test_marker_and_unit_names_pass(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("marker T3 e US 12") == "marker T3 e US 12"
    assert f.unit_label("US 12") == "US 12"


def test_passthrough_changes_nothing():
    f = Filter.passthrough()
    data = {"crs": "EPSG:32637", "bounds": [1, 2], "site": "Tell Sultan"}
    assert f.outgoing("Tell Sultan /Users/x/a.jpg") == "Tell Sultan /Users/x/a.jpg"
    assert f.incoming("Sito A") == "Sito A"
    assert f.facts(data) is data
    assert f.mapping == {}
    assert "coordinate" in WARNING


def test_mapping_lists_every_replacement(tmp_path):
    f = Filter(make_document(tmp_path, second_site=True), people=["Anna Verdi"])
    assert f.mapping == {"Tell Sultan": "Sito A", "Tell Sultan Est": "Sito B", "Lagoi": "Luogo 1",
                         "Mario Rossi": "Persona 1", "Anna Verdi": "Persona 2", "Volo 28/08": "chunk 1",
                         "Rilievo Nord": "progetto", "Scavi privati": "progetto"}
