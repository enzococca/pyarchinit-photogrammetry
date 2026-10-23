"""The processing report as a printable PDF (fpdf2).

A4 portrait: a title page (title, project, date, version of the program, the chunks of the report
and the contents with their pages), then every chunk from a new page - survey, calibration,
references and their errors, processing parameters, products, multispectral and thermal products,
measuring points (:mod:`io.report_content` collects them). Every page has a header (report,
project, chunk) and its number; the sections are bookmarks of the PDF. The text is DejaVu Sans
(Unicode: degrees, diameters, accents), the figures are small JPEGs with their colour bar, legend,
scale bar and north arrow drawn as vector graphics.

The pages of the contents are known only once the document is laid out: it is laid out twice,
the second time with the pages found the first time (the contents keep one line per entry, so
both layouts are the same).
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np

from .report_content import (ColourBar, Figure, KeyValues, Legend, Paragraph, Report, Section, Table, collect,
                             font_files, nice_length, walk)

ACCENT = (31, 78, 121)
TEXT_COLOUR = (35, 35, 35)
GREY = (115, 115, 115)
ZEBRA = (242, 245, 248)
RULE = (190, 198, 208)
MARGIN = 16.0
FAMILY = "ppgsans"
BODY, SMALL, TABLE = 9.0, 7.5, 7.6  # font sizes (points)
MAX_FIGURE_HEIGHT = 150.0  # mm
TOC_LINE = 5.4  # mm


def generate_pdf_report(target, path, language: str | None = None, chunks=None, progress=None,
                        title: str | None = None, description: str = "", index_palette: str = "rdylgn",
                        thermal_palette: str = "iron") -> Path:
    """The processing report of a :class:`Document` (its enabled chunks, or ``chunks``: objects,
    keys or labels) or of a :class:`Chunk` as a PDF at ``path``. ``language``: ``it`` or ``en``
    (default: the language of the interface, else of the system); ``title`` and
    ``description`` go on the title page; ``index_palette`` (``rdylgn``, ``grey``, ``viridis``,
    ``turbo``) colours the multispectral indices and ``thermal_palette`` (``iron``, ``inferno``,
    ``rainbow``, ``grey``) the temperatures. Returns the path written."""
    from ..core.progress import as_progress

    progress = as_progress(progress)
    report = collect(target, language=language, chunks=chunks, title=title, description=description,
                     index_palette=index_palette, thermal_palette=thermal_palette, progress=progress.sub(0.0, 0.85))
    progress(0.88, "Writing the PDF")
    out = write_pdf(report, path)
    progress(1.0, f"Report written: {out}")
    return out


def write_pdf(report: Report, path) -> Path:
    """``report`` laid out as a PDF at ``path`` (written to a temporary file, then moved)."""
    fonts = font_files()
    first = _render(report, fonts, None)
    pdf = _render(report, fonts, first.toc_pages)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        pdf.output(str(tmp))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _render(report: Report, fonts: dict, toc_pages: list[int] | None):
    pdf = _ReportPDF(report, fonts, toc_pages)
    pdf.title_page()
    for top in report.sections:
        pdf.section(top)
    return pdf


def _make_class():
    from fpdf import FPDF

    class ReportPDF(FPDF):
        """The layout of a :class:`Report` (see the module docstring)."""

        def __init__(self, report: Report, fonts: dict, toc_pages: list[int] | None):
            super().__init__(orientation="P", unit="mm", format="A4")
            self.report = report
            self.w_ = report.words
            self.toc_given = toc_pages  # pages of the entries of the contents (second layout)
            self.toc_pages: list[int] = []  # pages found while laying out
            self.chunk_label = ""
            self.has_unicode = bool(fonts.get(""))
            if self.has_unicode:
                regular = str(fonts[""])
                for style in ("", "B", "I", "BI"):
                    self.add_font(FAMILY, style, str(fonts.get(style) or fonts.get(style[:1]) or regular))
                self.text_family = FAMILY
            else:  # no Unicode font found: the core font, texts in Windows-1252
                self.text_family = "helvetica"
            self.set_margins(MARGIN, 20, MARGIN)
            self.set_auto_page_break(True, margin=17)
            self.alias_nb_pages()
            self.set_title(f"{report.title} - {report.project}")
            self.set_creator(f"pyarchinit-photogrammetry {report.version}")
            self.set_lang(report.language)
            self.set_text_color(*TEXT_COLOUR)
            self.c_margin = 1.2

        # -------------------------------------------------------------- text
        def t(self, text) -> str:
            text = "" if text is None else str(text)
            if self.has_unicode:
                return text
            text = text.replace("Δ", "d").replace("−", "-")  # the rest outside Windows-1252 becomes "?"
            return text.encode("cp1252", "replace").decode("cp1252")

        def use_font(self, size: float, style: str = "", colour=TEXT_COLOUR) -> None:
            self.set_font(self.text_family, style, size)
            self.set_text_color(*colour)

        def header(self) -> None:
            if self.page_no() == 1:
                return
            self.use_font(SMALL, "", GREY)
            self.set_xy(MARGIN, 9)
            left = f"{self.report.title} - {self.report.project}"
            self.cell(self.epw * 0.62, 5, self.t(self._fit(left, self.epw * 0.62)))
            self.cell(self.epw * 0.38, 5, self.t(self._fit(self.chunk_label, self.epw * 0.38)), align="R")
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(MARGIN, 14.5, self.w - MARGIN, 14.5)
            self.set_xy(MARGIN, 20)
            self.use_font(BODY)

        def footer(self) -> None:
            self.set_y(-12)
            self.use_font(SMALL, "", GREY)
            self.cell(self.epw / 2, 5, self.t(f"pyarchinit-photogrammetry {self.report.version}"))
            self.cell(self.epw / 2, 5, self.t(self.w_("page_of", self.page_no(), "{nb}")), align="R")

        def _fit(self, text: str, width: float) -> str:
            """``text`` cut to fit ``width`` mm in the current font, with an ellipsis."""
            text = self.t(text)
            if self.get_string_width(text) <= width:
                return text
            dots = "..."
            while text and self.get_string_width(text + dots) > width:
                text = text[:-1]
            return text + dots

        # -------------------------------------------------------------- title page
        def title_page(self) -> None:
            r, w = self.report, self.w_
            self.add_page()
            self.set_fill_color(*ACCENT)
            self.rect(0, 0, self.w, 9, style="F")
            self.set_y(28)
            self.use_font(24, "B", ACCENT)
            self.multi_cell(self.epw, 11, self.t(r.title), new_x="LMARGIN", new_y="NEXT")
            self.ln(1)
            self.use_font(15, "", TEXT_COLOUR)
            self.multi_cell(self.epw, 8, self.t(r.project), new_x="LMARGIN", new_y="NEXT")
            self.ln(4)
            self.draw_key_values(r.info)
            if r.description:
                self.ln(2)
                self.use_font(BODY, "B")
                self.cell(self.epw, 5.5, self.t(w("description")), new_x="LMARGIN", new_y="NEXT")
                self.use_font(BODY)
                self.multi_cell(self.epw, 4.6, self.t(r.description), new_x="LMARGIN", new_y="NEXT")
            self.ln(4)
            self.subtitle(w("chunks_title"))
            self.draw_table(r.chunks)
            self.ln(4)
            self.contents()

        def subtitle(self, text: str) -> None:
            self.use_font(11, "B", ACCENT)
            self.cell(self.epw, 7, self.t(text), new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(MARGIN, self.y, self.w - MARGIN, self.y)
            self.ln(1.5)

        def contents(self) -> None:
            """The entries of the contents (chunks and their sections), one line each, with their
            pages (unknown in the first layout: the same lines without numbers)."""
            entries = [s for s in walk(self.report.sections) if s.level <= 1]
            if not entries:
                return
            room = self.page_break_trigger - self.y - 9
            line = TOC_LINE
            if len(entries) * line > room:
                if len(entries) * 4.4 <= room:
                    line = 4.4  # closer lines: the contents stay on the title page
                else:
                    self.add_page()  # all of them together from the next page
            self.subtitle(self.w_("contents"))
            for i, s in enumerate(entries):
                if self.will_page_break(line):
                    self.add_page()
                page = self.toc_given[i] if self.toc_given is not None and i < len(self.toc_given) else None
                indent = 0.0 if s.level == 0 else 6.0
                self.use_font(9.5 if s.level == 0 else 9, "B" if s.level == 0 else "", TEXT_COLOUR)
                y = self.y
                link = self.add_link(page=page) if page else ""
                room = self.epw - indent - 16
                name = self._fit(s.title, room)
                self.set_x(MARGIN + indent)
                self.cell(self.get_string_width(name) + 1, line, name, link=link)
                end = self.x
                self.set_x(self.w - MARGIN - 14)
                self.cell(14, line, str(page) if page else "", align="R", link=link, new_x="LMARGIN",
                          new_y="NEXT")
                self.set_draw_color(*RULE)
                self.set_line_width(0.15)
                if self.w - MARGIN - 14 - end > 4:
                    self.set_dash_pattern(dash=0.4, gap=0.9)
                    self.line(end + 1, y + line * 0.72, self.w - MARGIN - 14, y + line * 0.72)
                    self.set_dash_pattern()

        # -------------------------------------------------------------- sections
        def _lead(self, s: Section) -> float:
            """Height the start of a section needs on its page: its heading and its first block (a
            figure can shrink, a table or a list starts with a few rows)."""
            head = {0: 0.0, 1: 15.0, 2: 9.0}.get(s.level, 9.0)
            first = s.blocks[0] if s.blocks else None
            if isinstance(first, Figure):
                return head + 80.0
            if isinstance(first, Section):
                return head + self._lead(first)
            if isinstance(first, (Table, KeyValues)):
                return head + 22.0
            return head + 10.0

        def section(self, s: Section) -> None:
            self.heading(s)
            for b in s.blocks:
                if isinstance(b, Section):
                    self.section(b)
                elif isinstance(b, KeyValues):
                    self.draw_key_values(b)
                elif isinstance(b, Table):
                    self.draw_table(b)
                elif isinstance(b, Figure):
                    self.figure(b)
                elif isinstance(b, Paragraph):
                    self.paragraph(b)
                self.ln(1.5)

        def heading(self, s: Section) -> None:
            if s.level == 0:
                self.chunk_label = s.title
                self.add_page()
                self.toc_pages.append(self.page_no())
                self.start_section(self.t(s.title), level=0)
                self.use_font(17, "B", ACCENT)
                self.multi_cell(self.epw, 9, self.t(s.title), new_x="LMARGIN", new_y="NEXT")
                self.set_draw_color(*ACCENT)
                self.set_line_width(0.6)
                self.line(MARGIN, self.y + 0.5, self.w - MARGIN, self.y + 0.5)
                self.ln(4)
                return
            if s.level == 1:
                if self.will_page_break(3 + self._lead(s)):
                    self.add_page()
                self.ln(3)
                self.toc_pages.append(self.page_no())
                self.start_section(self.t(s.title), level=1)
                self.use_font(13, "B", ACCENT)
                self.multi_cell(self.epw, 7, self.t(s.title), new_x="LMARGIN", new_y="NEXT")
                self.set_draw_color(*RULE)
                self.set_line_width(0.25)
                self.line(MARGIN, self.y + 0.3, self.w - MARGIN, self.y + 0.3)
                self.ln(2.5)
                return
            if self.will_page_break(1.5 + self._lead(s)):
                self.add_page()
            self.ln(1.5)
            self.start_section(self.t(s.title), level=2)
            self.use_font(10.5, "B", TEXT_COLOUR)
            self.multi_cell(self.epw, 6, self.t(s.title), new_x="LMARGIN", new_y="NEXT")
            self.ln(0.8)

        def paragraph(self, p: Paragraph) -> None:
            if p.note:
                self.use_font(8, "I", GREY)
                self.multi_cell(self.epw, 4.2, self.t(p.text), new_x="LMARGIN", new_y="NEXT")
            else:
                self.use_font(BODY)
                self.multi_cell(self.epw, 4.8, self.t(p.text), new_x="LMARGIN", new_y="NEXT")

        def draw_key_values(self, kv: KeyValues) -> None:
            if not kv.rows:
                return
            from fpdf.fonts import FontFace

            self.use_font(BODY, "B")
            label_w = min(70.0, max(self.get_string_width(self.t(k)) for k, _v in kv.rows) + 4)
            self.use_font(BODY)
            self.set_fill_color(255, 255, 255)  # the cells not striped take the colour in use
            with self.table(width=self.epw, col_widths=(label_w, self.epw - label_w), first_row_as_headings=False,
                            borders_layout="NONE", cell_fill_color=ZEBRA, cell_fill_mode="EVEN_ROWS",
                            line_height=4.7, text_align=("LEFT", "LEFT"), align="LEFT", padding=(0.6, 1.2)) as t:
                for k, v in kv.rows:
                    row = t.row()
                    row.cell(self.t(k), style=FontFace(emphasis="BOLD", color=(70, 70, 70)))
                    row.cell(self.t(v))

        def draw_table(self, tb: Table) -> None:
            if not tb.headers:
                return
            from fpdf.fonts import FontFace

            align = {"L": "LEFT", "R": "RIGHT", "C": "CENTER"}
            aligns = [align.get(a, "LEFT") for a in tb.alignment()]
            for cols in self._column_groups(tb):
                widths = self._widths(tb, cols)
                head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=ACCENT)
                self.use_font(TABLE)
                self.set_fill_color(255, 255, 255)  # the cells not striped take the colour in use
                with self.table(width=sum(widths), col_widths=tuple(widths), headings_style=head, align="LEFT",
                                borders_layout="NONE", cell_fill_color=ZEBRA, cell_fill_mode="ROWS",
                                line_height=4.3, text_align=tuple(aligns[c] for c in cols),
                                padding=(0.5, 1.2)) as t:
                    row = t.row()
                    for c in cols:
                        row.cell(self.t(tb.headers[c]))
                    for r in tb.rows:
                        row = t.row()
                        for c in cols:
                            row.cell(self.t(r[c] if c < len(r) else ""))
                self.ln(1)

        def _natural(self, tb: Table, c: int) -> tuple[float, float]:
            """Width of a column without wrapping its cells, and the least (the longest word)."""
            pad = 2 * 1.2 + 0.5
            self.use_font(TABLE, "B")
            head = self.get_string_width(self.t(tb.headers[c])) + pad
            head_word = max((self.get_string_width(self.t(x)) for x in str(tb.headers[c]).split()), default=0) + pad
            self.use_font(TABLE)
            cells = [self.t(r[c]) for r in tb.rows if c < len(r)]
            widest = max((self.get_string_width(v) for v in cells), default=0) + pad
            word = max((self.get_string_width(x) for v in cells for x in v.split()), default=0) + pad
            return max(head, widest, 9.0), max(head_word, word, 9.0)

        def _column_groups(self, tb: Table) -> list[list[int]]:
            """The columns in groups that fit the page (the first one repeated in every group)."""
            n = len(tb.headers)
            least = [self._natural(tb, c)[1] for c in range(n)]
            if sum(least) <= self.epw or n <= 2:
                return [list(range(n))]
            groups, cur, width = [], [0], least[0]
            for c in range(1, n):
                if width + least[c] > self.epw and len(cur) > 1:
                    groups.append(cur)
                    cur, width = [0], least[0]
                cur.append(c)
                width += least[c]
            groups.append(cur)
            return groups

        def _widths(self, tb: Table, cols: list[int]) -> list[float]:
            nat = [self._natural(tb, c) for c in cols]
            natural = [min(a, 75.0) for a, _b in nat]
            least = [min(b, 75.0) for _a, b in nat]
            if sum(natural) <= self.epw:
                return natural
            # shrink the columns that can wrap, never below their longest word
            spare = sum(natural) - self.epw
            slack = [a - b for a, b in zip(natural, least)]
            total = sum(slack) or 1.0
            out = [a - spare * s / total for a, s in zip(natural, slack)]
            out = [max(o, b) for o, b in zip(out, least)]
            if sum(out) > self.epw:
                k = self.epw / sum(out)
                out = [o * k for o in out]
            return out

        # -------------------------------------------------------------- figures
        def figure(self, f: Figure) -> None:
            ratio = f.height / max(f.width, 1)
            w = self.epw
            h = w * ratio
            if h > MAX_FIGURE_HEIGHT:
                h = MAX_FIGURE_HEIGHT
                w = h / ratio
            below = (12.0 if (f.colour_bar is not None or f.metres_per_pixel) else 0.0) + 6.0
            if f.legend is not None:
                below += 4.6 * self._legend_rows(f.legend, w)
            room = self.page_break_trigger - self.y - below
            if h > room:
                if room >= max(70.0, 0.55 * h):  # smaller, on this page
                    h = room
                    w = h / ratio
                else:
                    self.add_page()
            x = MARGIN + (self.epw - w) / 2
            y = self.y
            self.image(io.BytesIO(f.data), x=x, y=y, w=w, h=h)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.rect(x, y, w, h)
            if f.north:
                self.north_arrow(x + w - 6.5, y + 2.2)
            y2 = y + h + 1.8
            band = 0.0
            if f.metres_per_pixel:
                band = max(band, self.scale_bar(x, y2, w, f.width * f.metres_per_pixel))
            if f.colour_bar is not None:
                band = max(band, self.colour_bar(f.colour_bar, x, y2, w))
            y2 += band
            if f.legend is not None:
                y2 = self.legend(f.legend, x, y2 + 0.8, w)
            self.set_xy(MARGIN, y2 + 0.8)
            if f.caption:
                self.use_font(8, "I", GREY)
                self.multi_cell(self.epw, 4.0, self.t(f.caption), align="C", new_x="LMARGIN", new_y="NEXT")
            self.use_font(BODY)

        def north_arrow(self, x: float, y: float) -> None:
            """A white tile with an arrow pointing up and an N, at ``x``, ``y`` (its top left)."""
            self.set_fill_color(255, 255, 255)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.rect(x - 0.5, y - 0.5, 5.6, 9.6, style="DF")
            self.set_fill_color(30, 30, 30)
            cx = x + 2.3
            self.polygon([(cx, y + 3.4), (cx - 1.7, y + 8.4), (cx, y + 7.2), (cx + 1.7, y + 8.4)], style="F")
            self.use_font(6.5, "B", (30, 30, 30))
            self.set_xy(x - 0.5, y - 0.2)
            self.cell(5.6, 3.6, "N", align="C")

        def scale_bar(self, x: float, y: float, width_mm: float, ground: float) -> float:
            """A bar of a round length under the left of a figure ``width_mm`` wide showing
            ``ground`` metres; returns the height used."""
            length = nice_length(ground * 0.3)
            if length <= 0:
                return 0.0
            bar = width_mm * length / ground
            unit, k = ("cm", 100.0) if length < 1 else ("m", 1.0)
            if length >= 2000:
                unit, k = "km", 0.001
            self.set_line_width(0.15)
            self.set_draw_color(30, 30, 30)
            for i in range(4):
                self.set_fill_color(*((30, 30, 30) if i % 2 == 0 else (255, 255, 255)))
                self.rect(x + bar * i / 4, y + 3.6, bar / 4, 1.5, style="DF")
            self.use_font(6.5, "", (60, 60, 60))
            for frac in (0.0, 0.5, 1.0):
                v = length * frac * k
                text = self.w_.number(v, 0 if float(v).is_integer() else 1)
                if frac == 1.0:
                    text += f" {unit}"
                tw = self.get_string_width(text)
                self.set_xy(x + bar * frac - (tw / 2 if frac < 1.0 else tw / 2), y + 5.4)
                self.cell(tw, 3, text)
            return 9.0

        def colour_bar(self, cb: ColourBar, x: float, y: float, width_mm: float) -> float:
            """The ramp of a figure at the right of the band under it, with its label and ticks;
            returns the height used."""
            from PIL import Image

            w = min(95.0, width_mm * 0.55)
            x0 = x + width_mm - w
            self.use_font(7, "B", (60, 60, 60))
            self.set_xy(x0, y)
            self.cell(w, 3.2, self.t(cb.label))
            strip = Image.fromarray(np.repeat(cb.colours(512)[None, :, :], 8, axis=0), "RGB")
            buf = io.BytesIO()
            strip.save(buf, "PNG")
            self.image(io.BytesIO(buf.getvalue()), x=x0, y=y + 3.6, w=w, h=3.2)
            self.set_draw_color(120, 120, 120)
            self.set_line_width(0.15)
            self.rect(x0, y + 3.6, w, 3.2)
            self.use_font(6.5, "", (60, 60, 60))
            span = cb.high - cb.low
            decimals = cb.tick_decimals()
            for v in cb.ticks():
                if span <= 0:
                    break
                px = x0 + w * (v - cb.low) / span
                if px < x0 - 0.01 or px > x0 + w + 0.01:
                    continue
                self.line(px, y + 6.8, px, y + 7.6)
                text = self.w_.number(v, decimals)
                tw = self.get_string_width(text)
                self.set_xy(min(max(px - tw / 2, x0 - 2), x0 + w - tw + 2), y + 7.7)
                self.cell(tw, 3, text)
            return 11.2

        def _legend_rows(self, lg: Legend, width_mm: float) -> int:
            self.use_font(7.5)
            rows, used = 1, 0.0
            for _c, label in lg.items:
                item = 5.0 + self.get_string_width(self.t(label)) + 5.0
                if used + item > width_mm and used > 0:
                    rows += 1
                    used = 0.0
                used += item
            return rows

        def legend(self, lg: Legend, x: float, y: float, width_mm: float) -> float:
            """Swatches and labels in rows under a figure; returns the y below them."""
            self.use_font(7.5, "", (50, 50, 50))
            cx = x
            for colour, label in lg.items:
                text = self.t(label)
                item = 5.0 + self.get_string_width(text) + 5.0
                if cx + item > x + width_mm and cx > x:
                    cx = x
                    y += 4.6
                self.set_fill_color(*colour)
                self.set_draw_color(90, 90, 90)
                self.set_line_width(0.15)
                self.rect(cx, y + 0.6, 3.4, 3.0, style="DF")
                self.set_xy(cx + 4.4, y)
                self.cell(self.get_string_width(text) + 1, 4.2, text)
                cx += item
            return y + 4.6

    return ReportPDF


_CLASS = None


def _ReportPDF(report: Report, fonts: dict, toc_pages):  # noqa: N802 - a class made on first use
    global _CLASS
    if _CLASS is None:
        _CLASS = _make_class()
    return _CLASS(report, fonts, toc_pages)


def sample_pdf(path) -> Path:
    """A one-page PDF with the program's fonts, a table and a figure with its colour bar: the
    smoke test of the packaged application (fpdf2, fontTools and the fonts are there)."""
    from datetime import datetime

    from .report_content import DEM_STOPS, Words, _figure

    words = Words("en")
    rgb = np.dstack([np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (32, 1))] * 3)
    fig = _figure(rgb, caption="Check: ° Ø à", colour_bar=ColourBar(DEM_STOPS, 0.0, 1.0, "m"), metres_per_pixel=0.1)
    report = Report(words=words, title="Check", project="check", project_file="", created=datetime.now(), version="",
                    description="° Ø à ²", info=KeyValues([("a", "b")]), chunks=Table(["Chunk", "n"], [["c", "1"]]),
                    sections=[Section("Chunk", 0, [Section("Part", 1, [fig])])])
    return write_pdf(report, path)


__all__ = ["generate_pdf_report", "write_pdf", "sample_pdf"]
