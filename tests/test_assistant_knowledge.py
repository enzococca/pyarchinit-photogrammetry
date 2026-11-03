"""The word index over the guide: what a question finds, the size of the passages, the packaged copy of the guide."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from pyarchinit_photogrammetry.assistant import knowledge as K

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _pages_naming(lang: str, words: str) -> set[str]:
    return {p.relative_to(DOCS).as_posix() for p in K.Index.pages(DOCS, lang)
            if words in p.read_text(encoding="utf-8").lower()}


def test_search_finds_the_page_of_a_question():
    it = K.default_index("it").search("come faccio le curve di livello")
    assert it and all(p.lang == "it" for p in it)
    # the first passage is about contour lines, on a page that names them; the drawing tools are near the top
    assert it[0].page in _pages_naming("it", "curve di livello"), [p.page for p in it]
    assert "curve di livello" in (it[0].title + " " + it[0].text).lower()
    assert any(p.page == "gui/drawing.it.md" for p in it[:3]), [p.page for p in it]
    en = K.default_index("en").search("how do I make contour lines")
    assert en and all(p.lang == "en" and not p.page.endswith(".it.md") for p in en)
    assert en[0].page in _pages_naming("en", "contour line"), [p.page for p in en]
    assert any(p.page == "gui/drawing.md" for p in en[:3]), [p.page for p in en]
    # the English twin of the Italian answer: the same page without the suffix
    assert it[0].page.replace(".it.md", ".md") in [p.page for p in en[:3]]


def test_passages_are_bounded_and_carry_their_heading():
    for lang in ("it", "en"):
        index = K.default_index(lang)
        assert len(index.passages) > 500
        for p in index.passages:
            assert 0 < len(p.text) <= K.MAX_PASSAGE, (p.page, p.title, len(p.text))
            assert p.title and p.page and p.lang == lang
            assert p.page.endswith(".it.md") == (lang == "it")
            assert "superpowers" not in p.page and not p.page.startswith("releases/")
    # a heading and its anchor, as MkDocs makes them
    p = next(p for p in K.default_index("en").passages if p.page == "gui/panes.md" and p.title == "Python console")
    assert p.anchor == "python-console"
    assert K.slug("Tools > Target") == "tools-target"
    assert K.slug("Qualità e `code`, 2.0!") == "qualita-e-code-20"


def test_headings_inside_code_are_comments_and_long_sections_are_cut():
    md = ("# Title\n\nIntro.\n\n```python\n# not a heading\nx = 1\n```\n\n## Second\n\n"
          + "\n\n".join(["word " * 150] * 5))
    passages = K.split_page(md, "x.md", "en")
    assert [p.title for p in passages][:2] == ["Title", "Second"]
    assert "# not a heading" in passages[0].text
    second = [p for p in passages if p.title == "Second"]
    assert len(second) >= 3 and all(len(p.text) <= K.MAX_PASSAGE and p.anchor == "second" for p in second)
    # one paragraph, one line longer than the limit
    assert all(len(p.text) <= K.MAX_PASSAGE for p in K.split_page("# T\n\n" + "x" * 5000, "y.md", "en"))
    # the same heading twice gets MkDocs's second anchor
    twice = K.split_page("# A\n\none\n\n## Example\n\ntwo\n\n## Example\n\nthree\n", "z.md", "en")
    assert [p.anchor for p in twice if p.title == "Example"] == ["example", "example_1"]
    # an anchor the page gives with attr_list is the one of the site
    given = K.split_page("## Che cosa serve prima { #before }\n\ntesto\n", "w.it.md", "it")
    assert [(p.title, p.anchor) for p in given] == [("Che cosa serve prima", "before")]


def test_underscores_inside_words_stay_in_titles_and_anchors():
    # a code span keeps its text; an underscore inside a word is a letter to MkDocs (ids checked with Python-Markdown's
    # toc: `windowselected_polygon`, `using-chunkbuild_dem`), emphasis marks are only those at a word boundary
    code = K.split_page("## `window.selected_polygon()`\n\nx\n", "s.md", "en")
    assert [(p.title, p.anchor) for p in code] == [("window.selected_polygon()", "windowselected_polygon")]
    using = K.split_page("## Using `chunk.build_dem`\n\nx\n", "s.md", "en")
    assert [(p.title, p.anchor) for p in using] == [("Using chunk.build_dem", "using-chunkbuild_dem")]
    assert K._plain("*emph* and _under_ and snake_case word") == "emph and under and snake_case word"
    assert K._plain("**bold** and __dunder__ `a_b` ***x***") == "bold and dunder a_b x"
    assert K._plain("snake_case_word _it_") == "snake_case_word it"
    # the heading words (the identifier whole and in parts) match a query with the identifier
    heading, query = K.tokens("window.selected_polygon()", "en"), K.tokens("selected_polygon", "en")
    assert "window.selected_polygon" in heading and {"selected", "polygon"} <= set(heading) & set(query)


def test_accents_and_case_do_not_matter():
    index = K.default_index("it")
    plain = index.search("qualita")
    accented = index.search("QUALITÀ")
    assert plain and plain[0].text == accented[0].text
    assert any("qualità" in p.text.lower() for p in plain)
    assert K.fold("Qualità") == "qualita"


def test_index_builds_fast_for_both_languages():
    start = time.perf_counter()
    for lang in ("it", "en"):
        K.Index.build(K.guide_folder(), lang)
    assert time.perf_counter() - start < 2.0


def test_scripting_page_is_indexed():
    for lang, query in (("it", "selected_polygon"), ("en", "selected_polygon")):
        found = K.default_index(lang).search(query)
        assert found and any(p.page.startswith("scripting") for p in found[:3]), [(p.page, p.title) for p in found]
    it = K.default_index("it").search("come lancio uno script Python")
    assert any(p.page == "scripting.it.md" for p in it)
    assert not (set(K.Index.pages(DOCS, "it")) & set(K.Index.pages(DOCS, "en")))


def test_guide_folder_of_a_checkout_is_docs_and_the_package_has_its_copy():
    assert K.guide_folder() == DOCS
    assert (K.PACKAGE / "guide" / "scripting.md").is_file()


def test_packaged_guide_is_in_sync():
    packaged = K.PACKAGE / "guide"
    source = {p.relative_to(DOCS) for p in K._guide_files(DOCS)}
    copy = {p.relative_to(packaged) for p in K._guide_files(packaged)}
    how = "run: python -m pyarchinit_photogrammetry.assistant.knowledge --sync   (then commit the guide folder)"
    assert source == copy, f"the packaged guide differs from docs/ ({sorted(map(str, source ^ copy))[:5]}): {how}"
    stale = [str(r) for r in sorted(source) if (DOCS / r).read_bytes() != (packaged / r).read_bytes()]
    assert not stale, f"packaged pages out of date {stale[:5]}: {how}"


def test_sync_copies_pages_and_removes_what_is_gone(tmp_path):
    src, dst = tmp_path / "docs", tmp_path / "guide"
    (src / "gui").mkdir(parents=True)
    (src / "superpowers").mkdir()
    (src / "releases").mkdir()
    (src / "index.md").write_text("# A\n")
    (src / "gui" / "b.it.md").write_text("# B\n")
    (src / "image.png").write_bytes(b"x")
    (src / "superpowers" / "plan.md").write_text("# no\n")
    (src / "releases" / "v1.md").write_text("# no\n")
    (dst / "old").mkdir(parents=True)
    (dst / "old" / "gone.md").write_text("# gone\n")
    assert K.sync_guide(src, dst) == 2
    assert sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()) == ["gui/b.it.md", "index.md"]
    assert not (dst / "old").exists()
    assert K.sync_guide(src, dst) == 2  # again: nothing changes


def test_sync_refuses_a_source_that_is_not_the_guide_and_keeps_the_copy(tmp_path, monkeypatch, capsys):
    # an installed program has no docs/: the sync must not empty the packaged guide
    src, dst = tmp_path / "lib" / "docs", tmp_path / "guide"
    (dst / "gui").mkdir(parents=True)
    (dst / "index.md").write_text("# kept\n")
    (dst / "gui" / "b.it.md").write_text("# kept\n")
    before = {p.relative_to(dst).as_posix(): p.read_bytes() for p in dst.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError, match="not the guide"):
        K.sync_guide(src, dst)
    src.mkdir(parents=True)  # a folder without index.md is not the guide either
    (src / "stray.md").write_text("# no\n")
    with pytest.raises(RuntimeError, match="index.md"):
        K.sync_guide(src, dst)
    assert {p.relative_to(dst).as_posix(): p.read_bytes() for p in dst.rglob("*") if p.is_file()} == before
    # the command: exit code 1, a clear message, the copy untouched
    monkeypatch.setattr(K, "REPO", tmp_path / "lib")
    monkeypatch.setattr(K, "PACKAGE", tmp_path)
    assert K.main(["--sync"]) == 1
    assert "not the guide" in capsys.readouterr().err
    assert {p.relative_to(dst).as_posix(): p.read_bytes() for p in dst.rglob("*") if p.is_file()} == before
