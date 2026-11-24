"""What a command of the ``ppg`` command line printed, read as plain words by the tests of its messages."""
from __future__ import annotations

import re

_TERMINAL_CODES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")  # the colors and styles a terminal is told (CSI sequences)


def plain_words(output: str) -> str:
    """What a command printed, as plain words on one line. Typer prints help and usage errors in boxes that wrap the
    text and, where GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set (GitHub's runners), in forced colors that cut an
    option such as ``--base-position`` into styled pieces: the codes, the box and the line breaks are not what a test
    of the message is about."""
    return " ".join(re.sub("[─-╿]", " ", _TERMINAL_CODES.sub("", output)).split())
