"""Processing report as a standalone HTML page (the figures embedded).

The content is the one of the PDF report (:mod:`io.report_content`): survey, calibration,
references and their errors, processing parameters, products, multispectral and thermal products,
measuring points; :mod:`io.pdf_report` lays the same content out for printing.
"""
from __future__ import annotations

import base64
import html
from pathlib import Path

from .report_content import ColourBar, Figure, KeyValues, Legend, Paragraph, Report, Section, Table, collect

CSS = """
body{font-family:"DejaVu Sans",system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1000px;margin:2em auto;
padding:0 1em;color:#232323;background:#fff;line-height:1.4}
h1{color:#1f4e79;margin-bottom:.1em}h2{color:#1f4e79;border-bottom:2px solid #1f4e79;padding-bottom:.2em;margin-top:2em}
h3{color:#1f4e79;border-bottom:1px solid #bec6d0;padding-bottom:.15em;margin-top:1.6em}h4{margin:1.2em 0 .4em}
.sub{font-size:1.25em;margin-top:0}
table{border-collapse:collapse;margin:.4em 0 1em;font-size:13px}
td,th{padding:3px 8px;text-align:left;vertical-align:top}th{background:#1f4e79;color:#fff}
tr:nth-child(even) td{background:#f2f5f8}td.r,th.r{text-align:right}
table.kv td:first-child{font-weight:bold;color:#464646;padding-right:1.5em}
figure{margin:1em 0;text-align:center}figure img{max-width:100%;border:1px solid #bec6d0}
figcaption{font-style:italic;color:#737373;font-size:13px}
.bar{display:inline-block;width:min(380px,60%);margin:.4em 0;text-align:left;font-size:11px;color:#3c3c3c}
.ramp{height:12px;border:1px solid #787878}.ticks{position:relative;height:1.3em}.ticks span{position:absolute;
transform:translateX(-50%)}.legend{font-size:12px;margin:.3em 0}.legend span{display:inline-block;margin:0 .8em .2em 0}
.legend i{display:inline-block;width:12px;height:11px;border:1px solid #5a5a5a;margin-right:4px;vertical-align:-1px}
.note{color:#737373;font-style:italic;font-size:13px}nav li.l0{font-weight:bold;margin-top:.3em}
"""


def generate_report(chunk, path, language: str | None = "en", progress=None, index_palette: str = "rdylgn",
                    thermal_palette: str = "iron") -> Path:
    """The HTML report of ``chunk`` at ``path`` (a :class:`Document` gives every enabled chunk).
    ``language``: ``en`` (the default, as the report always was), ``it``, or None for the language
    of the interface or of the system."""
    report = collect(chunk, language=language, index_palette=index_palette, thermal_palette=thermal_palette,
                     progress=progress)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(report), encoding="utf-8")
    return path


def render_html(report: Report) -> str:
    """A report as one HTML page (figures as data URIs)."""
    w = report.words
    e = html.escape
    parts = [f"<h1>{e(report.title)}</h1>", f"<p class='sub'>{e(report.project)}</p>", _key_values(report.info)]
    if report.description:
        parts.append(f"<p><b>{e(w('description'))}</b><br>{e(report.description)}</p>")
    parts += [f"<h3>{e(w('chunks_title'))}</h3>", _table(report.chunks)]
    entries = []
    counter = [0]

    def anchor() -> str:
        counter[0] += 1
        return f"s{counter[0]}"

    body = []
    for top in report.sections:
        body.append(_section(top, anchor, entries, w))
    if entries:
        items = "".join(f"<li class='l{lvl}'><a href='#{a}'>{e(t)}</a></li>" for lvl, t, a in entries if lvl <= 1)
        parts += [f"<h3>{e(w('contents'))}</h3>", f"<nav><ul>{items}</ul></nav>"]
    doc = (f"<!doctype html><html lang='{report.language}'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{e(report.title)} - {e(report.project)}</title><style>{CSS}</style></head><body>"
           + "".join(parts) + "".join(body) + "</body></html>")
    return doc


def _section(s: Section, anchor, entries: list, words) -> str:
    a = anchor()
    entries.append((s.level, s.title, a))
    tag = {0: "h2", 1: "h3"}.get(s.level, "h4")
    out = [f"<{tag} id='{a}'>{html.escape(s.title)}</{tag}>"]
    for b in s.blocks:
        if isinstance(b, Section):
            out.append(_section(b, anchor, entries, words))
        elif isinstance(b, KeyValues):
            out.append(_key_values(b))
        elif isinstance(b, Table):
            out.append(_table(b))
        elif isinstance(b, Figure):
            out.append(_figure(b, words))
        elif isinstance(b, Paragraph):
            out.append(f"<p class='{'note' if b.note else ''}'>{html.escape(b.text)}</p>")
    return "".join(out)


def _key_values(kv: KeyValues) -> str:
    rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>" for k, v in kv.rows)
    return f"<table class='kv'>{rows}</table>"


def _table(t: Table) -> str:
    align = t.alignment()
    cls = ["r" if a == "R" else "" for a in align]
    head = "".join(f"<th class='{c}'>{html.escape(h)}</th>" for h, c in zip(t.headers, cls))
    body = "".join("<tr>" + "".join(f"<td class='{cls[i] if i < len(cls) else ''}'>{html.escape(str(v))}</td>"
                                    for i, v in enumerate(r)) + "</tr>" for r in t.rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _colour_bar(cb: ColourBar, words) -> str:
    cols = cb.colours(11)
    grad = ", ".join(f"rgb({r},{g},{b}) {k * 10}%" for k, (r, g, b) in enumerate(cols))
    span = (cb.high - cb.low) or 1.0
    decimals = cb.tick_decimals()
    ticks = "".join(f"<span style='left:{100 * (v - cb.low) / span:.2f}%'>{html.escape(words.number(v, decimals))}</span>"
                    for v in cb.ticks() if cb.low <= v <= cb.high)
    return (f"<div class='bar'><b>{html.escape(cb.label)}</b><div class='ramp' style='background:linear-gradient(to right,"
            f" {grad})'></div><div class='ticks'>{ticks}</div></div>")


def _legend(lg: Legend) -> str:
    items = "".join(f"<span><i style='background:rgb{tuple(c)}'></i>{html.escape(label)}</span>" for c, label in lg.items)
    return f"<div class='legend'>{items}</div>"


def _figure(f: Figure, words) -> str:
    src = f"data:{f.mime};base64,{base64.b64encode(f.data).decode()}"
    extra = ""
    if f.colour_bar is not None:
        extra += _colour_bar(f.colour_bar, words)
    if f.legend is not None:
        extra += _legend(f.legend)
    cap = f"<figcaption>{html.escape(f.caption)}</figcaption>" if f.caption else ""
    return f"<figure><img alt='{html.escape(f.caption)}' src='{src}'>{extra}{cap}</figure>"


__all__ = ["generate_report", "render_html"]
