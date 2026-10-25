"""Batch identification preserves geometry, separates namespaces and refuses stale previews."""
from __future__ import annotations

import pytest
from shapely.geometry import box

from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
from pyarchinit_photogrammetry.archaeo.unit_identity import apply_changes, plan_units, unit_label


def unit(number=None, kind="US", **attrs):
    return UnitPolygon(box(0, 0, 1, 1), us=number, tipo=kind, sito="Temple", area="1", attributes=attrs)


def test_numbering_reserves_drawings_and_archive_records_without_changing_assigned_units():
    units = [unit("4"), unit(), unit()]
    changes = plan_units(units, reserved=[{"sito": "Temple", "area": "1", "us": "8"}],
                         name_mode="progressive", name="Tomba", padding=4)
    assert units[1].us is None  # preview is pure
    apply_changes(units, changes)
    assert [u.us for u in units] == ["4", "9", "10"]
    assert units[1].attributes["nome"] == "Tomba0009"


def test_shared_name_moves_only_selected_units_to_topographic_toc_group():
    units = [unit(), unit(), unit()]
    original = units[0].geometry
    changes = plan_units(units, [0, 2], tipo="UT", progetto="Survey", name_mode="same", name="Tombe A",
                         group="Tombe A", reserved=[{"progetto": "Survey", "nr_ut": 5}],
                         number_mode="missing")
    apply_changes(units, changes)
    assert [u.us for u in units] == ["6", None, "7"]
    assert units[0].geometry is original
    assert units[0].attributes["_layer"] == "Unità topografiche - Tombe A"
    assert units[0].attributes["nome"] == units[2].attributes["nome"] == "Tombe A"
    assert units[0].attributes["nr_ut"] == 6
    assert units[0].attributes["entity_type"] == "UT"
    assert unit_label(units[0]) == "Tombe A · UT 6"
    assert units[1].tipo == "US"


def test_keep_numbers_while_naming_and_namespace_collision_preflight():
    units = [unit("1"), unit("2")]
    changes = plan_units(units, [0], number_mode="keep", name_mode="same", name="Gruppo")
    apply_changes(units, changes)
    assert units[0].us == "1" and units[1].attributes == {}
    with pytest.raises(ValueError, match="occupied"):
        plan_units(units, [0], tipo="UT", progetto="Survey", number_mode="keep",
                   reserved=[{"progetto": "Survey", "nr_ut": 1}])
    assert units[0].tipo == "US"


def test_explicit_start_skips_used_numbers_and_linked_entities_cannot_be_retyped():
    units = [unit("2"), unit(), unit()]
    apply_changes(units, plan_units(units, [1, 2], first=1))
    assert [u.us for u in units] == ["2", "1", "3"]
    with pytest.raises(ValueError, match="linked"):
        plan_units([unit("1", gid=42)], tipo="UT", progetto="Survey")


def test_stale_preview_refused_atomically():
    units = [unit(), unit()]
    changes = plan_units(units)
    units[1].attributes["nome"] = "Changed meanwhile"
    with pytest.raises(ValueError, match="changed"):
        apply_changes(units, changes)
    assert units[0].us is None and units[1].us is None


def test_retyping_checks_collisions_with_selected_existing_ut_regardless_of_order():
    units = [unit("1"), unit("1", kind="UT", progetto="Survey")]
    with pytest.raises(ValueError, match="occupied"):
        plan_units(units, tipo="UT", progetto="Survey", number_mode="keep")
    assert units[0].tipo == "US"


def test_newly_appended_unit_cannot_take_a_number_reserved_by_the_preview():
    units = [unit()]
    changes = plan_units(units)
    units.append(unit("1"))
    with pytest.raises(ValueError, match="became occupied"):
        apply_changes(units, changes)
    assert units[0].us is None


def test_same_name_on_1724_units_keeps_every_identifier_unique():
    units = [unit() for _ in range(1724)]
    changes = plan_units(units, tipo="UT", progetto="Survey", group="Necropoli",
                         name_mode="same", name="Tomba")
    apply_changes(units, changes)
    assert len({u.us for u in units}) == 1724
    assert {u.attributes["nome"] for u in units} == {"Tomba"}
    assert {u.attributes["_layer"] for u in units} == {"Unità topografiche - Necropoli"}
