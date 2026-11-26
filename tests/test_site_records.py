"""The unit records of a published web site (io/site_records.py): which columns of ``us_table`` a site publishes,
and how their values are written. No database and no real data: the bundled schema and values written here."""
from __future__ import annotations


def test_every_column_of_the_bundled_schema_in_exactly_one_set():
    from pyarchinit_photogrammetry.io import site_records as R
    from pyarchinit_photogrammetry.pyarchinit.bridge import reference_us_columns
    schema = {n for n, _ in reference_us_columns()} | {"id_us"}
    pub, people, never = set(R.published_columns()), set(R.PEOPLE_COLUMNS), set(R.NEVER_COLUMNS)
    assert (len(pub), len(people), len(never)) == (101, 3, 27)
    assert pub | people | never == schema and not (pub & people or pub & never or people & never)
    assert set(R.published_columns(people=True)) == pub | people
    assert all(set(R.COLUMN_TITLES[c]) == {"it", "en"} for c in pub - {"rapporti"} | people)


def test_list_text():
    from pyarchinit_photogrammetry.io.site_records import list_text
    assert list_text("[['argilla', 'frequente'], ['carbone', 'raro']]") == "argilla frequente; carbone raro"
    assert list_text("[]") == "" and list_text("terra bruna") is None and list_text("[1, 2") is None


def test_absolute_heights_follow_the_frame():
    from pyarchinit_photogrammetry.io.site_records import height_value as h
    kw = dict(dz=240.0, usm_heights="none")
    assert h("quota_abs", "245,30", frame="local", fixed=True, **kw) == 5.3
    assert h("quota_abs", 245.3, frame="local", fixed=False, **kw) is None      # nothing fixes dz
    assert h("quota_abs", "245,30 m s.l.m.", frame="local", fixed=True, **kw) is None
    assert h("quota_abs", "245,30 m s.l.m.", frame="crs", fixed=True, **kw) == "245,30 m s.l.m."
    assert h("quota_relativa", -1.25, frame="local", fixed=False, **kw) == -1.25


def test_heights_of_masonry_units_by_the_choice():
    from pyarchinit_photogrammetry.io.site_records import height_value as h
    v = 245.3
    assert h("quota_min_usm", v, frame="local", dz=240.0, fixed=True, usm_heights="none") is None
    assert h("quota_min_usm", v, frame="local", dz=240.0, fixed=True, usm_heights="relative") == 245.3
    assert h("quota_min_usm", v, frame="local", dz=240.0, fixed=True, usm_heights="absolute") == 5.3
    assert h("quota_min_usm", v, frame="local", dz=0.0, fixed=False, usm_heights="absolute") is None
    assert h("quota_max_usm", v, frame="crs", dz=0.0, fixed=True, usm_heights="none") == 245.3


def test_field_values():
    from decimal import Decimal

    from pyarchinit_photogrammetry.io.site_records import field_value as f
    scrub = str.upper
    assert [f("descrizione", v, scrub) for v in (None, "", "  ", "[]", "[[]]")] == [None] * 5
    assert f("inclusi", "[['argilla', 'frequente']]", scrub) == "ARGILLA FREQUENTE"
    assert f("descrizione", "terra bruna", scrub) == "TERRA BRUNA"
    assert f("anno_scavo", 2024, scrub) == 2024 and f("lunghezza_max", Decimal("1.25"), scrub) == 1.25


ROWS = [dict(sito="A", area="1", us="12", unita_tipo="US", d_stratigrafica="Strato"),
        dict(sito="A", area="1", us=" 13 ", unita_tipo="USM"), dict(sito="A", area="2", us="14", unita_tipo="US"),
        dict(sito="A", area="2", us="14", unita_tipo="USM"), dict(sito="B", area="1", us="12", unita_tipo="US"),
        dict(sito="A", area="3", us="20", unita_tipo="US"), dict(sito="A", area="4", us="20", unita_tipo="US")]


def test_matching_steps():
    from pyarchinit_photogrammetry.io.site_records import match_unit
    assert match_unit(ROWS, "A", "1", "US", 12).row is ROWS[0]                       # integer number
    f = match_unit(ROWS, "A", "1", "US", "13")                                        # type not set on the polygon
    assert (f.row, f.step, f.ignored) == (ROWS[1], 2, ("type",))
    f = match_unit(ROWS, "A", None, "USM", "14")
    assert (f.row, f.step) == (ROWS[3], 3)
    assert match_unit(ROWS, "A", "2", "USR", "14").reason == "several rows"           # never a guess
    assert match_unit(ROWS, "A", None, "US", "20").reason == "several rows"
    assert match_unit(ROWS, "A", "1", "US", "99").reason == "no row"
    assert match_unit(ROWS, "A", "1", "US", " ").reason == "no number"


def test_a_unit_without_a_site_never_matches_another_sites_unit():
    from pyarchinit_photogrammetry.io.site_records import match_unit
    assert match_unit(ROWS, None, "1", "US", "12").reason == "no site"
    assert match_unit(ROWS, "", "1", "US", "12").reason == "no site"
    assert match_unit(ROWS, "B", "1", "US", "12").row is ROWS[4]


def test_matching_details():
    from pyarchinit_photogrammetry.io.site_records import Found, match_unit, unit_text
    assert (unit_text(None), unit_text(" 12 "), unit_text(12)) == ("", "12", "12")
    f = match_unit(ROWS, "A", "1", None, "12")                                         # no type: "US"
    assert (f.row, f.step, f.ignored, f.reason) == (ROWS[0], 1, (), "")
    assert match_unit(ROWS, "A", "1", "us", "12").step == 1                            # case of the type
    f = match_unit(ROWS, "A", "", "USR", "13")                                         # no area, no such type
    assert (f.row, f.step, f.ignored) == (ROWS[1], 3, ("area", "type"))
    f = match_unit(ROWS, "A", None, "USM", "13")
    assert (f.row, f.step, f.ignored) == (ROWS[1], 3, ("area",))
    assert match_unit(ROWS, "A", "9", "US", "12") == Found(None, 0, "no row", ())     # an area never widens
    assert match_unit(ROWS, None, "1", "US", None).reason == "no site"                    # the site first


def test_names_of_people_only_when_asked(tmp_path):
    """The box of the names of people: off (the default) no feature, table or plugin contribution of the site holds
    one; on, the names of people pass - in the drawings and in what a plugin gives - and the accounts of who wrote
    a record never do. ``site.json`` says which (``people``)."""
    import json

    from test_site import ACCOUNTS, PEOPLE, _document_with_people, _people_in

    from pyarchinit_photogrammetry.io import site_plugins

    def provider(chunk):
        return [{"kind": "layer", "id": "persone", "title": "Unita'",
                 "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1001.0, 5001.0]},
                               "properties": {"us": "1", "disegnatore": PEOPLE["disegnatore"],
                                              "created_by": ACCOUNTS["created_by"]}}]}]

    doc = _document_with_people(tmp_path)
    entry = site_plugins.register(provider, owner="prova-persone")
    try:
        closed = doc.export_site(tmp_path / "closed").parent
        opened = doc.export_site(tmp_path / "opened", people=True).parent
    finally:
        site_plugins.unregister(entry)

    def plugin_layer(site):
        return next(site.rglob("prova-persone/persone.geojson")).read_text(encoding="utf-8")

    assert _people_in(closed) == []
    assert json.loads((closed / "site.json").read_text(encoding="utf-8"))["people"] is False
    assert PEOPLE["disegnatore"] not in plugin_layer(closed)

    assert json.loads((opened / "site.json").read_text(encoding="utf-8"))["people"] is True
    text = "".join(f.read_text(encoding="utf-8") for f in opened.rglob("*") if f.suffix in (".json", ".geojson"))
    for field in ("disegnatore", "schedatore", "responsabile", "draa", "direttore_us"):
        assert PEOPLE[field] in text, field
    every = b"".join(f.read_bytes() for f in opened.rglob("*") if f.is_file())
    assert all(name.encode() not in every for name in ACCOUNTS.values())
    assert PEOPLE["disegnatore"] in plugin_layer(opened)
    assert ACCOUNTS["created_by"] not in plugin_layer(opened)


# --- the records in the site (records.json, "record" on the features) ---

SITE = "Sito di prova"
SECRET_ID = 918273  # a value of id_us no coordinate of the site holds


def _unit_rows(**extra) -> list[dict]:
    """Rows of ``us_table``: US 12 with every published column filled (and the columns a site never publishes),
    US 14 plain, US 15 twice (no record: never a guess)."""
    from pathlib import Path

    from pyarchinit_photogrammetry.io import site_records as R

    full = {c: f"valore {c}" for c in R.published_columns(people=True)}
    full.update({c: 1.5 for c in R.ABSOLUTE_HEIGHTS + R.RELATIVE_HEIGHTS + R.USM_HEIGHT_COLUMNS})
    full.update({"sito": SITE, "area": "1", "us": "12", "unita_tipo": "US", "d_stratigrafica": "Strato",
                 "descrizione": str(Path.home() / "x.txt"),
                 "rapporti": f"[['Copre', '14', '1', '{SITE}'], ['Taglia', '99', '1', '{SITE}']]",
                 "quota_abs": 245.30, "quota_min_usm": 244.0, "schedatore": "Mario Schedatore",
                 "editing_by": "account-editing-918", "id_us": SECRET_ID, "entity_uuid": "uuid-918273-abcdef",
                 "colonna_nuova": "valore-nuovo-918"})
    full.update(extra)
    plain = {"sito": SITE, "area": "1", "us": "14", "unita_tipo": "US"}
    twice = [{"sito": SITE, "area": "1", "us": "15", "unita_tipo": "US", "descrizione": n} for n in ("a", "b")]
    return [full, plain, *twice]


def _units_document(tmp_path, spot: bool = True):
    """A project with one chunk in EPSG:32633 and three unit polygons: US 12 of the site, US 14 with no site (the
    project's), US 15; a spot height at 245.0 when ``spot``."""
    from shapely.geometry import box

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    doc = ppg.Document()
    doc.meta["sito"] = SITE
    ch = doc.add_chunk("Saggio 1")
    ch.crs = "EPSG:32633"
    units = [UnitPolygon(box(1000.0, 5000.0, 1002.0, 5002.0), us="12", area="1", sito=SITE),
             UnitPolygon(box(1003.0, 5000.0, 1005.0, 5002.0), us="14", area="1", sito=None),
             UnitPolygon(box(1006.0, 5000.0, 1008.0, 5002.0), us="15", area="1", sito=SITE)]
    ch.meta["drawings"] = {"units": [u.to_dict() for u in units]}
    if spot:
        ch.meta["drawings"]["quote"] = [{"x": 1001.0, "y": 5001.0, "quota_q": 245.0, "us_q": "12", "area_q": "1"}]
    tmp_path.mkdir(parents=True, exist_ok=True)
    doc.save(tmp_path / "progetto.ppg")
    return doc


def _build(doc, folder, **kw):
    import json

    site = doc.export_site(folder, products=("units", "drawings"), **kw).parent
    manifest = json.loads((site / "site.json").read_text(encoding="utf-8"))
    units = json.loads((site / "data" / "units.geojson").read_text(encoding="utf-8"))["features"]
    path = site / "data" / "records.json"
    records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return site, manifest, {f["properties"]["us"]: f["properties"] for f in units}, records


def _fields(record) -> dict:
    return {c: v for g in record["groups"] for c, v in g["fields"]}


def test_a_click_gives_the_record_of_its_unit(tmp_path):
    _site, manifest, units, records = _build(_units_document(tmp_path), tmp_path / "site",
                                              unit_records=_unit_rows())
    k12, k14 = units["12"]["record"], units["14"]["record"]
    assert "record" not in units["15"] and k12 != k14
    assert set(records) == {k12, k14}
    assert records[k12]["title"] == "US 12 - Strato" and records[k14]["title"] == "US 14"
    assert records[k12]["relations"] == [["Copre", "14", "1", k14], ["Taglia", "99", "1", None]]
    assert manifest["chunks"][0]["records"] == {"url": "data/records.json", "count": 2}
    assert "people" not in manifest["record_groups"] and "heights" in manifest["record_groups"]
    assert set(manifest["fields"]["d_stratigrafica"]) == {"it", "en"} and "label" in manifest["fields"]
    assert any(n == "Saggio 1: records of 2 of 3 units" for n in manifest["build"]["notes"])


def test_never_a_column_of_the_never_set_and_no_unknown_column(tmp_path):
    site, _manifest, units, records = _build(_units_document(tmp_path), tmp_path / "site",
                                             unit_records=_unit_rows())
    every = b"".join(f.read_bytes() for f in site.rglob("*") if f.is_file())
    for word in ("id_us", "entity_uuid", "editing_by", "colonna_nuova", str(SECRET_ID), "uuid-918273",
                 "account-editing-918", "valore-nuovo-918"):
        assert word.encode() not in every, word
    assert _fields(records[units["12"]["record"]])["descrizione"] == "x.txt"


def test_record_heights_lowered_like_the_dem(tmp_path):
    import pytest

    rows = _unit_rows()

    def fields(name, doc, **kw):
        _s, _m, units, records = _build(doc, tmp_path / name, unit_records=rows, **kw)
        return _fields(records[units["12"]["record"]])

    doc = _units_document(tmp_path)
    local = fields("local", doc)
    assert local["quota_abs"] == round(245.30 - 245.0, 3) and "quota_min_usm" not in local
    assert fields("absolute", doc, usm_heights="absolute")["quota_min_usm"] == round(244.0 - 245.0, 3)
    assert fields("relative", doc, usm_heights="relative")["quota_min_usm"] == 244.0
    assert fields("crs", doc, frame="crs")["quota_abs"] == 245.3
    assert "quota_abs" not in fields("loose", _units_document(tmp_path / "b", spot=False))
    with pytest.raises(ValueError):
        doc.export_site(tmp_path / "bad", products=("units",), unit_records=rows, usm_heights="x")


def test_people_of_a_record_only_with_the_box(tmp_path):
    doc = _units_document(tmp_path)
    _s, closed, units, records = _build(doc, tmp_path / "closed", unit_records=_unit_rows())
    assert "schedatore" not in _fields(records[units["12"]["record"]])
    _s, opened, units, records = _build(doc, tmp_path / "opened", unit_records=_unit_rows(), people=True)
    assert _fields(records[units["12"]["record"]])["schedatore"] == "Mario Schedatore"
    assert "people" in opened["record_groups"] and "schedatore" in opened["fields"]


def test_relations_from_plain_text(tmp_path):
    rows = _unit_rows(rapporti="copre 14; tagliato da 7")
    _s, _m, units, records = _build(_units_document(tmp_path), tmp_path / "site", unit_records=rows)
    relations = records[units["12"]["record"]]["relations"]
    assert len(relations) == 2
    assert relations[0][0] == "Copre" and relations[0][1] == "14" and relations[0][3] == units["14"]["record"]
    assert relations[1][:2] == ["Tagliato da", "7"] and relations[1][3] is None
    broken = _unit_rows(rapporti="[['Copre', ")  # neither a list nor words: no relation, no crash
    _s, _m, units, records = _build(_units_document(tmp_path / "b"), tmp_path / "broken", unit_records=broken)
    assert records[units["12"]["record"]]["relations"] == []


def test_an_attribute_named_record_never_opens_a_record(tmp_path):
    """``record`` is the build's key: a spot height, a unit or a plugin's feature with an attribute of that name
    carries no ``record`` (no record of another unit opened under it), with or without records."""
    import json

    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.io import site_plugins

    doc = _units_document(tmp_path)
    ch = doc.chunks[0]
    ch.meta["drawings"]["quote"][0]["record"] = "u1"
    units = ch.meta["drawings"]["units"]
    units[2] = UnitPolygon(box(1006.0, 5000.0, 1008.0, 5002.0), us="15", area="1", sito=SITE,
                           attributes={"record": "u1"}).to_dict()

    def provider(chunk):
        return [{"kind": "layer", "id": "propria", "title": "Propria",
                 "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1001.0, 5001.0]},
                               "properties": {"us": "1", "record": "u1"}}]}]

    entry = site_plugins.register(provider, owner="prova-record")
    try:
        for name, rows in (("with", _unit_rows()), ("without", None)):
            site = doc.export_site(tmp_path / name, products=("units", "drawings"), unit_records=rows).parent
            drawings = json.loads((site / "data" / "drawings.geojson").read_text(encoding="utf-8"))["features"]
            spot = next(f["properties"] for f in drawings if f["properties"].get("layer") == "Quote")
            assert "record" not in spot, name
            plugin = json.loads(next(site.rglob("prova-record/propria.geojson")).read_text(encoding="utf-8"))
            assert "record" not in plugin["features"][0]["properties"], name
            plan = json.loads((site / "data" / "units.geojson").read_text(encoding="utf-8"))["features"]
            assert "record" not in next(f["properties"] for f in plan if f["properties"]["us"] == "15"), name
            if rows:  # the build's own keys are still there
                assert next(f["properties"] for f in plan if f["properties"]["us"] == "12")["record"]
    finally:
        site_plugins.unregister(entry)


def test_built_again_without_records(tmp_path):
    doc = _units_document(tmp_path)
    folder = tmp_path / "site"
    _build(doc, folder, unit_records=_unit_rows())
    site, manifest, units, records = _build(doc, folder)
    assert records is None and not (site / "data" / "records.json").exists()
    assert b'"record"' not in (site / "data" / "units.geojson").read_bytes()
    assert "records" not in manifest["chunks"][0] and "record_groups" not in manifest
    assert not any("records of" in n for n in manifest["build"]["notes"])


def test_volume_unit():
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.io.site_records import volume_unit

    polys = [UnitPolygon(box(0, 0, 1, 1), us="12", area="1", tipo="US", sito="Altro sito"),
             UnitPolygon(box(0, 0, 1, 1), us="14", area="2", tipo="USM", sito=None)]
    assert volume_unit({"us": "12", "tipo": "US", "sito": "Primo"}, polys, SITE) == ("Altro sito", "1", "US", "12")
    assert volume_unit({"us": " 14 ", "tipo": "USM", "area": "2"}, polys, SITE) == (SITE, "2", "USM", "14")
    assert volume_unit({"us": "14", "tipo": "USM", "area": "3", "sito": "Primo"}, polys, SITE) == \
        (SITE, "3", "USM", "14")  # another area: no polygon, the project's site and never the entry's
    assert volume_unit({"us": "", "tipo": "US"}, polys, SITE) is None
    assert volume_unit({"us": "16", "tipo": "US", "sito": "Primo"}, polys, None) is None


def test_an_elevation_outline_carries_its_record(processed, tmp_path):
    import json
    import shutil

    import numpy as np
    from test_site import _with_drawings
    from test_site_more import _section

    doc, ch, _facts = _with_drawings(processed, tmp_path / "progetto")
    shutil.copytree(processed[0].path.parent / "photos", tmp_path / "progetto" / "photos")
    _section(ch, np.array([2480000.0, 4500000.0, 100.0]))
    doc.meta["sito"] = SITE
    doc.save()
    rows = [{"sito": SITE, "area": "3", "us": "7", "unita_tipo": "US", "d_stratigrafica": "Riempimento"}]
    site = doc.export_site(tmp_path / "site", products=("sections",), unit_records=rows).parent
    feats = json.loads((site / "data" / "sections" / "1" / "drawing.geojson").read_text(encoding="utf-8"))["features"]
    outline = next(f["properties"] for f in feats if f["properties"]["layer"] == "US")
    records = json.loads((site / "data" / "records.json").read_text(encoding="utf-8"))
    assert records[outline["record"]]["title"] == "US 7 - Riempimento"


def test_an_elevation_outline_opens_its_record(processed, tmp_path):
    """On the tab of the sections a click on the outline of US 7 drawn on the section opens its record."""
    import json
    import shutil

    import numpy as np
    import pytest
    from test_site import Browser, _serving, _with_drawings, find_browser
    from test_site_more import _section

    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    doc, ch, _facts = _with_drawings(processed, tmp_path / "progetto")
    shutil.copytree(processed[0].path.parent / "photos", tmp_path / "progetto" / "photos")
    _section(ch, np.array([2480000.0, 4500000.0, 100.0]))
    doc.meta["sito"] = SITE
    doc.save()
    rows = [{"sito": SITE, "area": "3", "us": "7", "unita_tipo": "US", "d_stratigrafica": "Riempimento"}]
    site = doc.export_site(tmp_path / "site", products=("sections",), unit_records=rows).parent
    feats = json.loads((site / "data" / "sections" / "1" / "drawing.geojson").read_text(encoding="utf-8"))["features"]
    outline = next(f for f in feats if f["properties"]["layer"] == "US")
    ring = outline["geometry"]["coordinates"][0]
    edge = [(ring[0][0] + ring[1][0]) / 2, (ring[0][1] + ring[1][1]) / 2]  # on the outline itself
    with _serving(site) as port:
        origin = f"http://127.0.0.1:{port}"
        browser = Browser(executable, tmp_path / "profilo")
        try:
            browser.call("Page.navigate", {"url": origin + "/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            browser.js("ppgSite.setTab('sections')")
            browser.wait("document.documentElement.dataset.section === '1'", 30)
            box = browser.js("(() => { const r = document.getElementById('section-map').getBoundingClientRect();"
                             " return [r.left, r.top]; })()")
            px = browser.js(f"ppgSite.state.sectionMap.getPixelFromCoordinate([{edge[0]!r}, {edge[1]!r}])")
            browser.click(box[0] + px[0], box[1] + px[1])
            browser.wait("document.querySelector('#info .record') !== null", 10)
            assert "US 7 - Riempimento" in browser.js("document.getElementById('info').innerText")
            assert not browser.errors, browser.errors
        finally:
            browser.close()


# --- the rows of the sites to read and the report of the matches ---

def test_rows_of_the_sites_only(pa_db):
    """``us_rows`` reads ``us_table`` site by site: an empty or missing site is never a query (the database layer
    drops a None filter, and a query with no site would read every site)."""
    from pyarchinit_photogrammetry.pyarchinit.bridge import PyArchInit

    pa = PyArchInit.connect()
    rows = pa.us_rows(["Saggio", "", None, " Saggio "])
    assert sorted(str(r["us"]) for r in rows) == ["1", "2", "3"]
    assert all(r["sito"] == "Saggio" for r in rows)
    assert pa.us_rows([]) == []
    assert pa.us_rows([None, "", "  "]) == []
    assert pa.us_rows(["Nessun sito"]) == []


def test_unit_sites_and_report(tmp_path):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.io import site_records as R

    doc = _units_document(tmp_path)
    assert R.unit_sites(doc) == [SITE]
    ch = doc.chunks[0]
    ch.meta["drawings"]["units"] = [
        UnitPolygon(box(0, 0, 1, 1), us="12", area="1", sito="A").to_dict(),
        UnitPolygon(box(2, 0, 3, 1), us="14", area="1", sito=None).to_dict()]
    doc.meta["sito"] = "B"
    assert R.unit_sites(doc) == ["A", "B"]
    assert R.unit_sites(doc, [ch]) == ["A", "B"]
    doc.meta["sito"] = None
    assert R.unit_sites(doc) == ["A"]  # a unit with no site and no project site: no site to read
    ch.meta["drawings"]["units"] = []
    assert R.unit_sites(doc) == []
    ch.meta["proxies"] = [{"name": "US 12", "us": "12", "tipo": "US"}]
    assert R.unit_sites(doc) == []
    doc.meta["sito"] = "B"
    assert R.unit_sites(doc) == ["B"]  # a volume takes the site of the project

    doc = _units_document(tmp_path / "report")
    report = R.match_report(doc, doc.chunks, _unit_rows())
    assert [(r["chunk"], r["label"], bool(r["step"])) for r in report] == [
        ("Saggio 1", "US 12", True), ("Saggio 1", "US 14", True), ("Saggio 1", "US 15", False)]
    assert R.summary(report) == (2, 3)
    assert R.summary([]) == (0, 0)
    assert R.summary(R.match_report(doc, None, [])) == (0, 3)
    # as the build matches them: the polygons only with the units product
    assert R.match_report(doc, None, _unit_rows(), products=("dem", "proxies", "sections")) == []
    assert R.summary(R.match_report(doc, None, _unit_rows(), products=("units",))) == (2, 3)


def test_record_in_the_page(tmp_path):
    """A click on US 12 on the plan opens its record in the panel (groups by their titles, the markup of a
    description as text); a relation toward a unit of the site is a button that selects it, one toward a unit not
    in the site is text; the group titles follow the language."""
    import json
    import time

    import pytest
    from test_site import Browser, _serving, find_browser

    executable = find_browser()
    if executable is None:
        pytest.skip("no Chromium found (PPG_BROWSER names one)")
    markup = "<img src=x onerror=alert(1)>"
    site, _manifest, units, _records = _build(_units_document(tmp_path), tmp_path / "site",
                                              unit_records=_unit_rows(descrizione=markup))
    k14 = units["14"]["record"]
    feature = next(f for f in json.loads((site / "data/units.geojson").read_text(encoding="utf-8"))["features"]
                   if f["properties"]["us"] == "12")
    ring = feature["geometry"]["coordinates"][0][:-1]
    ux, uy = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    with _serving(site) as port:
        origin = f"http://127.0.0.1:{port}"
        browser = Browser(executable, tmp_path / "profilo")
        try:
            browser.call("Page.navigate", {"url": origin + "/"})
            browser.wait("['ready', 'error'].includes(document.documentElement.dataset.state)", 60)
            assert browser.js("document.documentElement.dataset.state") == "ready", browser.errors
            box = browser.js("(() => { const r = document.getElementById('map').getBoundingClientRect();"
                             " return [r.left, r.top]; })()")
            px = browser.js(f"ppgSite.map.getPixelFromCoordinate([{ux!r}, {uy!r}])")
            browser.click(box[0] + px[0], box[1] + px[1])
            browser.wait("document.querySelector('#info .record') !== null", 10)
            info = browser.js("document.getElementById('info').innerText")
            assert "US 12 - Strato" in info and "Definizione" in info
            assert markup in info and browser.js("document.querySelectorAll('#info img').length") == 0
            buttons = browser.js("[...document.querySelectorAll('#info .record button')].map((b) => b.textContent)")
            assert any("Copre" in b and "14" in b for b in buttons), buttons
            assert "Taglia 99" in info and not any("Taglia" in b for b in buttons), buttons

            browser.js("document.getElementById('language').click()")
            info = browser.js("document.getElementById('info').innerText")
            assert "Definition" in info and "Definizione" not in info

            browser.js("[...document.querySelectorAll('#info .record button')]"
                       ".find((b) => b.textContent.includes('Copre')).click()")
            browser.wait(f"document.documentElement.dataset.highlight === {k14!r}", 10)
            info = browser.js("document.getElementById('info').innerText")
            assert "US 14" in info and "US 12 - Strato" not in info
            assert browser.js("ppgSite.state.highlight.getSource().getFeatures().length") == 1

            # a click on the plan again: the unit selected before is no longer drawn over it
            browser.js("document.getElementById('fit').click()")  # the map went to US 14: the whole plan again
            time.sleep(0.5)
            px = browser.js(f"ppgSite.map.getPixelFromCoordinate([{ux!r}, {uy!r}])")
            browser.click(box[0] + px[0], box[1] + px[1])
            browser.wait("document.getElementById('info').innerText.includes('US 12 - Strato')", 10)
            assert browser.js("ppgSite.state.highlight.getSource().getFeatures().length") == 0
            assert browser.js("document.documentElement.dataset.highlight") is None
            assert not browser.errors, browser.errors
            outside = [u for u in browser.requests if not u.startswith((origin, "about:", "blob:", "data:"))]
            assert not outside, outside
        finally:
            browser.close()


def test_web_build_with_pyarchinit(pa_db, tmp_path):
    """``ppg web build --pyarchinit``: the rows of the sites of the units are read from the database (``--url``,
    else the one of PyArchInit's configuration) and published as the records; the match report goes to the output
    (a unit without a record with its reason), never a name of a person; without the flag no record."""
    import json

    from shapely.geometry import box
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.cli import app

    doc = ppg.Document()
    doc.meta["sito"] = "Saggio"
    ch = doc.add_chunk("Saggio 1")
    ch.crs = "EPSG:32633"
    ch.meta["drawings"] = {"units": [UnitPolygon(box(1000.0 + 3 * i, 5000.0, 1002.0 + 3 * i, 5002.0), us=str(i + 1),
                                                 area="1", sito="Saggio").to_dict() for i in range(3)]}
    project = tmp_path / "progetto.ppg"
    doc.save(project)
    run = CliRunner()
    base = ["web", "build", str(project)]
    units = ["--product", "units", "--product", "drawings"]

    r = run.invoke(app, base + [str(tmp_path / "site"), *units, "--pyarchinit", "--url", str(pa_db)])
    assert r.exit_code == 0, r.output
    assert "records: 3 of 3 units" in r.output
    site = tmp_path / "site"
    records = json.loads((site / "data" / "records.json").read_text(encoding="utf-8"))
    assert sorted(r["title"].split(" - ")[0] for r in records.values()) == ["US 1", "US 2", "US 3"]
    assert json.loads((site / "site.json").read_text(encoding="utf-8"))["people"] is False

    # the database of the configuration; a unit of no row said with its reason; heights and people passed
    ch.meta["drawings"]["units"].append(UnitPolygon(box(1010.0, 5000.0, 1012.0, 5002.0), us="15", area="1",
                                                    sito="Saggio").to_dict())
    doc.save(project)
    r = run.invoke(app, base + [str(tmp_path / "other"), *units, "--pyarchinit", "--people",
                                "--usm-heights", "relative"])
    assert r.exit_code == 0, r.output
    assert "records: 3 of 4 units" in r.output
    assert "  US 15 (Saggio 1): no row" in r.output
    assert json.loads((tmp_path / "other" / "site.json").read_text(encoding="utf-8"))["people"] is True

    r = run.invoke(app, base + [str(tmp_path / "plain"), *units])
    assert r.exit_code == 0, r.output
    assert "records:" not in r.output
    assert not (tmp_path / "plain" / "data" / "records.json").exists()

    r = run.invoke(app, base + [str(tmp_path / "bad"), *units, "--usm-heights", "all"])
    assert r.exit_code == 2
