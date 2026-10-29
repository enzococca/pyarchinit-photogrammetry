"""A word index over the guide, for the assistant.

The guide is the Markdown of ``docs/`` (the site).  A source checkout reads it from there; the installed program
reads the copy packaged in ``assistant/guide/`` (a wheel cannot include ``docs/``), made by::

    python -m pyarchinit_photogrammetry.assistant.knowledge --sync

Each page is split at its ``#``, ``##`` and ``###`` headings into passages of at most :data:`MAX_PASSAGE`
characters, and :class:`Index` finds the passages that best answer a question by the words they share with it
(tf-idf with a length normalisation; case, accents and the plural or the final vowel do not matter).  Nothing here
needs a model or a library outside the standard one.
"""
from __future__ import annotations

import argparse
import math
import re
import shutil
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parents[2]  # <checkout>/src/pyarchinit_photogrammetry/assistant -> <checkout>
MAX_PASSAGE = 1200  # characters of one passage
LANGUAGES = ("it", "en")
# folders of docs/ that are not guide: the plans of the development, and the notes of a release (written in Italian,
# without the suffix of a translation, and no answer to a "how do I")
SKIPPED_FOLDERS = ("superpowers", "releases")

# words that open a question or join a sentence and say nothing about its subject
STOPWORDS = {
    "it": set("""a ad al alla alle allo agli ai anche che chi ci come con cosa cui da dal dalla dalle dei del della
        delle dello degli di dove e ed essere fa faccio fare fai fanno ha ho hanno i il in io la le lo gli ma mi ne
        nel nella
        nelle nello nei non o per perche piu poi puo posso possibile qual quale quali quando quanto se si sono su sul
        sulla sulle sullo sui tra fra tu un una uno unico usa uso usare utilizzare vorrei voglio""".split()),
    "en": set("""a an and are as at be by can could do does for from how i if in is it its my of on or so that the this
        to use using want what when where which with would you your""".split()),
}
_WORD = re.compile(r"[a-z0-9]+(?:[_.][a-z0-9]+)*")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)(\{[^}]*\})?")
_ATTR_ID = re.compile(r"\{[^}]*#([\w-]+)[^}]*\}\s*$")  # "## Title { #anchor }" of attr_list


@dataclass
class Passage:
    """A piece of a guide page: ``title`` is its heading, ``page`` the path of the page relative to the guide folder,
    ``anchor`` the heading's anchor on the site, ``score`` how well it answers the query (0 when not from a search)."""

    title: str
    page: str
    anchor: str
    text: str
    score: float
    lang: str


# ------------------------------------------------------------------ words
def fold(text: str) -> str:
    """Lower case without accents (``Qualità`` -> ``qualita``)."""
    decomposed = unicodedata.normalize("NFKD", text.replace("’", "'"))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _stem(word: str, lang: str) -> str:
    # the plural and the gender: the last vowel of an Italian word, the 's' of an English one
    if lang == "it":
        if len(word) > 4 and word[-1] in "aeio":
            return word[:-1]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str, lang: str) -> list[str]:
    """The words of a text for the index: folded, without the stop words and with the plural taken off.  An
    identifier such as ``selected_polygon`` or ``chunk.build_dem`` is kept whole and also as its parts."""
    stop = STOPWORDS.get(lang, ())
    out: list[str] = []
    for word in _WORD.findall(fold(text)):
        parts = re.split(r"[_.]", word)
        for w in ([word] + parts) if len(parts) > 1 else parts:
            if w and w not in stop and not (len(w) == 1 and not w.isdigit()):
                out.append(_stem(w, lang))
    return out


def slug(heading: str) -> str:
    """The anchor MkDocs (Python-Markdown's ``toc``) gives a heading: accents dropped, punctuation removed, lower
    case, runs of spaces and hyphens one hyphen."""
    value = unicodedata.normalize("NFKD", heading).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def _plain(heading: str) -> str:
    """A heading without its Markdown: links, emphasis, code marks, HTML."""
    heading = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"\{[^}]*\}\s*$", "", heading)  # {#id} of attr_list
    return re.sub(r"[`*_]{1,3}(?=\S)|(?<=\S)[`*_]{1,3}", "", heading).strip()


# ------------------------------------------------------------------ pages
def _chunks(text: str) -> list[str]:
    """``text`` as pieces of at most MAX_PASSAGE characters, cut at the blank lines between paragraphs (at a line
    when one paragraph is longer, anywhere when one line is)."""
    text = text.strip()
    if len(text) <= MAX_PASSAGE:
        return [text] if text else []
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if len(paragraph) <= MAX_PASSAGE:
            pieces.append(paragraph)
            continue
        current = ""
        for line in paragraph.splitlines():
            while len(line) > MAX_PASSAGE:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.append(line[:MAX_PASSAGE])
                line = line[MAX_PASSAGE:]
            if current and len(current) + 1 + len(line) > MAX_PASSAGE:
                pieces.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            pieces.append(current)
    packed: list[str] = []
    for piece in pieces:  # small paragraphs together again, up to the limit
        if packed and len(packed[-1]) + 2 + len(piece) <= MAX_PASSAGE:
            packed[-1] = f"{packed[-1]}\n\n{piece}"
        else:
            packed.append(piece)
    return packed


def split_page(markdown: str, page: str, lang: str) -> list[Passage]:
    """The passages of one page: one per section (``#`` to ``###``), long sections cut at paragraph boundaries.
    A ``#`` inside a code fence is a comment, not a heading."""
    sections: list[tuple[str, str, list[str]]] = []  # (title, anchor, lines)
    seen: dict[str, int] = {}
    title, anchor, lines = Path(page).name.split(".")[0], "", []
    fenced = False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            sections.append((title, anchor, lines))
            title = _plain(match.group(2))
            explicit = _ATTR_ID.search(match.group(2))
            if explicit:  # an anchor given by the page
                anchor = explicit.group(1)
                seen[anchor] = seen.get(anchor, 0) + 1
            else:
                base = slug(title)
                n = seen.get(base, 0)
                seen[base] = n + 1
                anchor = base if n == 0 else f"{base}_{n}"  # the second "Example" of a page is example_1
            lines = []
        else:
            lines.append(line)
    sections.append((title, anchor, lines))
    out = []
    for title, anchor, lines in sections:
        body = _IMAGE.sub("", "\n".join(lines))
        for piece in _chunks(body):
            out.append(Passage(title, page, anchor, piece, 0.0, lang))
    return out


class Index:
    """The passages of the guide in one language and the words they hold."""

    def __init__(self, passages: list[Passage], lang: str = "it"):
        self.lang = lang
        self.passages = passages
        self._postings: dict[str, list[tuple[int, float]]] = {}  # word -> [(passage, weight of its frequency)]
        lengths = []
        for i, p in enumerate(passages):
            words = tokens(p.text, lang)
            heading = tokens(p.title, lang)
            counts: dict[str, float] = {}
            for w in words:
                counts[w] = counts.get(w, 0.0) + 1.0
            for w in heading:  # a word of the heading weighs as three in the text
                counts[w] = counts.get(w, 0.0) + 3.0
            lengths.append(len(words) + len(heading))
            for w, c in counts.items():
                self._postings.setdefault(w, []).append((i, 1.0 + math.log(c)))
        mean = (sum(lengths) / len(lengths)) if lengths else 1.0
        # long passages say more words by chance: their weights are scaled (half the effect of a full normalisation)
        self._norm = [0.5 + 0.5 * (n / mean if mean else 1.0) for n in lengths]
        self._idf = {w: math.log(1.0 + len(passages) / len(post)) for w, post in self._postings.items()}

    @staticmethod
    def pages(folder: Path, lang: str) -> list[Path]:
        """The pages of a language: ``*.it.md`` for Italian, the ``*.md`` without a language suffix for English."""
        if lang not in LANGUAGES:
            raise ValueError(f"lang: one of {LANGUAGES}")
        folder = Path(folder)
        found = []
        for path in folder.rglob("*.md"):
            parts = path.relative_to(folder).parts
            if any(part in SKIPPED_FOLDERS or part.startswith(".") for part in parts[:-1]):
                continue
            if path.name.endswith(".it.md") == (lang == "it"):
                found.append(path)
        return sorted(found)

    @classmethod
    def build(cls, folder: Path, lang: str = "it") -> "Index":
        folder = Path(folder)
        passages: list[Passage] = []
        for path in cls.pages(folder, lang):
            relative = path.relative_to(folder).as_posix()
            passages.extend(split_page(path.read_text(encoding="utf-8"), relative, lang))
        return cls(passages, lang)

    def search(self, query: str, k: int = 5) -> list[Passage]:
        """The ``k`` passages that share the most (and the rarest) words with ``query``, best first."""
        scores: dict[int, float] = {}
        for word in set(tokens(query, self.lang)):
            idf = self._idf.get(word)
            if idf is None:
                continue
            for i, weight in self._postings[word]:
                scores[i] = scores.get(i, 0.0) + weight * idf
        ranked = sorted(((s / self._norm[i], i) for i, s in scores.items()), key=lambda t: (-t[0], t[1]))
        return [replace(self.passages[i], score=round(s, 4)) for s, i in ranked[:max(0, k)]]


# ------------------------------------------------------------------ the guide folder
def guide_folder() -> Path:
    """``docs/`` of a source checkout, else the guide packaged with the program."""
    docs = REPO / "docs"
    if (docs / "index.md").is_file() and (REPO / "pyproject.toml").is_file():
        return docs
    return PACKAGE / "guide"


def _guide_files(folder: Path) -> list[Path]:
    return sorted(p for p in Path(folder).rglob("*.md")
                  if not any(part in SKIPPED_FOLDERS or part.startswith(".")
                             for part in p.relative_to(folder).parts[:-1]))


def sync_guide(src: Path = REPO / "docs", dst: Path = PACKAGE / "guide") -> int:
    """Copy the Markdown of ``src`` (not the images, not the development plans) to ``dst`` with its folders, and
    remove what ``src`` no longer has.  Returns the number of pages copied."""
    src, dst = Path(src), Path(dst)
    files = _guide_files(src)
    wanted = {p.relative_to(src) for p in files}
    dst.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        for old in _guide_files(dst):
            if old.relative_to(dst) not in wanted:
                old.unlink()
        for folder in sorted((d for d in dst.rglob("*") if d.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()
    for path in files:
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != path.read_bytes():
            shutil.copyfile(path, target)
    return len(files)


_INDEXES: dict[str, Index] = {}


def default_index(lang: str) -> Index:
    """The index of the guide in ``lang`` ("it" or "en"), built once per language."""
    if lang not in _INDEXES:
        _INDEXES[lang] = Index.build(guide_folder(), lang)
    return _INDEXES[lang]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pyarchinit_photogrammetry.assistant.knowledge",
                                     description="The guide index of the assistant.")
    parser.add_argument("--sync", action="store_true", help="copy docs/*.md into assistant/guide/")
    parser.add_argument("query", nargs="*", help="search the guide")
    parser.add_argument("--lang", default="it", choices=LANGUAGES)
    args = parser.parse_args(argv)
    if args.sync:
        print(f"{sync_guide()} pages copied to {PACKAGE / 'guide'}")
    if args.query:
        for p in default_index(args.lang).search(" ".join(args.query)):
            print(f"{p.score:7.2f}  {p.page}#{p.anchor}  {p.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
