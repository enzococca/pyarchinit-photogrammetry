import re

import numpy as np

from pyarchinit_photogrammetry.assistant.privacy import ABSOLUTE_KEYS, WARNING, Filter, _is_wkt
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
    assert f.outgoing("vedi https://example.org/a/b.tif.") == "vedi indirizzo."
    assert f.outgoing("vedi https://example.org/a/b.html.") == "vedi indirizzo."


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
    assert f.outgoing("TellSultanX e TellSultan_01") == "TellSultanX e Sito A_01"  # letters make another word


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
    assert f.outgoing("file:///Users/enzo/Dropbox/x/a.tif") == "indirizzo"
    assert f.outgoing("file:///Users/enzo/Dropbox/Tell%20Sultan/a.tif") == "indirizzo"
    assert f.outgoing("postgresql://enzo:pw@10.0.0.5:5432/db fallita") == "indirizzo fallita"
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
    assert f.outgoing("CRS EPSG:32637 non valido; UTM zone 37N") == "CRS sistema di coordinate non valido; sistema di coordinate"
    assert f.outgoing("WGS 84 / UTM zone 37N") == "sistema di coordinate"
    assert f.outgoing("+proj=utm +zone=37 +datum=WGS84") == "sistema di coordinate"


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
    assert f.outgoing("https://example.com/Tell%20Sultan/x") == "indirizzo"
    assert f.outgoing("https://user@github.com/x/y") == "indirizzo"  # credentials: not public
    assert f.outgoing("https://notgithub.com/x/y") == "indirizzo"


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


def test_keys_with_any_coordinate_token_are_dropped_unless_derived(tmp_path):
    f = Filter(make_document(tmp_path))
    xyz = [312345.2, 4512345.1, 100.2]
    for key in ("marker_xyz", "gcp_xyz", "camera_lat", "camera_lon", "camera_z", "ground_z", "top_z", "base_z", "target_xy",
                "camera_center", "site_centroid", "marker_coordinates", "camera_position", "camera_positions",
                "marker_position", "z_range", "markerXyz", "cameraLocation", "dem_min", "tie_point_z"):
        assert f.facts({key: xyz, "ok": 1}) == {"ok": 1}, key
    kept = {"gps_rmse": 0.02, "pixel_size_x": 0.01, "position_accuracy_m": 1.5, "gnss_status": "fix",
            "camera_z_error": 0.1, "marker_xyz_std": [1, 2, 3], "z_range_count": 4, "n_positions": 12,
            "dem_resolution": 0.07, "x_spread": 2.0}
    assert f.facts(kept) == kept


def test_real_paths_with_spaces_are_reduced_whole(tmp_path):
    f = Filter(make_document(tmp_path))
    cases = {
        "/Users/x/Anna Neri/Missione Siria/Timestamp.MRK": "Timestamp",
        "/Users/x/Anna Neri/Missione Siria/": "Missione_Siria",
        "/Volumes/Extreme Pro/missione siria/drone/tell": "tell",
        "C:\\Dati\\Sultan Scavi\\foto": "foto",
        "~/Documenti/Missione archeologica in Siria 2026 campagna autunno/a.jpg": "a",
        "/Users/x/Missione archeologica Siria Tell Sultan Nord/a.jpg": "a",
        "/Users/x/Anna Neri/rover 1.obs": "rover_1",
        "/Users/x/Anna Neri/db/pyarchinit.sqlite": "pyarchinit",
        "/Users/x/Anna Neri/Note di campo/rapporto.docx": "rapporto",
    }
    for text, name in cases.items():
        assert f.outgoing(text) == name, text
        assert f.outgoing(f"apri {text} ora") == f"apri {name} ora", text
        assert f.outgoing(f"apri {text}.") == f"apri {name}.", text
    assert f.facts({"p": "/Users/x/Anna Neri/Missione Siria/Timestamp.MRK"}) == {"p": "Timestamp"}


def test_prose_after_a_path_does_not_continue_it(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("apri /Users/a/b.jpg e /Users/c/d.jpg ora") == "apri b e d ora"
    assert f.outgoing("la cartella /Users/x/Scavo. Poi 12 km/h, e/o 5 mm/px") == "la cartella Scavo. Poi 12 km/h, e/o 5 mm/px"
    # privacy first (ruling T2-7): after a path, a word with a separator is a piece of it, whatever its length; the
    # prose in between goes with it
    assert f.outgoing("cd /Users/x/Scavo e omega/phi/kappa e Dati") == "cd kappa e Dati"
    # a date or a fraction stops the path like a unit (ruling T2-8)
    assert f.outgoing("cd /Users/x/Scavo e omega/phi/kappa e 12/08/2026") == "cd kappa e 12/08/2026"
    assert f.outgoing("cd /Users/x/Scavo e 12/08/2026") == "cd Scavo e 12/08/2026"
    assert f.outgoing("apri /Users/x/Dati Neri/rover del 2026/08/12 e Vista/Pannelli") == \
        "apri rover del 2026/08/12 e Vista/Pannelli"
    assert f.outgoing("apri /Users/x/Dati Neri/rover a 1/2 e Vista/Pannelli") == "apri rover a 1/2 e Vista/Pannelli"
    assert f.outgoing("apri /Users/a/b e poi usa Vista/Pannelli") == "apri Pannelli"
    # a short unit stops the path
    assert f.outgoing("apri /Users/x/Scavo Tell Halaf/rover 5 mm/px e poi Dati/x") == "apri rover 5 mm/px e poi Dati/x"
    for unit in ("km/h", "m/s", "e/o", "mm/px", "px/cm", "H/E/N", "and/or", "TCP/IP"):
        assert f.outgoing(f"apri /Users/x/Dati Neri/rover poi {unit} e Vista/Pannelli") == \
            f"apri rover poi {unit} e Vista/Pannelli", unit


def test_relative_markdown_links_are_kept(tmp_path):
    f = Filter(make_document(tmp_path))
    for link in ("[API](../api.md#x)", "[API](../api.md)", "[sezione](#x)", "[gui](./gui/)", "[su](../)",
                 "[plugin](../plugins/geoarchaeology.it.md#csv)"):
        assert f.outgoing(link) == link, link
    assert f.outgoing("![x](../img/gui/x.png)") == "![x](x)"
    assert f.outgoing("[x](/Users/a/b.md)") == "[x](b)"


def test_variable_anchors_and_bare_words(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("$HOME/Mario Rossi/a.jpg") == "a"
    assert f.outgoing("apri $PPG_DATA/Archivio/x.tif ora") == "apri x ora"
    assert f.outgoing("${HOME}/Archivio/x.tif") == "x"
    for line in ("tra 1/2 e /3", "apri /clear poi /help", "5 \u20ac/h e /ora", "3 m /s"):
        assert f.outgoing(line) == line, line
    assert f.outgoing("apri /dev/null") == "apri null"


def test_system_label_in_text_does_not_say_projected(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("--crs EPSG:4979") == "--crs sistema di coordinate"


def test_ordinary_lines_of_the_guide_change_only_when_they_hold_a_path_or_url():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    guide = sorted((root / "src" / "pyarchinit_photogrammetry" / "assistant" / "guide").glob("*.it.md"))
    # one limit of 2 % for the packaged guide and the docs alike (ruling T4-2): the guarantee is the check below that
    # every changed line holds a path, a link or EPSG/UTM text; the share only says the filter does not run wild
    sets = {"guide": (guide, 0.02)} if guide else {
        "docs it": (sorted((root / "docs").glob("*.it.md")), 0.02),
        "docs en": (sorted((root / "docs").glob("*.md")), 0.02),
        "docs gui": (sorted((root / "docs" / "gui").glob("*.it.md")), 0.02)}
    anchor = re.compile(r"(?:^|[^\w])(?:/|~/|\.\./|\./|[A-Za-z]:[\\/]|\\|%\w+%|\$\w+/)|://|EPSG|UTM|\+proj=", re.IGNORECASE)
    images = re.compile(r"!\[[^\]]*\]\([^)]*\)")
    f = Filter(None)  # no names: what changes here is the paths and the links
    for label, (files, limit) in sets.items():
        assert files, label
        lines = changed = 0
        examples = []
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                lines += 1
                out = f.outgoing(line)
                if images.sub("", out) != images.sub("", line):  # relative images of the guide: no private data, not counted
                    changed += 1
                    assert anchor.search(line), (path.name, line)
                    if len(examples) < 10:
                        examples.append((path.name, line, out))
        print(f"{label}: {changed} of {lines} lines changed")
        for e in examples:
            print(e)
        assert changed < lines * limit, label


def test_a_path_with_spaces_is_reduced_to_its_end(tmp_path):
    f = Filter(make_document(tmp_path))
    cases = {
        "/Users/x/Scavo Tell Halaf/Dati Neri/rover": "rover",
        "/Volumes/Extreme Pro/Tell Abu Ruwail/foto": "foto",
        "/Users/x/Dropbox/Scavo Tell Halaf/Dati Neri/rover": "rover",
        "$HOME/Scavo Tell Halaf/Dati Neri/rover": "rover",
        "\\\\server\\share\\Scavo Tell Halaf\\Dati Neri\\rover": "rover",
        "D:\\Missione Siria Nord\\Anna Neri\\foto": "foto",
        "~/Documents/Scavo Tell Halaf/foto": "foto",
        "/Users/x/Foto (2)/Tell Halaf/a.jpg": "a",
        "/Users/x/Sant'Angelo/foto": "foto",
        "/Users/x/Dott. Neri/foto": "foto",
    }
    for text, name in cases.items():
        assert f.outgoing(text) == name, text
        assert f.outgoing(f"apri {text} ora") == f"apri {name} ora", text
        assert f.outgoing(f"apri {text}.") == f"apri {name}.", text
        assert f.outgoing(f"apri {text}, poi") == f"apri {name}, poi", text
        for q in ('"', "'", "`"):
            assert f.outgoing(f"cd {q}{text}{q} e poi") == f"cd {q}{name}{q} e poi", (q, text)
        assert f.outgoing(f"({text}) e km/h") == f"({name}) e km/h", text
    # inside quotes the closing quote says where the path ends, also when its last folder holds spaces
    assert f.outgoing('cd "/Users/x/Anna Neri/Scavo Tell Halaf Nord" ok') == 'cd "Scavo_Tell_Halaf_Nord" ok'
    # a second path in the same line is a path of its own
    assert f.outgoing("[x](/Users/a/Tell Halaf/b) e [y](/Users/c/d)") == "[x](b) e [y](d)"
    for line in PROSE:  # no anchor: unchanged
        assert f.outgoing(line) == line, line


def test_a_mean_of_coordinates_is_a_coordinate(tmp_path):
    f = Filter(make_document(tmp_path))
    for key in ("x_mean", "lat_mean", "camera_z_mean", "easting_mean", "centroid_mean", "y_median", "mean_z", "gps_mean",
                "pos", "camera_pos", "coords", "marker_coord"):
        assert f.facts({key: 312345.2, "ok": 1}) == {"ok": 1}, key
    kept = {"x_spread": 2.0, "mean": 3.0, "median": 2.0, "gps_mean_error": 0.3, "pos_accuracy": 0.02, "n_coords": 4}
    assert f.facts(kept) == kept


def test_foreign_links_are_just_an_address(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("https://www.google.com/maps/@41.134,14.266,17z") == "indirizzo"
    assert f.outgoing("vedi https://www.openstreetmap.org/#map=17/41.134/14.266 ora") == "vedi indirizzo ora"
    assert f.outgoing("[Tell Sultan](https://example.org/x)") == "[Sito A](indirizzo)"
    assert f.outgoing("https://example.org/Tell_Sultan/foto.html") == "indirizzo"
    assert f.outgoing("https://example.org/dati/rilievo%201.tif") == "indirizzo"
    # a foreign link is always "indirizzo" (ruling T2-12): extended over "e poi TellSultan_01.jpg" (a known extension),
    # it gives no file name that would keep the pin
    out = f.outgoing("https://www.google.com/maps/@41.134,14.266,17z e poi TellSultan_01.jpg")
    assert out == "indirizzo"
    assert not re.search(r"\d", out)
    assert f.facts({"link": "https://www.google.com/maps/@41.134,14.266,17z"}) == {"link": "indirizzo"}


def test_invisible_characters_and_names_written_surname_first(tmp_path):
    f = Filter(make_document(tmp_path), people=["Anna Verdi", "Mario De Luca"])
    assert f.outgoing("Mario\u200bRossi") == "Persona 1"
    assert f.outgoing("Mario Ros\u200bsi e Mario\ufeff Rossi") == "Persona 1 e Persona 1"
    assert f.outgoing("Tell\u00adSultan e Tell Sul\u00adtan e La\u200cgoi") == "Sito A e Sito A e Luogo 1"
    assert f.outgoing("Rossi, Mario e Verdi,Anna e ROSSI , MARIO") == "Persona 1 e Persona 2 e Persona 1"
    assert f.outgoing("De Luca, Mario") == "Persona 3"
    assert f.outgoing("Tell, Sultan") == "Tell, Sultan"  # only people are written surname first
    assert f.incoming("Persona 1") == "Mario Rossi"


def test_email_addresses_stay_home(tmp_path):
    f = Filter(make_document(tmp_path), people=["Anna Verdi"])
    assert f.outgoing("scrivi a anna.verdi@example.org o a x+y@sub.uni.it.") == \
        "scrivi a indirizzo e-mail o a indirizzo e-mail."
    assert f.outgoing("mailto:mario.rossi@uni.it") == "mailto:indirizzo e-mail"
    assert f.facts({"contatto": "anna.verdi@example.org", "n": 1}) == {"contatto": "indirizzo e-mail", "n": 1}


PIN = "https://www.google.com/maps/place/41%C2%B008'02.4%22N+14%C2%B015'57.6%22E/@41.134,14.266,17z"


def test_foreign_links_with_spaces_or_apostrophes_are_reduced_whole(tmp_path):
    f = Filter(make_document(tmp_path))
    cases = {
        "file:///C:/Users/Mario Rossi/Scavo Tell Halaf/foto/a.jpg": "indirizzo",
        "file:///Users/x/Scavo Tell Halaf/foto/a.jpg": "indirizzo",
        "smb://nas/Scavo Tell Halaf/foto": "indirizzo",
        "file://C:\\Users\\x\\Scavo Tell Halaf\\foto": "indirizzo",
        "http://192.168.1.10/Scavo Tell Halaf/foto/a.jpg": "indirizzo",
        "https://example.org/Sant'Angelo/Tell%20Halaf/foto": "indirizzo",
        PIN: "indirizzo",
    }
    for text, name in cases.items():
        for line, expected in ((text, name), (f"vedi {text} ora", f"vedi {name} ora"), (f"vedi {text}.", f"vedi {name}."),
                               (f'cd "{text}" e poi', f'cd "{name}" e poi'), (f"({text}) e km/h", f"({name}) e km/h"),
                               (f"{text} a 5 mm/px e Vista/Pannelli", f"{name} a 5 mm/px e Vista/Pannelli")):
            out = f.outgoing(line)
            assert out == expected, line
    assert not re.search(r"\d", f.outgoing(PIN))  # no coordinate left
    # public links stay whole, and the text around them too
    assert f.outgoing("vedi https://github.com/a/b e /Users/x/c.jpg") == "vedi https://github.com/a/b e c"
    # a public link ends before a quote: the path after it is still reduced (ruling T2-11)
    assert f.outgoing("['https://github.com/a','/Users/x/Scavo Tell Halaf/foto/a.jpg']") == "['https://github.com/a','a']"
    assert f.outgoing("VALUES ('https://github.com/x','/Users/x/Anna Neri/a.jpg')") == "VALUES ('https://github.com/x','a')"
    assert f.outgoing("vedi https://github.com/a'/Users/x/Anna Neri/a.jpg") == "vedi https://github.com/a'a"


def test_names_glued_without_gaps(tmp_path):
    f = Filter(make_document(tmp_path), people=["Anna Verdi"])
    assert f.outgoing("TellSultan e tellsultan") == "Sito A e Sito A"
    assert f.outgoing("/Users/x/foto/TellSultan_001.jpg") == "Sito A_001"
    assert f.outgoing("2026TellSultan_01") == "2026Sito A_01"
    assert f.outgoing("MarioRossi e AnnaVerdi") == "Persona 1 e Persona 2"
    assert f.outgoing("/Users/x/mariorossi_scheda.pdf") == "Persona 1_scheda"
    assert f.outgoing("mariorossi_scheda.pdf") == "Persona 1_scheda.pdf"
    assert f.outgoing("TellSultanX e xTellSultan") == "TellSultanX e xTellSultan"  # a letter on a side: another word
    g = Filter(None, people=["Li Wu"], places=["Ai Bo"])  # glued forms shorter than 6 letters are not used
    assert g.outgoing("LiWu e AiBo, Li Wu e Ai Bo") == "LiWu e AiBo, Persona 1 e Luogo 1"


def test_unc_like_and_user_home_anchors(tmp_path):
    f = Filter(make_document(tmp_path))
    assert f.outgoing("//nas/Scavo Tell Halaf/foto") == "foto"
    assert f.outgoing("apri //nas/share/a.jpg ora") == "apri a ora"
    assert f.outgoing("apri ~enzo/Scavo/foto ora") == "apri foto ora"
    assert f.outgoing("~mario.rossi/Scavo Tell Halaf/foto") == "foto"
    for line in ("// commento", "a // b", "~ 5 m", "circa ~3/4"):
        assert f.outgoing(line) == line, line


def test_is_wkt_takes_a_geometry_and_not_a_word_that_starts_like_one():
    # ruling T4-1: a WKT geometry is the word, optionally Z/M/ZM, then "(" or EMPTY
    for wkt in ("POINT (1 2)", "POINT(1 2)", "POINT Z (1 2 3)", "POINT ZM(1 2 3 4)", "MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)))",
                "SRID=4326;POINT(1 2)", "POINT EMPTY", "GEOMETRYCOLLECTION (POINT (1 2))", "GEOMETRYCOLLECTION EMPTY",
                " polygon ((0 0, 1 0, 1 1, 0 0))", "LINESTRING (0 0, 1 1)", "CURVEPOLYGON ((0 0, 1 0, 1 1, 0 0))",
                "MULTISURFACE (((0 0, 1 0, 1 1, 0 0)))"):
        assert _is_wkt(wkt), wkt
    for text in ("point_cloud", "polygons", "surface", "pointer", "Polygon of the trench", "Curve di livello e volumi",
                 "tie points (sparse)", 12):
        assert not _is_wkt(text), text
    f = Filter(None)
    assert f.facts({"source": "point_cloud", "title": "Curve di livello e volumi", "wkt": "POINT (1 2)"}) == {
        "source": "point_cloud", "title": "Curve di livello e volumi"}


def test_names_in_the_drawings_of_the_elevations_leave_as_placeholders(tmp_path):
    """A person, a place or a site named only in the drawings made on an elevation or a section (``frames``) is
    replaced as those of the plan are."""
    doc = make_document(tmp_path)
    doc.chunks[0].meta["drawings"]["frames"] = {"ab12cd34": {"units": [
        {"wkt": "POLYGON EMPTY", "sito": "Tell Sultan", "us": "5", "tipo": "USM",
         "attributes": {"disegnatore": "Giulia Bianchi", "comune": "Ariano"}}], "quote": []}}
    f = Filter(doc)
    out = f.outgoing("Prospetto di Giulia Bianchi ad Ariano")
    assert "Giulia" not in out and "Ariano" not in out
    assert f.incoming(out) == "Prospetto di Giulia Bianchi ad Ariano"
