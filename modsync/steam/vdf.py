"""Minimal Valve KeyValues (VDF/ACF) text parser.

Steam's ``libraryfolders.vdf`` and ``appmanifest_*.acf`` use this format::

    "key"   "value"
    "key"   { ... nested ... }

We only ever *read* these files, so this is a small tokenising parser. It
tolerates spaces/parens in values (e.g. prefix dirs named ``pfx (last camp)``)
and the usual ``\\"`` / ``\\\\`` escapes, and skips ``//`` line comments.
Binary KeyValues are not supported (Steam doesn't use them for these files).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Union

KV = dict[str, Union[str, "KV"]]

# Order matters: braces and comments are matched before the "bare word" branch,
# and the bare branch explicitly excludes braces/quotes so it can't swallow them.
_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|(//[^\n]*)|([^\s{}"]+)')


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(text):
        quoted, brace, comment, bare = m.groups()
        if comment is not None:
            continue
        if quoted is not None:
            tokens.append(("str", _unescape(quoted)))
        elif brace:
            tokens.append(("brace", brace))
        elif bare is not None:
            tokens.append(("str", bare))
    return tokens


def loads(text: str) -> KV:
    tokens = _tokenize(text)
    pos = 0

    def parse_block() -> KV:
        nonlocal pos
        obj: KV = {}
        while pos < len(tokens):
            typ, val = tokens[pos]
            if typ == "brace" and val == "}":
                pos += 1
                return obj
            key = val
            pos += 1
            if pos >= len(tokens):
                break
            ntyp, nval = tokens[pos]
            if ntyp == "brace" and nval == "{":
                pos += 1
                obj[key] = parse_block()
            else:
                obj[key] = nval
                pos += 1
        return obj

    root: KV = {}
    while pos < len(tokens):
        typ, val = tokens[pos]
        if typ == "brace":
            pos += 1
            continue
        key = val
        pos += 1
        if pos < len(tokens) and tokens[pos] == ("brace", "{"):
            pos += 1
            root[key] = parse_block()
        elif pos < len(tokens):
            root[key] = tokens[pos][1]
            pos += 1
    return root


def load(path: Path | str) -> KV:
    return loads(Path(path).read_text(encoding="utf-8", errors="replace"))
