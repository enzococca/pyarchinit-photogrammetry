"""What plugins add to the web site of a survey (:mod:`.site`): layers on the plan, tabs of cards, pages of text.

A plugin registers a provider with ``context.add_site_contributions(provider)`` (:mod:`..plugins.api`); when a site
is built, ``provider(chunk)`` is called for every chunk that goes in and returns a list of contributions, each a
dict with a ``kind``:

``"layer"`` - a layer of the plan::

    {"kind": "layer", "id": "finds", "title": {"it": "Reperti", "en": "Finds"},
     "features": <GeoJSON FeatureCollection, or a list of features, in the coordinates of the chunk>,
     "colour": "#1f77b4",                        # of the features without a class of the legend
     "legend": {"field": "classe",              # the attribute that gives the class of a feature
                "items": [{"value": "anfora", "title": "Anfore", "colour": "#d62728"}, ...]},
     "fields": {"classe": {"it": "Classe", "en": "Class"}},   # names of the attributes in the popup
     "visible": True}

``"table"`` - a tab of cards (one per row) with the attributes and, when a row has one, an image::

    {"kind": "table", "id": "catalogue", "title": "Catalogo",
     "columns": [{"key": "n", "title": "N."}, {"key": "descrizione", "title": {"it": "Descrizione", "en": ...}}],
     "rows": [{"n": 1, "descrizione": "...", "image": "/path/of/a/picture.jpg"}, ...],
     "image": "image"}                            # the key of the rows that holds the path of an image

``"html"`` - a tab with a page of text::

    {"kind": "html", "id": "note", "title": "Note", "html": "<h3>...</h3><p>...</p>",
     "images": {"pianta.jpg": "/path/of/pianta.jpg"}}   # pictures the page shows: <img src="pianta.jpg">

What goes out: ``data/plugins/<plugin>/`` (``data/<n>/plugins/<plugin>/`` with several chunks) holds ``<id>.geojson``,
``<id>.json`` or ``<id>.html`` and the pictures (``<id>-<n>.jpg``); ``site.json`` lists them in ``plugins`` of the
chunk (a layer also among the ``layers`` of the chunk, drawn by the plan as the drawings of the program).

What the site makes sure of, whatever the plugin gives: the coordinates of a layer are moved into the frame of the
site (the local one: shifted, the heights ``z`` too); every text goes through the same cleaning as the texts of the
program (no path of the computer, no private key ``_...``); the pictures are decoded and written again as JPEG of at
most :data:`IMAGE_SIDE` pixels (no EXIF, no GPS, nothing but the pixels); the HTML keeps only the tags and attributes
of :data:`TAGS` (no script, no style, no event, no link but ``http(s)``/``mailto``, pictures only among its
``images``), and the page cleans it again when it shows it. A provider that fails, or a contribution that is not
understood, is left out and said in the notes of the build; the site is built all the same.
"""
from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger("pyarchinit_photogrammetry")

KINDS = ("layer", "table", "html")
IMAGE_SIDE = 1600
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TAGS = {  # tag: attributes kept
    "p": (), "br": (), "hr": (), "h3": (), "h4": (), "h5": (), "strong": (), "b": (), "em": (), "i": (), "u": (),
    "small": (), "sub": (), "sup": (), "code": (), "pre": (), "blockquote": (), "span": (), "div": (),
    "ul": (), "ol": (), "li": (), "dl": (), "dt": (), "dd": (), "table": (), "thead": (), "tbody": (), "tr": (),
    "th": ("colspan", "rowspan"), "td": ("colspan", "rowspan"), "caption": (), "figure": (), "figcaption": (),
    "a": ("href", "title"), "img": ("src", "alt", "title", "width", "height"),
}
VOID = {"br", "hr", "img"}
DROP = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "head", "title",
        "textarea", "select", "button", "form", "frameset", "frame", "link", "meta", "base"}

_providers: list[tuple[str, Callable]] = []


def register(provider: Callable, owner: str = "") -> tuple[str, Callable]:
    """``provider(chunk)`` -> a list of contributions, called for every chunk of every site built from now on;
    ``owner``: the name of the plugin (the folder of its files on the site). Returns the entry, for
    :func:`unregister`."""
    owner = str(owner or getattr(provider, "__module__", "") or "plugin").lower()
    owner = re.sub(r"[^a-z0-9_-]+", "-", owner).strip("-") or "plugin"
    entry = (owner, provider)
    _providers.append(entry)
    return entry


def unregister(entry) -> None:
    _providers[:] = [p for p in _providers if p is not entry]


def providers() -> list[tuple[str, Callable]]:
    return list(_providers)


# ------------------------------------------------------------------------------------------------ HTML
class _Cleaner(HTMLParser):
    def __init__(self, images: set[str]):
        super().__init__(convert_charrefs=True)
        self.images = images
        self.out: list[str] = []
        self.open: list[str] = []
        self.skip = 0  # inside a dropped element: its text goes too

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP:
            if tag not in VOID:
                self.skip += 1
            return
        if self.skip or tag not in TAGS:
            return
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            value = "" if value is None else str(value).strip()
            if name not in TAGS[tag]:
                continue
            if name == "href":
                if not re.match(r"^(https?:|mailto:)", value, re.I):
                    continue
            elif name == "src":
                if value not in self.images:
                    continue
            elif name in ("width", "height", "colspan", "rowspan") and not value.isdigit():
                continue
            kept.append(f" {name}={quoteattr(value)}")
        if tag == "img" and not any(k.startswith(" src=") for k in kept):
            return
        if tag == "a":
            kept.append(' rel="noopener noreferrer nofollow" target="_blank"')
        self.out.append(f"<{tag}{''.join(kept)}>")
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID and tag.lower() in TAGS and not self.skip and self.open \
                and self.open[-1] == tag.lower():
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP:
            if tag not in VOID:
                self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in TAGS or tag in VOID or tag not in self.open:
            return
        while self.open:
            last = self.open.pop()
            self.out.append(f"</{last}>")
            if last == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data))

    def close(self):
        super().close()
        while self.open:
            self.out.append(f"</{self.open.pop()}>")


def clean_html(text: str, images=()) -> str:
    """``text`` with only the tags and attributes of :data:`TAGS` (see the module docstring); ``images``: the names
    an ``<img src>`` may give."""
    cleaner = _Cleaner(set(images))
    cleaner.feed(str(text or ""))
    cleaner.close()
    return "".join(cleaner.out)


# ------------------------------------------------------------------------------------------------ writing
def _title(value, scrub, fallback: str) -> dict:
    if isinstance(value, dict):
        out = {k: scrub(str(v)) for k, v in value.items() if k in ("it", "en") and v}
        if out:
            return out
    text = scrub(str(value)) if value else fallback
    return {"it": text, "en": text}


def _colour(text, fallback: str) -> str:
    return text if isinstance(text, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", text) else fallback


def _picture(src, dst: Path) -> bool:
    """The picture ``src`` written again as a JPEG of at most :data:`IMAGE_SIDE` pixels, nothing but its pixels."""
    from PIL import Image, ImageOps

    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            if max(im.size) > IMAGE_SIDE:
                f = IMAGE_SIDE / max(im.size)
                im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
            im.save(dst, "JPEG", quality=85, optimize=True)
        return True
    except Exception as exc:  # noqa: BLE001 - a picture that cannot be read is left out
        log.warning("site: picture %s left out: %s", Path(str(src)).name, exc)
        return False


def _features(data) -> list[dict]:
    if isinstance(data, dict):
        if data.get("type") == "FeatureCollection":
            return list(data.get("features") or [])
        if data.get("type") == "Feature":
            return [data]
    return list(data or [])


def write(chunk, data: Path, rel: str, shift, clean, scrub, notes: list[str]) -> tuple[list[dict], list[dict], int]:
    """The contributions of every registered plugin for ``chunk`` written under ``data/plugins``: the layers for the
    ``layers`` of the chunk in ``site.json``, the entries of its ``plugins`` (tables and pages; the layers too) and
    the bytes written. ``notes``: where what was left out is said."""
    layers, listed, size = [], [], 0
    seen = set()
    for owner, provider in providers():
        try:
            given = provider(chunk) or []
        except Exception as exc:  # noqa: BLE001 - a plugin never stops the build
            log.exception("site: the plugin %s failed on %s", owner, chunk.label)
            notes.append(f"{chunk.label}: plugin {owner} failed ({exc}); left out")
            continue
        if isinstance(given, dict):
            given = [given]
        for item in given:
            try:
                kind, cid = item.get("kind"), str(item.get("id") or "")
                if kind not in KINDS or not NAME.match(cid) or (owner, cid) in seen:
                    raise ValueError(f"kind {kind!r}, id {cid!r}: kind one of {', '.join(KINDS)}, id lowercase "
                                     "letters, digits, '-' and '_', once per plugin")
                seen.add((owner, cid))
                folder = data / "plugins" / owner
                folder.mkdir(parents=True, exist_ok=True)
                url = f"{rel}/plugins/{owner}"
                title = _title(item.get("title"), scrub, cid)
                entry = {"plugin": owner, "kind": kind, "id": cid, "title": title}
                if kind == "layer":
                    entry.update(_layer(item, folder, url, shift, clean, scrub))
                    layers.append({"id": f"plugin:{owner}:{cid}", "type": "vector", "url": entry["url"],
                                   "title": title, "visible": bool(item.get("visible", True)),
                                   "count": entry["count"], "colour": entry["colour"], "plugin": owner,
                                   **({"groups": entry["groups"], "group_by": entry["group_by"]}
                                      if entry.get("groups") else {}),
                                   **({"fields": entry["fields"]} if entry.get("fields") else {})})
                elif kind == "table":
                    entry.update(_table(item, folder, url, clean, scrub))
                else:
                    entry.update(_page(item, folder, url, scrub))
                size += sum(f.stat().st_size for f in folder.glob(f"{cid}*") if f.is_file())
                listed.append(entry)
            except Exception as exc:  # noqa: BLE001 - one contribution left out, the others go
                notes.append(f"{chunk.label}: plugin {owner}: a contribution left out ({exc})")
    return layers, listed, size


def _move(c, shift):
    if c and isinstance(c[0], (int, float)):
        return shift.position(c)
    return [_move(x, shift) for x in c]


def _layer(item: dict, folder: Path, url: str, shift, clean, scrub) -> dict:
    feats = []
    for f in _features(item.get("features")):
        g = f.get("geometry") or {}
        if g.get("type") not in ("Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"):
            continue
        feats.append({"type": "Feature", "properties": clean(f.get("properties") or {}),
                      "geometry": {"type": g["type"], "coordinates": _move(g["coordinates"], shift)}})
    if not feats:
        raise ValueError(f"layer {item.get('id')}: no feature with a geometry")
    cid = item["id"]
    (folder / f"{cid}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                      ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    out = {"url": f"{url}/{cid}.geojson", "count": len(feats), "colour": _colour(item.get("colour"), "#7b3fa0")}
    legend = item.get("legend") or {}
    field = scrub(str(legend.get("field") or "")) if isinstance(legend, dict) else ""
    if field and legend.get("items"):
        groups = []
        for x in legend["items"]:
            value = scrub(str(x.get("value")))
            groups.append({"name": value, "title": _title(x.get("title"), scrub, value),
                           "colour": _colour(x.get("colour"), out["colour"]),
                           "count": sum(str(f["properties"].get(field)) == value for f in feats)})
        out.update(groups=groups, group_by=field)
    fields = item.get("fields")
    if isinstance(fields, dict):
        out["fields"] = {scrub(str(k)): _title(v, scrub, str(k)) for k, v in fields.items()}
    return out


def _table(item: dict, folder: Path, url: str, clean, scrub) -> dict:
    cid = item["id"]
    key = str(item.get("image") or "image")
    columns = []
    for c in item.get("columns") or []:
        c = c if isinstance(c, dict) else {"key": c}
        columns.append({"key": scrub(str(c.get("key"))), "title": _title(c.get("title"), scrub, str(c.get("key")))})
    rows = []
    for n, row in enumerate(item.get("rows") or [], 1):
        if not isinstance(row, dict):
            continue
        picture = row.get(key)
        out = clean({k: v for k, v in row.items() if k != key})
        if picture:
            name = f"{cid}-{n}.jpg"
            if _picture(picture, folder / name):
                out["_image"] = f"{url}/{name}"
        rows.append(out)
    if not columns:
        names = []
        for r in rows:
            names += [k for k in r if k != "_image" and k not in names]
        columns = [{"key": k, "title": {"it": k, "en": k}} for k in names]
    (folder / f"{cid}.json").write_text(json.dumps({"columns": columns, "rows": rows}, ensure_ascii=False,
                                                   separators=(",", ":")), encoding="utf-8")
    return {"url": f"{url}/{cid}.json", "count": len(rows)}


def _page(item: dict, folder: Path, url: str, scrub) -> dict:
    cid = item["id"]
    names = {}
    for n, (given, src) in enumerate((item.get("images") or {}).items(), 1):
        name = f"{cid}-{n}.jpg"
        if _picture(src, folder / name):
            names[str(given)] = name
    text = clean_html(str(item.get("html") or ""), names)

    def to_file(m):
        return f'src="{names[m.group(1)]}"' if m.group(1) in names else m.group(0)

    text = re.sub(r'src="([^"]*)"', to_file, text)
    # a path of the computer in the text: cut as every other text
    text = re.sub(r">([^<]+)<", lambda m: ">" + scrub(m.group(1)) + "<", f">{text}<")[1:-1]
    (folder / f"{cid}.html").write_text(text, encoding="utf-8")
    return {"url": f"{url}/{cid}.html", "base": url}


__all__ = ["register", "unregister", "providers", "clean_html", "write", "KINDS", "TAGS", "IMAGE_SIDE"]
