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
