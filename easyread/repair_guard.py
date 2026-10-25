"""Conservative formula repair guard, adapted from upstream 34e6295.

Unlike upstream's non-empty-string check, accept only math delimiter additions in
text fields. All other characters, list positions, keys and scalar values must
remain unchanged. Reject uncertain repairs rather than guessing equivalence.
"""
import re

from .codeblocks import CODE as _CODE

from .tables import MATRICES, FOOTNOTES

_DELIMITERS = re.compile(r"(?<!\\)(?:\$|\\[\[\]()])")
_TEXT = {"en", "zh", "tex", "caption_en", "caption_zh", "image_en", "image_zh",
         "head", "rows", "items", *MATRICES, *FOOTNOTES}
_EXACT = {"id", "type", "page", "source", "references", "meta", "checks"}


def _text_content(text):
    # Protect literal code, including dollar signs inside fenced/inline code.
    pieces, pos = [], 0
    for match in _CODE.finditer(text):
        pieces.append(_DELIMITERS.sub("", text[pos:match.start()]))
        pieces.append(match.group())
        pos = match.end()
    pieces.append(_DELIMITERS.sub("", text[pos:]))
    return "".join(pieces)


def _delimiter_extension(original, repaired):
    # Only insert complete delimiter tokens. In particular, dropping a dollar
    # sign from a price must not masquerade as a formula repair.
    def slots(text):
        result, removed = {}, 0
        for token in _DELIMITERS.finditer(text):
            result.setdefault(token.start() - removed, []).append(token.group())
            removed += len(token.group())
        return result
    added = slots(repaired)
    for offset, tokens in slots(original).items():
        available = iter(added.get(offset, []))
        if not all(any(candidate == token for candidate in available) for token in tokens):
            return False
    return True


def _retains(original, repaired, text=False, frozen=False):
    if type(original) is not type(repaired):
        return False
    if isinstance(original, dict):
        if original.keys() != repaired.keys():
            return False
        return all(_retains(value, repaired[key], text or key in _TEXT,
                            frozen or key in _EXACT or "hash" in key or key.startswith("source"))
                   for key, value in original.items())
    if isinstance(original, list):
        return len(original) == len(repaired) and all(
            _retains(a, b, text, frozen) for a, b in zip(original, repaired))
    if isinstance(original, str) and text and not frozen:
        return (_text_content(original) == _text_content(repaired)
                and _delimiter_extension(original, repaired))
    return original == repaired


def retains_content(original, repaired):
    """Validate before normalization, so normalization cannot hide lost fields."""
    return isinstance(original, dict) and _retains(original, repaired)
