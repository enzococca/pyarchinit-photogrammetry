"""Preview and apply bulk names and identifiers without conflating UT and excavation units.

``reserved`` accepts UnitPolygon objects or records. US records use ``tipo``/``unita_tipo``,
``sito``, ``area``, ``us``; UT records use ``tipo='UT'``, ``progetto``, ``nr_ut``. Records with
``nr_ut`` also imply UT. Numbering shares a namespace across US kinds within site/area; UT has
its own project namespace. Geometry and stable feature identifiers are left intact.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType

from .stratigraphy import US_TYPES

UNIT_TYPES = [*US_TYPES, "UT"]


def _text(value):
    return str(value).strip() if value not in (None, "") else None


def _number(value):
    value = _text(value)
    return int(value) if value is not None and value.isdecimal() else None


@dataclass(frozen=True)
class UnitState:
    us: str | None
    tipo: str
    area: str | None
    sito: str | None
    attributes: Mapping

    def as_dict(self):
        return {"us": self.us, "tipo": self.tipo, "area": self.area, "sito": self.sito,
                "attributes": deepcopy(dict(self.attributes))}


@dataclass(frozen=True)
class UnitChange:
    index: int
    before: UnitState
    after: UnitState


def _state(unit):
    if isinstance(unit, UnitState):
        return unit
    if isinstance(unit, Mapping):
        a = deepcopy(dict(unit.get("attributes") or {}))
        kind = unit.get("tipo") or unit.get("unita_tipo") or ("UT" if "nr_ut" in unit else "US")
        for key in ("progetto", "nr_ut", "ut_letterale"):
            if key in unit:
                a[key] = unit[key]
        return UnitState(_text(unit.get("us", unit.get("nr_ut"))), kind, _text(unit.get("area")),
                         _text(unit.get("sito")), MappingProxyType(a))
    return UnitState(_text(unit.us), unit.tipo or "US", _text(unit.area), _text(unit.sito),
                     MappingProxyType(deepcopy(dict(unit.attributes or {}))))


def namespace(unit):
    state = _state(unit)
    if state.tipo == "UT":
        return ("UT", _text(state.attributes.get("progetto")) or state.sito or "")
    return ("US", state.sito or "", state.area or "")


def _identity(state):
    if not state.us:
        return None
    number = _number(state.us)
    return (*namespace(state), str(number) if number is not None else state.us)


def unit_layer(tipo, group=""):
    base = "Unità topografiche" if tipo == "UT" else "Unita' stratigrafiche"
    return f"{base} - {str(group).strip()}" if str(group or "").strip() else base


def unit_label(unit):
    state = _state(unit)
    identity = f"{state.tipo} {state.us or '?'}"
    name = _text(state.attributes.get("nome")) or (_text(state.attributes.get("ut_letterale"))
                                                  if state.tipo == "UT" else None)
    return f"{name} · {identity}" if name and name != identity else identity


def plan_units(units, indices=None, *, tipo=None, number_mode="missing", first=None,
               name_mode="keep", name="", padding=4, group=None, area=None, sito=None,
               progetto=None, reserved=(), order="source") -> list[UnitChange]:
    """Return changes without modifying units; an explicit start skips occupied identifiers.

    ``missing`` only numbers blank units; ``all`` reassigns every chosen number; ``keep`` preserves
    them. Names can remain unchanged, be shared verbatim, or use ``name`` as prefix followed by
    the numeric identifier (or the selection's progression when identifiers are kept blank).
    ``group=None`` preserves grouping; an empty string removes it. ``order='spatial'`` orders by
    centroid north-to-south, then west-to-east. Existing duplicate identities are tolerated when
    naming only; moving an identity into an occupied namespace is refused.
    """
    if number_mode not in ("keep", "missing", "all") or name_mode not in ("keep", "same", "progressive"):
        raise ValueError("Invalid numbering or naming mode")
    if type(padding) is not int or not 0 <= padding <= 12:
        raise ValueError("Name padding must be between 0 and 12")
    if first is not None and (type(first) is not int or first < 1):
        raise ValueError("The first number must be a positive integer")
    if order not in ("source", "spatial"):
        raise ValueError("Invalid numbering order")
    if tipo is not None:
        lookup = {kind.upper(): kind for kind in UNIT_TYPES}
        tipo = lookup.get(str(tipo).strip().upper())
        if tipo is None:
            raise ValueError("Unknown unit type")
    chosen = sorted(set(range(len(units)) if indices is None else indices))
    if any(type(i) is not int or not 0 <= i < len(units) for i in chosen):
        raise ValueError("Invalid unit selection")
    if order == "spatial":
        chosen.sort(key=lambda i: (-units[i].geometry.centroid.y, units[i].geometry.centroid.x, i))
    before = [_state(unit) for unit in units]
    outsiders = [_state(unit) for unit in reserved]
    proposed, renumber = {}, set()
    for i in chosen:
        old = before[i]
        kind = tipo or old.tipo
        if (kind == "UT") != (old.tipo == "UT") and any(old.attributes.get(k) is not None
                                                       for k in ("gid", "id_us", "id_ut")):
            raise ValueError("A unit linked to a PyArchInit record cannot change between US and UT")
        attrs = deepcopy(dict(old.attributes))
        if group is not None:
            if str(group).strip():
                attrs["gruppo"] = str(group).strip()
            else:
                attrs.pop("gruppo", None)
        site = _text(sito) if sito is not None else old.sito
        if kind == "UT":
            attrs["entity_type"] = "UT"
            attrs["progetto"] = (_text(progetto) if progetto is not None else
                                 _text(attrs.get("progetto")) or site or "")
            if not attrs["progetto"]:
                raise ValueError("Choose a project for topographic units")
        elif old.tipo == "UT":
            for key in ("entity_type", "nr_ut", "progetto", "ut_letterale"):
                attrs.pop(key, None)
        if kind == "UT" or kind != old.tipo or group is not None:
            attrs["_layer"] = unit_layer(kind, attrs.get("gruppo", ""))
        proposed[i] = UnitState(old.us, kind, _text(area) if area is not None else old.area, site,
                                MappingProxyType(attrs))
        if number_mode == "all" or (number_mode == "missing" and not old.us):
            renumber.add(i)
    occupied, maxima = set(), {}
    for state in before + outsiders:
        num = _number(state.us)
        if num is not None:
            key = namespace(state)
            maxima[key] = max(maxima.get(key, 0), num)
    for i, state in enumerate(before):
        if i not in proposed and (key := _identity(state)) is not None:
            occupied.add(key)
    occupied.update(key for state in outsiders if (key := _identity(state)) is not None)
    # A moving identity must also be checked against selected units that keep their number,
    # independent of their iteration order. Naming existing duplicates alone stays possible.
    kept = [(i, _identity(proposed[i])) for i in chosen if i not in renumber]
    for i, identity in kept:
        if identity is not None and identity != _identity(before[i]) and any(
                other != i and key == identity for other, key in kept):
            raise ValueError(f"Identifier {proposed[i].us} is already occupied in the destination")
    for i in chosen:
        if i in renumber:
            continue
        state = proposed[i]
        identity = _identity(state)
        if identity is not None:
            if identity in occupied and identity != _identity(before[i]):
                raise ValueError(f"Identifier {state.us} is already occupied in the destination")
            occupied.add(identity)
    counters, result = {}, []
    for rank, i in enumerate(chosen):
        state = proposed[i]
        values = state.as_dict()
        attrs = values["attributes"]
        if i in renumber:
            ns = namespace(state)
            num = counters.get(ns, first if first is not None else maxima.get(ns, 0) + 1)
            while (*ns, str(num)) in occupied:
                num += 1
            values["us"] = str(num)
            occupied.add((*ns, str(num)))
            counters[ns] = num + 1
        if name_mode != "keep":
            text = str(name).strip()
            if name_mode == "progressive":
                num = _number(values["us"])
                text += f"{num if num is not None else (first or 1) + rank:0{padding}d}"
            if text:
                attrs["nome"] = text
            else:
                attrs.pop("nome", None)
        if values["tipo"] == "UT":
            num = _number(values["us"])
            if values["us"] and (num is None or not 1 <= num <= 2_147_483_647):
                raise ValueError("Topographic unit numbers must be positive integers")
            attrs["nr_ut"] = num
            if name_mode != "keep" or not attrs.get("ut_letterale"):
                attrs["ut_letterale"] = attrs.get("nome", "")
        final = UnitState(values["us"], values["tipo"], values["area"], values["sito"], MappingProxyType(attrs))
        if final != before[i]:
            result.append(UnitChange(i, before[i], final))
    return result


def apply_changes(units, changes) -> int:
    """Apply a complete validated plan, refusing stale previews before changing any unit."""
    changes = list(changes)
    if len({change.index for change in changes}) != len(changes):
        raise ValueError("A unit occurs more than once in the plan")
    for change in changes:
        if not 0 <= change.index < len(units) or _state(units[change.index]) != change.before:
            raise ValueError("The units changed after the preview; prepare a new plan")
    chosen = {change.index for change in changes}
    occupied = {key for i, unit in enumerate(units) if i not in chosen
                and (key := _identity(_state(unit))) is not None}
    for change in changes:
        key = _identity(change.after)
        if key is not None:
            if key in occupied and key != _identity(change.before):
                raise ValueError("An identifier became occupied after the preview; prepare a new plan")
            occupied.add(key)
    for change in changes:
        unit, values = units[change.index], change.after.as_dict()
        for key in ("us", "tipo", "area", "sito", "attributes"):
            setattr(unit, key, values[key])
    return len(changes)
