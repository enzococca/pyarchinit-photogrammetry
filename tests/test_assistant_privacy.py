import re

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
    assert f.outgoing("la cartella /Users/x/Tell_Sultan_nord") == "la cartella Sito A_nord"
    assert f.outgoing("foto 12.jpg") == "foto 12.jpg"  # not a camera of the project, not a path
    assert f.outgoing("vedi https://example.org/a/b.html.") == "vedi b."


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


def test_names_glued_to_underscores_digits_and_gaps(tmp_path):
    doc = make_document(tmp_path)
    doc.chunks[0].cameras.append(Camera(key=2, label="x", path="/Users/x/foto/Tell_Sultan_0001.JPG", sensor_key=0))
    f = Filter(doc)
    assert f.outgoing("/Users/x/foto/Tell_Sultan_0001.JPG") == "Sito A_0001"
    assert f.outgoing("/data/Tell_Sultan_dem.tif") == "Sito A_dem"
    assert f.facts({"cameras": ["/Users/x/foto/Tell_Sultan_0001.JPG"]}) == {"cameras": ["Sito A_0001"]}
    assert f.outgoing("Tell-Sultan, TELL.SULTAN e tell_sultan") == "Sito A, Sito A e Sito A"
    assert f.outgoing("Tell Sultan2026.JPG") == "Sito A2026.JPG"  # a digit is no part of a word
    assert f.outgoing("2026Tell-Sultan.JPG") == "2026Sito A.JPG"
    assert f.outgoing("TellSultanX e TellSultan_01") == "TellSultanX e TellSultan_01"  # letters make another word


def test_crs_fails_closed(tmp_path):
    from pyproj import CRS

    f = Filter(make_document(tmp_path))
    assert f.facts({"crs": 32637}) == {"crs": "proiettato, in metri"}
    assert f.facts({"target_crs": 4326}) == {"target_crs": "geografico (gradi)"}
    assert f.facts({"crs": {"epsg": 32637}}) == {"crs": "proiettato, in metri"}
    assert f.facts({"crs": ["EPSG:32637"]}) == {"crs": "proiettato, in metri"}
    assert f.facts({"crs": CRS.from_epsg(32637)}) == {"crs": "proiettato, in metri"}
    assert f.facts({"crs": "boh"}) == {"crs": "sistema non detto"}
    assert f.facts({"crs_wkt": 'PROJCS["WGS 84 / UTM zone 37N"]', "epsg": 32637, "utm_zone": "37N", "srs": 32637,
                    "proj": "+proj=utm +zone=37"}) == {"crs_wkt": "sistema non detto", "epsg": "proiettato, in metri",
                                                       "utm_zone": "sistema non detto", "srs": "proiettato, in metri",
                                                       "proj": "proiettato, in metri"}


def test_facts_drop_absolute_geometry_and_heights(tmp_path):
    f = Filter(make_document(tmp_path))
    unit = {"wkt": "POLYGON((4512345 2314567, 4512350 2314567, 4512350 2314570, 4512345 2314567))", "sito": "Tell Sultan",
            "attributes": {"quota_abs": 312.4, "us": "12"}}
    assert f.facts(unit) == {"sito": "Sito A", "attributes": {"us": "12"}}
    assert f.facts({"wkt": "POINT(1 2)", "quota_q": 312.4, "n": 3}) == {"n": 3}
    for key in ("bbox", "extent", "centroid", "position", "location", "coordinates", "xyz", "geometry", "xmin", "x_max",
                "zMin", "z_min_m", "dem_min", "lon_deg", "min_z", "Easting", "quota"):
        assert f.facts({key: [1.5, 2.5], "ok": 1}) == {"ok": 1}, key
    assert f.facts({"polygon": [[1.0, 2.0], [3.0, 4.0]], "points": 120, "line": "a"}) == {"points": 120, "line": "a"}
    assert f.facts({"note": "POINT(1 2)"}) == {}


def test_accents_decomposed_and_apostrophes(tmp_path):
    doc = make_document(tmp_path)
    doc.meta["sito"] = "Città di Castello"
    doc.chunks[0].meta["drawings"]["units"][0]["attributes"]["comune"] = "Sant'Angelo"
    f = Filter(doc)
    a, b = f.mapping["Città di Castello"], f.mapping["Sant'Angelo"]
    assert f.outgoing("citta di castello") == a
    assert f.outgoing("Citta\u0300 di Castello") == a  # NFD
    assert f.outgoing("CITTÀ DI CASTELLO e Citta\u0300") == f"{a} e Citta\u0300"
    assert f.outgoing("sant\u2019angelo e Sant'Angelo") == f"{b} e {b}"
    assert f.outgoing("Dopo, citt\u00e0 di castello.") == f"Dopo, {a}."


def test_urls_unc_and_windows_paths_leak_nothing(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("file:///Users/enzo/Dropbox/x/a.tif") == "a"
    assert f.outgoing("file:///Users/enzo/Dropbox/Tell%20Sultan/a.tif") == "a"
    assert f.outgoing("postgresql://enzo:pw@10.0.0.5:5432/db fallita") == "db fallita"
    assert f.outgoing("https://example.org") == "indirizzo"
    assert f.outgoing("\\\\server\\share\\Tell Sultan\\a.jpg") == "a"
    assert f.outgoing("C:\\Dati\\Archivio\\foto") == "foto"
    assert f.outgoing("apri C:\\Dati\\Mario Rossi Archivio\\foto 12.jpg ora") == "apri foto_12 ora"
    # no extension, no spaces: the path ends at the first space (the names the filter knows go anyway)
    assert f.outgoing("C:\\Dati\\foto non esiste") == "foto non esiste"
    assert f.outgoing("/Users/enzo/Scavi/Rilievo") == "Rilievo"
    assert f.outgoing("~/Dropbox/tell/x") == "x"
    assert f.outgoing("missione_siria/Tell Sultan/foto/a.jpg") == "missione_siria/Sito A/foto/a.jpg"
    assert f.outgoing("e/o, 28/08 e 2026/10/29 e 1/0.5") == "e/o, 28/08 e 2026/10/29 e 1/0.5"


def test_coordinate_systems_in_text(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("CRS EPSG:32637 non valido; UTM zone 37N") == "CRS sistema proiettato non valido; sistema proiettato"
    assert f.outgoing("WGS 84 / UTM zone 37N") == "sistema proiettato"
    assert f.outgoing("+proj=utm +zone=37 +datum=WGS84") == "sistema proiettato"


def test_update_adds_names_and_keeps_the_placeholders(tmp_path):
    doc = make_document(tmp_path)
    f = Filter(doc)
    before = dict(f.mapping)
    assert f.update(doc) == 0
    doc.chunks[0].meta["drawings"]["units"].append(
        {"wkt": "POLYGON EMPTY", "sito": "Veio", "attributes": {"schedatore": "Anna Verdi"}})
    doc.add_chunk("Volo 29/08")
    assert f.update(doc) == 3
    assert {k: v for k, v in f.mapping.items() if k in before} == before
    assert f.mapping["Veio"] == "Sito B" and f.mapping["Anna Verdi"] == "Persona 2"
    assert f.mapping["Volo 29/08"] == "chunk 2"
    assert f.outgoing("Veio") == "Sito B" and f.incoming("Sito B") == "Veio"
    assert Filter.passthrough().update(doc) == 0


def test_facts_keys_that_collide_keep_both(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.facts({"Tell Sultan": 1, "Sito A": 2, "tell sultan": 3}) == {"Sito A": 1, "Sito A (2)": 2, "Sito A (3)": 3}


def test_facts_sets_bytes_and_objects(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.facts({"s": {"Lagoi"}, "b": b"Lagoi", "l": [b"x", "Lagoi"], "o": object.__new__(type("N", (), {
        "__str__": lambda self: "Lagoi"}))}) == {"s": ["Luogo 1"], "l": ["Luogo 1"], "o": "Luogo 1"}


def test_incoming_tolerates_case_and_whitespace_but_not_ordinary_text(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.incoming("SITO A, Sito  A, Sito\nA, sito A e SITO  A") == "Tell Sultan, Tell Sultan, Tell Sultan, Tell Sultan e Tell Sultan"
    assert f.incoming("il sito a nord, Sito a sud") == "il sito a nord, Sito a sud"
    assert f.incoming("LUOGO 1 e luogo 1, PERSONA 1, Chunk 1") == "Lagoi e Lagoi, Mario Rossi, Volo 28/08"
    assert f.incoming("Sito AND Luogo 7 Sito B") == "Sito AND Luogo 7 Sito B"


PROSE = ["km/h e m/s", "5 mm/px e 3 px/cm", "e/o oppure and/or", "omega/phi/kappa", "</figure>", "ANTENNA: DELTA H/E/N",
         "Vista/Pannelli e File/Esporta", "Sì/No e on/off", "TCP/IP e HTTP/HTTPS", "28/08 e 2026/10/29 e 1/0.5",
         "100% fatto, 50%25 e %41", "usa \\n per andare a capo", "(x / 2) e a / b", "il file a.jpg e b.png"]


def test_prose_is_never_altered(tmp_path):
    f = Filter(make_document(tmp_path))
    for line in PROSE:
        assert f.outgoing(line) == line, line


def test_paths_are_only_what_starts_with_an_anchor(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("ppg gnss ppk --mrk /card/F.MRK --rover /card/F.obs") == "ppg gnss ppk --mrk F --rover F"
    assert f.outgoing("apri /Users/a/b.jpg e /Users/c/d.jpg ora") == "apri b e d ora"
    assert f.outgoing("apri /Users/a/foto 12.jpg e basta") == "apri foto_12 e basta"
    assert f.outgoing("apri /Users/a/foo e poi guarda a.jpg") == "apri foo e poi guarda a.jpg"
    assert f.outgoing('src="/it/gui/x.png"') == 'src="x"'


def test_dot_rooted_and_variable_paths(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("./Archivio/foto/a.jpg") == "a"
    assert f.outgoing("apri ../Mario_Rossi/scavo/x.tif ora") == "apri x ora"
    assert f.outgoing("cd ../Mario_Rossi/scavo/") == "cd scavo"
    assert f.outgoing("\\Users\\enzo\\Mario Rossi\\a.jpg") == "a"
    assert f.outgoing("%USERPROFILE%\\Mario Rossi\\a.jpg") == "a"
    assert f.outgoing("%USERPROFILE%\\Scavi\\x") == "x"
    assert f.outgoing("la cartella ./Tell_Sultan") == "la cartella Sito A"


def test_public_urls_pass_and_others_are_reduced(tmp_path):
    f = Filter(make_document(tmp_path))
    guide = "https://enzococca.github.io/pyarchinit-photogrammetry/it/gui/drawing/"
    assert f.outgoing(f"vedi {guide} e basta") == f"vedi {guide} e basta"
    md = "[GitHub Release](https://github.com/enzococca/pyarchinit-photogrammetry/releases)"
    assert f.outgoing(md) == md
    assert f.outgoing("https://docs.github.com/a/b") == "https://docs.github.com/a/b"
    assert f.facts({"url": guide}) == {"url": guide}
    assert f.outgoing("https://example.com/Tell Sultan/x") == "https:/Sito A/x".replace("https:/", "https://example.com/") \
        or "example" not in f.outgoing("https://example.com/Tell%20Sultan/x")
    assert f.outgoing("https://example.com/Tell%20Sultan/x") == "x"
    assert f.outgoing("https://user@github.com/x/y") == "y"  # credentials: not public
    assert f.outgoing("https://notgithub.com/x/y") == "y"


def test_percent_is_decoded_only_inside_paths(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("100% fatto") == "100% fatto"
    assert f.outgoing("%41 e %20") == "%41 e %20"
    assert f.outgoing("/Users/x/Tell%20Sultan/a%20b.tif") == "a_b"


def test_only_coordinate_keys_are_dropped(tmp_path):
    f = Filter(make_document(tmp_path))
    kept = {"gps_rmse": 0.02, "position_accuracy_m": 1.5, "pixel_size_x": 0.01, "gnss_status": "fix", "x_resolution": 0.1,
            "camera_gps_error_m": 0.3, "reference_rmse": 0.01, "transform_scale": 1.0, "max": 4, "n_points": 120}
    assert f.facts(kept) == kept
    for key in ("z_min", "zMin", "quota_abs", "centerPoint", "x_max", "pointXyz", "lon_deg", "bounds"):
        assert f.facts({key: 312.4, "ok": 1}) == {"ok": 1}, key
    assert f.facts({"reference": [1.0, 2.0], "references": {"a": 3}, "ok": 1}) == {"ok": 1}
    assert f.facts({"reference": "Fotogrammetria"}) == {"reference": "Fotogrammetria"}


def test_ordinary_lines_of_the_guide_change_only_when_they_hold_a_path_or_url():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "src" / "pyarchinit_photogrammetry" / "assistant" / "guide").glob("*.it.md"))
    limit = 0.01
    if not files:  # the guide is not there yet: the installation docs, full of commands and paths, with a looser limit
        files, limit = sorted((root / "docs").glob("*.it.md")), 0.02
    assert files
    anchor = re.compile(r"(?:^|[^\w])(?:/|~/|\.\./|\./|[A-Za-z]:[\\/]|\\|%\w+%)|://|EPSG|UTM|\+proj=", re.IGNORECASE)
    f = Filter(None)  # no names: what changes here is the paths and the links
    lines = changed = 0
    examples = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            lines += 1
            out = f.outgoing(line)
            if out != line:
                changed += 1
                assert anchor.search(line), (path.name, line)
                if len(examples) < 10:
                    examples.append((path.name, line, out))
    print(f"{changed} of {lines} lines changed")
    for e in examples:
        print(e)
    assert changed < lines * limit
