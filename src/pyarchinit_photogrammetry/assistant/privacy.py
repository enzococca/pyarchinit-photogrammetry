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
# a key is dropped when ANY of its tokens (split on ``_``, camelCase and digits) names a raw coordinate, unless a token of a
# derived quantity (rmse, error, accuracy, status, size, resolution, ...) is also there: ``marker_xyz``, ``camera_z``,
# ``z_range`` go; ``gps_rmse``, ``pixel_size_x``, ``position_accuracy_m`` stay.  A mean or a median of coordinates is a
# coordinate: ``x_mean``, ``lat_mean``, ``camera_z_mean`` go
_COORDINATE_TOKENS = frozenset(ABSOLUTE_KEYS) - {"reference", "references"} | {
    "bbox", "extent", "geometry", "geom", "centroid", "centroids", "position", "positions", "location", "locations",
    "coordinates", "coordinate", "coord", "coords", "pos", "xy", "xyz", "quota", "elevation", "altitude", "lng", "range",
    "corner", "wkt"}
_DERIVED_TOKENS = frozenset({"rmse", "error", "errors", "accuracy", "precision", "status", "count", "n", "size", "resolution",
                             "spacing", "std", "sigma", "percent", "ratio", "spread", "quality", "scale"})
_DEM_TOKENS = frozenset({"dem", "dsm", "dtm"})
_MODIFIER_TOKENS = frozenset({"min", "max", "abs", "q", "m", "deg", "degrees", "meters", "metres"})
_AXIS_STAT = re.compile(r"[xyz](?:min|max|mean|med|median)|(?:min|max|mean|median)[xyz]")
_REFERENCE_TOKENS = frozenset({"reference", "references"})
# the tokens that name a geometry: dropped when the value is a list of numbers or a WKT string
_GEOMETRY_TOKENS = frozenset({"polygon", "polygons", "line", "lines", "points", "point", "path", "vertices", "outline",
                              "ring", "contour", "contours", "poly", "linestring"})
_CRS_TOKENS = frozenset({"crs", "epsg", "srs", "proj", "utm"})
_WKT_GEOMETRY = re.compile(r"^\s*(?:SRID=\d+;)?\s*(?:MULTI)?(?:POINT|LINESTRING|POLYGON|CURVE|SURFACE)|"
                           r"^\s*GEOMETRYCOLLECTION", re.IGNORECASE)

CRS_PROJECTED = "proiettato, in metri"
CRS_GEOGRAPHIC = "geografico (gradi)"
CRS_LOCAL = "locale"
CRS_UNKNOWN = "sistema non detto"
CRS_IN_TEXT = "sistema di coordinate"

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
# invisible characters that can sit inside a name (zero-width space, joiners, word joiner, BOM, soft hyphen)
_INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
EMAIL = "indirizzo e-mail"

# --- paths. Only a token that starts with an anchor is a path (``/``, ``~/``, ``./``, ``../``, ``X:\\``, ``\\\\``, ``\\dir\\``,
# ``%VAR%\\``, ``scheme://``); it holds no space, unless it ends with a known file extension (at most 200 characters)
PUBLIC_HOSTS = ("enzococca.github.io", "github.com", "pypi.org", "doi.org", "opengeos.org", "qgis.org", "wikipedia.org")
_KNOWN_EXT = (r"\.(?:jpg|jpeg|png|tif|tiff|ppg|psx|gpkg|csv|txt|json|ply|las|laz|obj|glb|gltf|pdf|xlsx|dxf|svg|ifc|e57|"
              r"zip|mrk|obs|nav|rnx|crx|sqlite|db|cfg|md|docx|doc|xls|kml|kmz|shp|dbf|geojson|mp4|mov|srt|log|yaml|yml|"
              r"toml|ini)(?![A-Za-z0-9])")
_ANCHOR = re.compile("|".join([
    r"(?P<url>(?<!\w)[A-Za-z][A-Za-z0-9+.\-]*://)",
    r"(?P<win>(?<![\w\\])(?:[A-Za-z]:[\\/]|\\\\|%[A-Za-z_][A-Za-z0-9_]*%[\\/]))",
    r"(?P<rooted>(?<![\w\\.:/])\\(?=[A-Za-z0-9_$.][^\s\\]+\\))",
    r"(?P<var>(?<![\w$\\])\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})/)",
    r"(?P<posix>(?<![\w./:~\\<>%$)\]}*°])(?:~/|\.{1,2}/|/(?=[^\s/])))",
]))
# the first token of a path: no space nor punctuation, but an apostrophe between two letters (Sant'Angelo)
_TOKEN = re.compile(r"(?:[^\s\"'`<>|*?,;()\[\]]|(?<=[^\W\d_])'(?=[^\W\d_]))+")
_MD_TARGET = re.compile(r"(?:\.{1,2}/|#)[^\s)]*(?:\.md(?:#[^\s)]*)?|/)$")
# the words after the first token: a path goes on over them; these characters end it (an apostrophe only when it is
# not between two letters), and these are stripped from its end
_STOPS = re.compile(r"[\"`<>|*?;]|'(?![^\W\d_])|(?<![^\W\d_])'")
_TRAILING = ".,:!)]"
_UNIT_PART = re.compile(r"[^\W\d_]{1,3}")
_QUOTES = {'"': '"', "'": "'", "`": "`"}
_BRACKETS = {"(": ")", "[": "]"}
_PATH_CAP = 200
_ENDS_KNOWN = re.compile(_KNOWN_EXT + r"$", re.IGNORECASE)
_URL_TOKEN = re.compile(r"[^\s\"'`<>]+")
_HAS_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}$")

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


def _fold(text: str, invisible: str = "") -> tuple[str, list[int] | None]:
    """``text`` for matching: lower case, no accents (composed or not), plain apostrophes, the invisible characters
    (zero-width, soft hyphen) replaced by ``invisible`` (``""``: dropped, ``" "``: a gap between words); and for each
    character of the result the index of the character of ``text`` it comes from (``None``: the same index, ASCII text)."""
    if text.isascii():
        return text.lower(), None
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch in _INVISIBLE:
            if invisible:
                out.append(invisible)
                idx.append(i)
            continue
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


def _is_public(url: str) -> bool:
    """Whether the host of ``url`` is one of :data:`PUBLIC_HOSTS` (or a subdomain), without credentials."""
    authority = re.split(r"[/?#]", url.split("://", 1)[1], 1)[0]
    if "@" in authority:
        return False
    host = authority.rsplit(":", 1)[0].lower() if re.search(r":\d+$", authority) else authority.lower()
    return any(host == h or host.endswith("." + h) for h in PUBLIC_HOSTS)


def _url_name(url: str) -> str:
    """The neutral name of a URL, the punctuation that closes the sentence after it kept; a public URL stays."""
    bare = url.rstrip(").,;:!?]")
    if _is_public(bare):
        return url
    return _url_neutral(bare) + url[len(bare):]


def _url_neutral(url: str) -> str:
    """"indirizzo", or the neutral name of the file the URL points to when its last component ends with a known
    extension (``.../rilievo/a.tif`` -> ``a``): a map link says nothing (``google.com/maps/@41.13,14.26,17z``)."""
    rest = re.split(r"[?#]", url.split("://", 1)[1])[0].rstrip("/")
    parts = rest.split("/")
    last = unquote(parts[-1]) if len(parts) >= 2 else ""
    if not last or not _ENDS_KNOWN.search(last):
        return "indirizzo"
    return neutral_name(last)


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


def _holds_numbers(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, dict):
        return any(_holds_numbers(x) for x in v.values())
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(_holds_numbers(x) for x in v)
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


def _is_unit(word: str) -> bool:
    """Whether a word that holds a separator is a short unit or an alternative (``km/h``, ``m/s``, ``mm/px``, ``e/o``,
    ``H/E/N``, ``TCP/IP``): no known extension and every part made of 1-3 letters; or a date or a fraction, every
    part made of digits (``12/08/2026``, ``1/2``).  Any other such word after the start of a path is a piece of it
    (``Halaf/Dati``, ``Vista/Pannelli``, ``omega/phi/kappa``)."""
    if _ENDS_KNOWN.search(word):
        return False
    parts = re.split(r"[\\/]", word)
    return all(_UNIT_PART.fullmatch(part) for part in parts) or all(part.isdigit() for part in parts)


def _quoted_end(text: str, start: int, opener: str) -> int | None:
    """The end of a path that starts at ``start`` right after the quote ``opener``: the closing quote on the same line
    (for ``'`` one that is not between two letters), within 200 characters; ``None`` when there is none."""
    limit = min(len(text), start + _PATH_CAP + 1)
    for i in range(start + 1, limit):
        c = text[i]
        if c == "\n":
            return None
        if c != opener:
            continue
        if opener == "'" and text[i - 1:i].isalpha() and text[i + 1:i + 2].isalpha():
            continue  # an apostrophe: Sant'Angelo
        end = i
        while end > start and text[end - 1].isspace():
            end -= 1
        return end
    return None


def _extend(text: str, start: int, end: int, closer: str | None = None) -> int:
    """The end of the path that starts at ``start`` and whose first space-free token ends at ``end``.  Privacy first:
    the path goes on over the words that follow, as far as the last word on the line that holds a separator or ends
    with a known extension (at most 200 characters, the punctuation that closes it left outside).  It stops at a short
    unit (``5 mm/px``), at a new path, at a character that cannot be in a file name, and at a word that ends with
    ``closer`` (the bracket that closes the one the path started after).  Prose after a real path on the same line may
    be swallowed."""
    last = pos = end
    cap = start + _PATH_CAP
    while text[pos:pos + 1] in (".", ":", "!"):  # "Dott. Rossi/foto": a full stop does not end the path
        pos += 1
    while True:
        gap = re.compile(r"[^\S\n]+").match(text, pos)
        word = re.compile(r"\S+").match(text, gap.end()) if gap else None
        if word is None:
            return last
        w0, raw = word.start(), word.group(0)
        stop = _STOPS.search(raw)
        cut = stop.start() if stop else None
        if closer is not None:  # a word that ends with the closing bracket ends the path: "(/Users/a/b c/d)"
            head = raw[:cut].rstrip(".,:!")
            if head.endswith(closer):
                cut = len(head) - 1
        piece = (raw if cut is None else raw[:cut]).rstrip(_TRAILING)
        if w0 + len(piece) > cap or _ANCHOR.search(text, w0, w0 + len(raw)):
            return last  # too long, or a new path (also inside the word: "[y](/Users/...")
        if re.search(r"[\\/]", piece) and re.search(r"[^\W_]", piece):
            if _is_unit(piece):
                return last
            last = w0 + len(piece)
        elif piece and _ENDS_KNOWN.search(piece):
            last = w0 + len(piece)
        if cut is not None:
            return last
        pos = word.end()


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
        self._aliases: dict[str, str] = {}  # a person written surname first ("rossi, mario") -> the name's key
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
        # a person is also written surname first, with a comma: "Rossi, Mario" (and "De Luca, Mario" for "Mario De Luca")
        self._aliases = {}
        for k, (_, ph) in self._names.items():
            tokens = k.split(" ")
            if not ph.startswith("Persona "):
                continue
            for i in range(1, len(tokens)):
                alias = " ".join(tokens[i:]) + ", " + " ".join(tokens[:i])
                self._aliases.setdefault(alias, k)
        forms = {k: _GAP.join(re.escape(t) for t in k.split(" ")) for k in self._names}
        for alias in self._aliases:
            head, tail = alias.split(", ", 1)
            forms.setdefault(alias, _GAP.join(re.escape(t) for t in head.split(" ")) + r"\s*,\s*"
                             + _GAP.join(re.escape(t) for t in tail.split(" ")))
        patterns = [forms[k] for k in sorted(forms, key=lambda n: (-len(n), n))]
        # not preceded nor followed by a letter or a digit: "_" and "-" separate (Tell_Sultan_0001.JPG)
        self._out = re.compile(r"(?<![^\W\d_])(?:" + "|".join(patterns) + r")(?![^\W\d_])") if patterns else None
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
        # first the invisible characters dropped (Ros\u200bsi), then as gaps between words (Mario\u200bRossi)
        return self._replace_names_once(self._replace_names_once(text, ""), " ")

    def _name_of(self, matched: str) -> str:
        """The placeholder of a matched (folded) name, also when written surname first."""
        k = " ".join(_tokens_of(matched))
        if k in self._names:
            return self._names[k][1]
        head, tail = re.split(r"\s*,\s*", matched, maxsplit=1)
        return self._names[self._aliases[" ".join(_tokens_of(head)) + ", " + " ".join(_tokens_of(tail))]][1]

    def _replace_names_once(self, text: str, invisible: str) -> str:
        if self._out is None:
            return text
        folded, idx = _fold(text, invisible)
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
            out.append(self._name_of(m.group(0)))
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
        """URLs (but the public ones) and anchored paths reduced to a neutral name; prose is left as it is."""
        out: list[str] = []
        pos = 0
        for m in _ANCHOR.finditer(text):
            s0 = m.start()
            if s0 < pos:
                continue
            if s0 and text[s0 - 1] == "`" and text[:s0].count("`") % 2 == 0:
                continue  # `mild`/`none`: the backquote closes a word, it does not open a path
            if m.group("url"):
                token = _URL_TOKEN.match(text, s0).group(0)
                end = s0 + len(token)
                out.append(text[pos:s0])
                out.append(_url_name(token))
                pos = end
                continue
            token = _TOKEN.match(text, s0)
            token = token.group(0) if token else text[s0]
            token = token.rstrip(".:!") or token  # the full stop that ends the sentence stays outside
            if s0 >= 2 and text[s0 - 2:s0] == "](" and _MD_TARGET.match(token):
                continue  # a relative link of the guide (../api.md#x, ./, #): no private data
            known = bool(_ENDS_KNOWN.search(token) or _HAS_EXT.search(token))
            if m.group(0) == "/" and not known and len([c for c in token.split("/") if c]) < 2:
                continue  # /clear, /3: a command or a fraction, not a path
            if not known:
                opener = text[s0 - 1] if s0 else ""
                if opener == "'" and s0 >= 2 and text[s0 - 2].isalpha():
                    opener = ""  # dell'/...: an apostrophe, not a quote
                quoted = _quoted_end(text, s0, opener) if opener in _QUOTES else None
                if quoted is not None:
                    token = text[s0:quoted]  # the closing quote says where the path ends
                else:
                    token = text[s0:_extend(text, s0, s0 + len(token), _BRACKETS.get(opener))]
            name = neutral_name(unquote(token)) if re.search(r"[^\W_]", token[len(m.group(0)):]) else None
            if name is None:
                continue
            out.append(text[pos:s0])
            out.append(name)
            pos = s0 + len(token)
        if not out:
            return text
        out.append(text[pos:])
        return "".join(out)

    def outgoing(self, text: str) -> str:
        if not self.active or not isinstance(text, str) or not text:
            return text
        text = self._neutralise_known(text)
        text = self._neutralise_paths(text)
        text = _EMAIL.sub(EMAIL, text)
        text = self._replace_names(text)
        for pattern in _SYSTEMS:
            text = pattern.sub(CRS_IN_TEXT, text)
        return text

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
        """Whether a key says the value is a raw coordinate, extent, absolute height or geometry."""
        kl = str(key).lower()
        if kl in ABSOLUTE_KEYS and kl not in _REFERENCE_TOKENS or kl in ELEVATION_KEYS or _is_wkt(value):
            return True
        toks = _key_tokens(key)
        ts = set(toks)
        derived = bool(ts & _DERIVED_TOKENS)
        if ts & _REFERENCE_TOKENS and not derived and not (ts & _COORDINATE_TOKENS):
            return _holds_numbers(value)
        if not derived and (ts & _COORDINATE_TOKENS or any(_AXIS_STAT.fullmatch(t) for t in toks)
                            or (ts & _DEM_TOKENS and ts & {"min", "max"})):
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
