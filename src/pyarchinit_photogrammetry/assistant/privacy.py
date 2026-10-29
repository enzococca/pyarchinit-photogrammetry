"""The privacy filter of the assistant.

When the model is Claude or OpenAI, what leaves the computer passes through a :class:`Filter`: the names of the
sites, places and people, the chunk labels, the project name, the absolute paths, the absolute coordinates and the
coordinate system become neutral placeholders.  The answer of the model comes back through :meth:`Filter.incoming`,
which puts the real names back.  A local model needs no filter (:meth:`Filter.passthrough`).
"""
from __future__ import annotations

import os
import re
from pathlib import PurePath
from typing import Any, Iterable

SITE_FIELDS = ("sito", "comune", "provincia", "regione", "toponimo", "soprintendenza")
PEOPLE_FIELDS = ("schedatore", "direttore_us", "responsabile_us", "disegnatore")
ABSOLUTE_KEYS = ("bounds", "origin", "center", "centre", "corners", "transform", "x", "y", "z", "lat", "lon",
                 "latitude", "longitude", "easting", "northing", "gps", "gnss", "reference", "references")
WARNING = "Il filtro non riconosce coordinate o nomi scritti nella domanda: non scriverli."

# absolute elevations: the tools give the heights above the lowest point instead (``height_above_lowest``)
ELEVATION_KEYS = ("z_min", "z_max", "z_mean", "z_median", "elevation", "elevation_min", "elevation_max",
                  "elevation_mean")
# the fields of the site / place kind: the first one names the site ("Sito A"), the others are places ("Luogo 1")
_SITE_ONLY = ("sito", "sito_q")
_PLACE_FIELDS = tuple(f for f in SITE_FIELDS if f != "sito")
_MIN_LENGTH = 2

_WORD_GAP = r"[\s_]+"
# a path with an extension may contain spaces ("/Users/x/Scavi/Tell Sultan/foto 12.jpg"); one without stops at a space
_PATH_FILE = re.compile(r"(?<![\w./:])(?:[A-Za-z]:[\\/]|/)(?:[^\\/\n,;:\"'()]+[\\/])*[^\\/\n,;:\"'()]*?\.[A-Za-z0-9]{1,5}\b")
_PATH_PLAIN = re.compile(r"(?<![\w./:])(?:[A-Za-z]:[\\/]|/)[^\s,;\"')]+")


def _letters(n: int) -> str:
    """A, B, ... Z, AA, AB, ... for n = 0, 1, ..."""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _key(name: str) -> str:
    return re.sub(_WORD_GAP, " ", name.strip()).casefold()


def neutral_name(path: Any) -> str:
    """The file name of ``path`` without folders nor extension, spaces as underscores: ``foto 12.jpg`` -> ``foto_12``."""
    name = re.split(r"[\\/]", str(path).rstrip("/\\"))[-1]
    stem = os.path.splitext(name)[0] or name
    return re.sub(r"\s+", "_", stem.strip()) or "file"


def _pattern(names: Iterable[str]) -> re.Pattern | None:
    names = sorted({n for n in names if n}, key=lambda n: (-len(n), n))
    if not names:
        return None
    parts = [_WORD_GAP.join(re.escape(w) for w in n.split()) for n in names]
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


class Filter:
    """Replaces the names of the project with placeholders on their way out, and back on their way in.

    ``people`` and ``places`` are names to hide besides those the document holds.  With ``active=False`` (or
    :meth:`passthrough`) nothing is changed."""

    def __init__(self, document: Any = None, people: Iterable[str] = (), places: Iterable[str] = (),
                 active: bool = True):
        self.active = bool(active)
        self._names: dict[str, tuple[str, str]] = {}  # normalised real name -> (first spelling, placeholder)
        self._counts = {"site": 0, "place": 0, "person": 0, "chunk": 0}
        self._paths: dict[str, str] = {}  # a literal path -> its neutral name
        self._files: dict[str, str] = {}  # a bare file name (casefolded) -> its neutral name
        self._out: re.Pattern | None = None
        self._in: re.Pattern | None = None
        self._back: dict[str, str] = {}
        self._project: set[str] = set()
        if not self.active:
            return
        if document is not None:
            self._collect(document)
        for name in people:
            self._add("person", name)
        for name in places:
            self._add("place", name)
        self._compile()

    @classmethod
    def passthrough(cls) -> "Filter":
        return cls(None, active=False)

    # ------------------------------------------------------------------ collecting
    def _placeholder(self, kind: str) -> str:
        n = self._counts[kind]
        self._counts[kind] += 1
        return {"site": f"Sito {_letters(n)}", "place": f"Luogo {n + 1}", "person": f"Persona {n + 1}",
                "chunk": f"chunk {n + 1}"}[kind]

    def _add(self, kind: str, name: Any) -> None:
        if not isinstance(name, str):
            return
        name = name.strip()
        if len(name) < _MIN_LENGTH:
            return
        k = _key(name)
        if k in self._names:
            return
        self._names[k] = (re.sub(_WORD_GAP, " ", name) if " " in name else name, self._placeholder(kind))

    def _add_record(self, rec: Any) -> None:
        if not isinstance(rec, dict):
            return
        for source in (rec, rec.get("attributes")):
            if not isinstance(source, dict):
                continue
            for f in _SITE_ONLY:
                self._add("site", source.get(f))
            for f in _PLACE_FIELDS:
                self._add("place", source.get(f))
            for f in PEOPLE_FIELDS:
                self._add("person", source.get(f))

    def _collect(self, doc: Any) -> None:
        meta = getattr(doc, "meta", None) or {}
        self._add("site", meta.get("sito"))
        chunks = list(getattr(doc, "chunks", []) or [])
        for ch in chunks:
            drawings = (getattr(ch, "meta", None) or {}).get("drawings") or {}
            for rows in drawings.values():
                if isinstance(rows, (list, tuple)):
                    for rec in rows:
                        self._add_record(rec)
        for ch in chunks:
            self._add("chunk", getattr(ch, "label", None))
        path = getattr(doc, "path", None)
        if path is not None:
            self._paths[str(path)] = "progetto"
            try:
                self._paths[str(doc.files_dir)] = "progetto"
            except Exception:  # a folder that cannot be made must not stop the filter
                pass
            for name in (path.stem, path.parent.name):
                if len(name) >= _MIN_LENGTH and _key(name) not in self._names:
                    self._names[_key(name)] = (name, "progetto")
                    self._project.add(_key(name))
        for ch in chunks:
            for cam in getattr(ch, "cameras", []) or []:
                p = getattr(cam, "path", None)
                if p:
                    p = str(p)
                    self._paths[p] = neutral_name(p)
                    self._files[re.split(r"[\\/]", p)[-1].casefold()] = neutral_name(p)

    def _compile(self) -> None:
        self._out = _pattern(v[0] for v in self._names.values())
        # the way back: case sensitive (the model writes "sito a nord" in Italian), the placeholders of the names only
        self._back = {}
        for real, ph in self._names.values():
            if ph != "progetto":
                self._back.setdefault(ph, real)
        names = sorted(self._back, key=lambda n: (-len(n), n))
        self._in = (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)")
                    if names else None)
        self._file_pattern = (re.compile(r"(?<![\w.])(?:" + "|".join(
            re.escape(n) for n in sorted(self._files, key=lambda n: (-len(n), n))) + r")(?!\w)", re.IGNORECASE)
            if self._files else None)

    # ------------------------------------------------------------------ the way out and back
    @property
    def mapping(self) -> dict[str, str]:
        """Real name -> placeholder, as the window "Cosa viene inviato" shows it."""
        if not self.active:
            return {}
        return {real: ph for real, ph in self._names.values()}

    def _neutralise_paths(self, text: str) -> str:
        for p in sorted(self._paths, key=len, reverse=True):
            if p in text:
                text = text.replace(p, self._paths[p])
        text = _PATH_FILE.sub(lambda m: neutral_name(m.group(0)), text)
        text = _PATH_PLAIN.sub(lambda m: neutral_name(m.group(0)), text)
        if self._file_pattern is not None:
            text = self._file_pattern.sub(lambda m: self._files[m.group(0).casefold()], text)
        return text

    def outgoing(self, text: str) -> str:
        if not self.active or not isinstance(text, str) or not text:
            return text
        text = self._neutralise_paths(text)
        if self._out is not None:
            text = self._out.sub(lambda m: self._names[_key(m.group(0))][1], text)
        return text

    def incoming(self, text: str) -> str:
        if not self.active or not isinstance(text, str) or not text or self._in is None:
            return text
        return self._in.sub(lambda m: self._back[m.group(0)], text)

    def unit_label(self, name: str) -> str:
        """Marker and unit names (T3, US 12) are not personal: they pass unchanged."""
        return name

    # ------------------------------------------------------------------ tool results
    @staticmethod
    def _crs(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.strip().upper() in ("LOCAL", "LOCALE", ""):
            return "locale"
        try:
            from pyproj import CRS

            geographic = CRS.from_user_input(value).is_geographic
        except Exception:
            geographic = bool(re.search(r"\b(?:4326|4979|4258|4937|deg|degree)\b", value, re.IGNORECASE))
        return "geografico (gradi)" if geographic else "proiettato, in metri"

    def facts(self, data: Any) -> Any:
        """A copy of a tool result that is safe to send: no absolute coordinates, the system named by its kind,
        the names as placeholders, the paths as file names."""
        if not self.active:
            return data
        return self._facts(data)

    def _facts(self, data: Any) -> Any:
        if hasattr(data, "item") and getattr(data, "shape", None) == ():
            data = data.item()  # a numpy scalar
        elif hasattr(data, "tolist") and not isinstance(data, (str, bytes)):
            data = data.tolist()
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                kl = str(k).lower()
                if kl in ABSOLUTE_KEYS or kl in ELEVATION_KEYS:
                    continue
                key = self.outgoing(k) if isinstance(k, str) else k
                out[key] = self._crs(v) if kl == "crs" or kl.endswith("_crs") else self._facts(v)
            return out
        if isinstance(data, list):
            return [self._facts(v) for v in data]
        if isinstance(data, tuple):
            return tuple(self._facts(v) for v in data)
        if isinstance(data, PurePath):
            return neutral_name(data)
        if isinstance(data, str):
            return self.outgoing(data)
        return data
