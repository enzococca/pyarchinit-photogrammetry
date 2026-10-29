"""The privacy filter of the assistant.

When the model is Claude or OpenAI, what leaves the computer passes through a :class:`Filter`: the names of the
sites, places and people, the chunk labels, the project name, the paths, the absolute coordinates and the
coordinate system become neutral placeholders.  The answer of the model comes back through :meth:`Filter.incoming`,
which puts the real names back.  A local model needs no filter (:meth:`Filter.passthrough`).

Names are matched without regard to case, accents (also decomposed ones), typographic apostrophes and the gap
between the words (space, ``_``, ``-``, ``.``), and a name glued to ``_`` or to digits (``Tell_Sultan_0001``) is a
name.  One Filter is meant to live as long as a conversation: :meth:`Filter.update` adds the names that appeared
since, and keeps the placeholders already sent.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import PurePath
from typing import Any, Iterable
from urllib.parse import unquote

SITE_FIELDS = ("sito", "comune", "provincia", "regione", "toponimo", "soprintendenza")
PEOPLE_FIELDS = ("schedatore", "direttore_us", "responsabile_us", "disegnatore")
ABSOLUTE_KEYS = ("bounds", "origin", "center", "centre", "corners", "transform", "x", "y", "z", "lat", "lon",
                 "latitude", "longitude", "easting", "northing", "gps", "gnss", "reference", "references")
WARNING = "Il filtro non riconosce coordinate o nomi scritti nella domanda: non scriverli."

# absolute elevations: the tools give the heights above the lowest point instead (``height_above_lowest``)
ELEVATION_KEYS = ("z_min", "z_max", "z_mean", "z_median", "elevation", "elevation_min", "elevation_max",
                  "elevation_mean")
# the tokens of a key (split on ``_``, camelCase and digits) that make the whole value absolute
_DENY_TOKENS = frozenset(ABSOLUTE_KEYS) | {
    "bbox", "extent", "geometry", "geom", "centroid", "position", "location", "coordinates", "coordinate", "coord",
    "coords", "xy", "xyz", "quota", "elevation", "altitude", "lng", "easting", "northing"}
# the tokens that name a geometry: dropped when the value is a list of numbers or a WKT string
_GEOMETRY_TOKENS = frozenset({"polygon", "polygons", "line", "lines", "points", "point", "path", "vertices", "outline",
                              "ring", "contour", "contours", "poly", "linestring"})
_CRS_TOKENS = frozenset({"crs", "epsg", "srs", "proj", "utm"})
_RELATIVE_HEIGHT_TOKENS = frozenset({"dem", "dsm", "dtm"})
_STAT_TOKENS = frozenset({"min", "max", "mean", "median", "avg"})
_WKT_GEOMETRY = re.compile(r"^\s*(?:SRID=\d+;)?\s*(?:MULTI)?(?:POINT|LINESTRING|POLYGON|CURVE|SURFACE)|"
                           r"^\s*GEOMETRYCOLLECTION", re.IGNORECASE)

CRS_PROJECTED = "proiettato, in metri"
CRS_GEOGRAPHIC = "geografico (gradi)"
CRS_LOCAL = "locale"
CRS_UNKNOWN = "sistema non detto"
CRS_IN_TEXT = "sistema proiettato"

# the site fields: the first kind names the site ("Sito A"), the others are places ("Luogo 1")
_SITE_ONLY = ("sito", "sito_q")
_PLACE_FIELDS = tuple(f for f in SITE_FIELDS if f != "sito")
_MIN_LENGTH = 2
# folders that say nothing about the project: the parent folder of the project file is not hidden when it is one of them
_GENERIC_FOLDERS = frozenset({"dati", "data", "documents", "documenti", "desktop", "downloads", "download", "users",
                              "utenti", "home", "tmp", "temp", "dropbox", "progetti", "projects", "lavoro", "work",
                              "scavi", "scavo", "rilievi", "volumes", "private", "var", "folders"})

_GAP = r"[\s_\-.]+"
_GAP_RE = re.compile(_GAP)
_APOSTROPHES = {"’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'", "′": "'"}

# --- paths. A directory segment starts and ends with a visible character (so it never swallows the words around a path)
_SEG = r"[^\s/\\\n,;\"<>|?*](?:[^/\\\n,;\"<>|?*]*[^\s/\\\n,;\"<>|?*])?"
_EXT = r"\.[A-Za-z0-9]{1,5}(?![A-Za-z0-9])"
_URL = re.compile(r"(?<![\w])[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>]+")
_WIN_FILE = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)(?:" + _SEG + r"[\\/])*[^\\/\n,;\"<>|?*]*?" + _EXT)
_WIN_PLAIN = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)(?:" + _SEG + r"[\\/])*[^\\/\n,;\"<>|?*]+")
_POSIX_FILE = re.compile(r"(?<![\w./:~\\])~?/(?:" + _SEG + r"/)*[^/\\\n,;\"<>|?*]*?" + _EXT)
_POSIX_PLAIN = re.compile(r"(?<![\w./:~\\])~?/(?:" + _SEG + r"/)*[^\s/\\,;\"<>|?*]+")
# a relative path: its first component has no space, the others may
_RELATIVE = re.compile(r"(?<![\w./:~\\\-])[\w.~\-]+(?:/" + _SEG + r")+")

_SYSTEMS = (
    re.compile(r"\+proj=[^\n\"]*", re.IGNORECASE),
    re.compile(r"\bWGS ?84 ?/ ?UTM(?:\s+zone)?\s*\d{1,2}\s*[NSns]?\b", re.IGNORECASE),
    re.compile(r"\bUTM(?:\s+zone)?\s*\d{1,2}\s*[NSns]?\b", re.IGNORECASE),
    re.compile(r"\bEPSG\s*[:=]?\s*\d{3,6}\b", re.IGNORECASE),
)

_PLACEHOLDER = re.compile(
    r"(?<![^\W_])(?:(?i:sito)[\s_]+(?P<s>[A-Z]{1,3})|(?i:luogo)[\s_]+(?P<l>\d+)|(?i:persona)[\s_]+(?P<p>\d+)"
    r"|(?i:chunk)[\s_]+(?P<c>\d+))(?![^\W_])")


def _letters(n: int) -> str:
    """A, B, ... Z, AA, AB, ... for n = 0, 1, ..."""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _fold(text: str) -> tuple[str, list[int] | None]:
    """``text`` for matching: lower case, no accents (composed or not), plain apostrophes; and for each character of
    the result the index of the character of ``text`` it comes from (``None``: the same index, ASCII text)."""
    if text.isascii():
        return text.lower(), None
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c):
                continue
            for f in _APOSTROPHES.get(c, c).casefold():
                out.append(f)
                idx.append(i)
    return "".join(out), idx


def _tokens_of(folded: str) -> list[str]:
    return [t for t in _GAP_RE.split(folded) if t]


def _key(name: str) -> str:
    return " ".join(_tokens_of(_fold(name)[0]))


def neutral_name(path: Any) -> str:
    """The file name of ``path`` without folders nor extension, spaces as underscores: ``foto 12.jpg`` -> ``foto_12``."""
    name = re.split(r"[\\/]", str(path).rstrip("/\\"))[-1]
    stem = os.path.splitext(name)[0] or name
    return re.sub(r"\s+", "_", stem.strip()) or "file"


def _url_name(url: str) -> str:
    """The neutral name of a URL, the punctuation that closes the sentence after it kept."""
    bare = url.rstrip(").,;:!?]")
    return _url_neutral(bare) + url[len(bare):]


def _url_neutral(url: str) -> str:
    rest = re.split(r"[?#]", url.split("://", 1)[1])[0].rstrip("/")
    parts = rest.split("/")
    if len(parts) < 2 or not parts[-1]:
        return "indirizzo"
    return neutral_name(unquote(parts[-1]))


def _key_tokens(key: Any) -> list[str]:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", str(key))
    return [t.lower() for t in re.findall(r"[A-Za-z]+|\d+", s)]


def _numeric(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, (list, tuple)) and v:
        return all(_numeric(x) for x in v)
    return False


def _is_wkt(v: Any) -> bool:
    return isinstance(v, str) and bool(_WKT_GEOMETRY.match(v))


def _crs_label(value: Any) -> Any:
    """The kind of coordinate system ``value`` names (a string, a code, a pyproj CRS, a dict or a list of them),
    never the value; what cannot be told is "sistema non detto"."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            label = _crs_label(item)
            if label != CRS_UNKNOWN:
                return label
        return CRS_UNKNOWN
    if isinstance(value, bool):
        return CRS_UNKNOWN
    if isinstance(value, str) and value.strip().casefold() in ("local", "locale", "local coordinates"):
        return CRS_LOCAL
    if hasattr(value, "is_geographic"):
        geographic = value.is_geographic
    else:
        try:
            from pyproj import CRS

            geographic = CRS.from_user_input(value if isinstance(value, (str, int)) else str(value)).is_geographic
        except Exception:
            return CRS_UNKNOWN
    return CRS_GEOGRAPHIC if geographic else CRS_PROJECTED


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
        self._file_pattern: re.Pattern | None = None
        self._back: dict[str, str] = {}
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
        if not k or k in self._names:
            return
        self._names[k] = (re.sub(r"\s+", " ", name), self._placeholder(kind))

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
                k = _key(name)
                if len(name) >= _MIN_LENGTH and k and k not in self._names and k not in _GENERIC_FOLDERS:
                    self._names[k] = (name, "progetto")
        for ch in chunks:
            for cam in getattr(ch, "cameras", []) or []:
                p = getattr(cam, "path", None)
                if p:
                    p = str(p)
                    self._paths[p] = neutral_name(p)
                    self._files[re.split(r"[\\/]", p)[-1].casefold()] = neutral_name(p)

    def update(self, document: Any) -> int:
        """Adds the names that appeared in ``document`` since (a unit drawn, a chunk renamed, a site typed in
        PyArchInit); the placeholders already given stay as they are.  Returns the number of names added."""
        if not self.active or document is None:
            return 0
        before = len(self._names)
        self._collect(document)
        self._compile()
        return len(self._names) - before

    def _compile(self) -> None:
        patterns = []
        for k in sorted(self._names, key=lambda n: (-len(n), n)):
            tokens = k.split(" ")
            patterns.append(_GAP.join(re.escape(t) for t in tokens))
        # not preceded nor followed by a letter or a digit: "_" and "-" separate (Tell_Sultan_0001.JPG)
        self._out = re.compile(r"(?<![^\W_])(?:" + "|".join(patterns) + r")(?![^\W_])") if patterns else None
        self._back = {ph: real for real, ph in self._names.values() if ph != "progetto"}
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

    def _replace_names(self, text: str) -> str:
        if self._out is None:
            return text
        folded, idx = _fold(text)
        out: list[str] = []
        last = 0
        for m in self._out.finditer(folded):
            start, end = m.span()
            if idx is not None:
                start, end = idx[start], idx[end - 1] + 1
                while end < len(text) and unicodedata.combining(text[end]):
                    end += 1
            if start < last:
                continue
            out.append(text[last:start])
            out.append(self._names[" ".join(_tokens_of(m.group(0)))][1])
            last = end
        if not out:
            return text
        out.append(text[last:])
        return "".join(out)

    def _neutralise_known(self, text: str) -> str:
        for p in sorted(self._paths, key=len, reverse=True):
            if p in text:
                text = text.replace(p, self._paths[p])
        if self._file_pattern is not None:
            text = self._file_pattern.sub(lambda m: self._files[m.group(0).casefold()], text)
        return text

    @staticmethod
    def _neutralise_paths(text: str) -> str:
        for pattern in (_WIN_FILE, _WIN_PLAIN, _POSIX_FILE, _POSIX_PLAIN):
            text = pattern.sub(lambda m: neutral_name(m.group(0)), text)

        def relative(m: re.Match) -> str:
            s = m.group(0)
            if not re.search(r"[^\W\d_]", s.split("/", 1)[0]):
                return s  # 2026/10/29, 28/08 e 1/0.5: a date or a ratio, not a folder
            if s.count("/") < 2 and not re.search(_EXT + r"$", s):
                return s  # "e/o", "28/08": one slash is not a path unless it ends in a file
            return neutral_name(s)

        return _RELATIVE.sub(relative, text)

    def outgoing(self, text: str) -> str:
        if not self.active or not isinstance(text, str) or not text:
            return text
        text = _URL.sub(lambda m: _url_name(m.group(0)), text)
        if "%" in text:
            text = unquote(text)
        text = self._neutralise_known(text)
        text = self._replace_names(text)
        for pattern in _SYSTEMS:
            text = pattern.sub(CRS_IN_TEXT, text)
        return self._neutralise_paths(text)

    def incoming(self, text: str) -> str:
        if not self.active or not isinstance(text, str) or not text or not self._back:
            return text

        def restore(m: re.Match) -> str:
            if m.group("s"):
                canonical = f"Sito {m.group('s')}"
            elif m.group("l"):
                canonical = f"Luogo {int(m.group('l'))}"
            elif m.group("p"):
                canonical = f"Persona {int(m.group('p'))}"
            else:
                canonical = f"chunk {int(m.group('c'))}"
            return self._back.get(canonical, m.group(0))

        return _PLACEHOLDER.sub(restore, text)

    def unit_label(self, name: str) -> str:
        """Marker and unit names (T3, US 12) are not personal: they pass unchanged."""
        return name

    # ------------------------------------------------------------------ tool results
    def facts(self, data: Any) -> Any:
        """A copy of a tool result that is safe to send: no absolute coordinates, the system named by its kind,
        the names as placeholders, the paths as file names."""
        if not self.active:
            return data
        out = self._facts(data)
        return None if out is _DROP else out

    def _plain(self, data: Any) -> Any:
        if hasattr(data, "item") and getattr(data, "shape", None) == ():
            return data.item()  # a numpy scalar
        if hasattr(data, "tolist") and not isinstance(data, (str, bytes, bytearray)):
            return data.tolist()
        return data

    def _drops(self, key: Any, value: Any) -> bool:
        """Whether a key says the value is an absolute position, extent, height or geometry."""
        kl = str(key).lower()
        if kl in ABSOLUTE_KEYS or kl in ELEVATION_KEYS or _is_wkt(value):
            return True
        toks = _key_tokens(key)
        ts = set(toks)
        if ts & _DENY_TOKENS:
            return True
        if any(re.fullmatch(r"[xyz](?:min|max|mean|med|median)|(?:min|max|mean|median)[xyz]", t) for t in toks):
            return True
        if ts & _RELATIVE_HEIGHT_TOKENS and ts & _STAT_TOKENS:
            return True
        if ts & _GEOMETRY_TOKENS and (isinstance(value, (list, tuple)) and _numeric(value) or _is_wkt(value)):
            return True
        return False

    def _facts(self, data: Any) -> Any:
        data = self._plain(data)
        if isinstance(data, dict):
            out: dict = {}
            for k, v in data.items():
                v = self._plain(v)
                toks = set(_key_tokens(k))
                if "wkt" in toks:
                    if _is_wkt(v):
                        continue
                    new = _crs_label(v if v is not None else CRS_UNKNOWN)
                elif toks & _CRS_TOKENS:
                    new = _crs_label(v)
                elif self._drops(k, v):
                    continue
                else:
                    new = self._facts(v)
                    if new is _DROP:
                        continue
                key = self.outgoing(k) if isinstance(k, str) else k
                if key in out:  # two names became one placeholder: both entries stay
                    n = 2
                    while f"{key} ({n})" in out:
                        n += 1
                    key = f"{key} ({n})"
                out[key] = new
            return out
        if isinstance(data, (list, tuple, set, frozenset)):
            items = list(data)
            if isinstance(data, (set, frozenset)):
                items = sorted(items, key=str)
            kept = [x for x in (self._facts(v) for v in items) if x is not _DROP]
            return tuple(kept) if isinstance(data, tuple) else kept
        if isinstance(data, (bytes, bytearray, memoryview)):
            return _DROP
        if isinstance(data, PurePath):
            return self.outgoing(neutral_name(data))
        if isinstance(data, str):
            return self.outgoing(data)
        if data is None or isinstance(data, (bool, int, float)):
            return data
        return self.outgoing(str(data))  # an object nobody foresaw: its text, filtered


_DROP = object()
