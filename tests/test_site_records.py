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
