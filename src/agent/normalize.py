"""Turn a raw Ukrainian question string into a NormalizedQuery.

Pure functions only: no I/O, no network, no store access. Ukrainian
inflection is handled by generic truncation stemming, not a per-word-form
dictionary — residual mismatches are expected and are absorbed later by
resolve.py's fuzzy match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .store import normalize_order_number, normalize_size

STEM_PREFIX_LEN = 5

_CYRILLIC_WORD_RE = re.compile(r"^[а-яіїєґ]+$")
_TOKEN_RE = re.compile(r"\d+-\d+|\d+|[a-zа-яіїєґ']+")
_HASH_ORDER_RE = re.compile(r"#\s*(\d+)")

ORDER_KEYWORD_ROOTS = ("замовл", "номер")
SIZE_KEYWORD_ROOT = "розмір"
QUANTITY_DISQUALIFIER_ROOTS = ("євро", "грн", "гривен", "відсот", "літр")
# Unit abbreviations that are exact tokens rather than word prefixes, e.g.
# "35 л" (35 litres) written short instead of "35 літрів".
QUANTITY_DISQUALIFIER_TOKENS = frozenset({"л"})

# Homoglyph guard: a customer (especially via voice-to-text) may type a
# Cyrillic look-alike instead of the Latin letter store.json actually uses
# for size codes. Scoped to short tokens that would then exactly match a
# known size code, so it never touches ordinary Cyrillic words. Note this
# only rescues letters that LOOK alike (М -> M); sizes typed by
# transliteration (ХЛ meaning XL) are handled by CYRILLIC_SIZE_CODES below.
_CYRILLIC_LATIN_CONFUSABLES = {
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
}

# Size codes typed in pure Cyrillic by transliteration, not homoglyph:
# "розмір ХЛ" means XL, "розмір С" means S. Checked as whole tokens only,
# and never for a token that directly follows a digit — "35 л" is litres
# and "2 м" is metres, not sizes (see _extract_size).
CYRILLIC_SIZE_CODES: dict[str, str] = {
    "с": "S", "м": "M", "л": "L",
    "хс": "XS", "хл": "XL", "ххл": "XXL",
}

# Month/year context: a 4-digit token next to these is a date, not an order
# number ("замовлення від 12 серпня 2026" must not become order #2026).
MONTH_ROOTS = ("січ", "лют", "берез", "квіт", "трав", "черв",
               "лип", "серп", "верес", "жовт", "листоп", "груд")
YEAR_WORD_ROOTS = ("рок", "рік")

# Closed, small colour vocabulary matching store.json's variant colours.
# Same spirit as store.py's LETTER_SIZES/ONE_SIZE_TOKENS tables — a fixed
# enumerable palette translation, not the open-ended inflection dictionary
# the spec prohibits. Matched via startswith so any case form collapses:
# "синьому".startswith("син") etc.
COLOUR_ROOTS: dict[str, str] = {
    "син": "navy",
    "беж": "beige",
    "чорн": "black",
    "оливков": "olive",
    "сір": "grey",
    "червон": "red",
    "іржав": "rust",
    "сталев": "steel",
    "navy": "navy",
    "beige": "beige",
    "black": "black",
    "olive": "olive",
    "grey": "grey",
    "gray": "grey",
    "red": "red",
    "rust": "rust",
    "steel": "steel",
}

# Closed cardinal-word table (1..10), oblique case forms included. Additive
# only: a matched token still passes through to `tokens` unchanged (aside
# from the normal stemming every Cyrillic word gets), so it keeps
# contributing to product resolution — e.g. "набір із трьох" must still
# resolve to the socks product from "трьох", not lose it.
CARDINALS: dict[str, int] = {
    "один": 1, "одна": 1, "одне": 1, "одного": 1, "одній": 1, "одну": 1, "одним": 1,
    "два": 2, "дві": 2, "двох": 2, "двома": 2,
    "три": 3, "трьох": 3, "трьома": 3,
    "чотири": 4, "чотирьох": 4, "чотирма": 4,
    "п'ять": 5, "п'яти": 5, "п'ятьох": 5, "п'ятьма": 5,
    "шість": 6, "шести": 6, "шістьох": 6, "шістьма": 6,
    "сім": 7, "семи": 7, "сімох": 7, "сімома": 7,
    "вісім": 8, "восьми": 8, "вісьмох": 8, "вісьмома": 8,
    "дев'ять": 9, "дев'яти": 9, "дев'ятьох": 9, "дев'ятьма": 9,
    "десять": 10, "десяти": 10, "десятьох": 10, "десятьма": 10,
}


@dataclass(frozen=True, slots=True)
class NormalizedQuery:
    text: str
    tokens: tuple[str, ...]
    numbers: tuple[int, ...]
    order_number: str | None
    size: str | None
    colour: str | None


def _clean_text(question: str) -> str:
    unified_apostrophes = question.replace("’", "'").replace("ʼ", "'")
    lowered = unified_apostrophes.lower()
    return re.sub(r"\s+", " ", lowered).strip()


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _is_cyrillic_word(token: str) -> bool:
    return bool(_CYRILLIC_WORD_RE.match(token))


def _stem(token: str) -> str:
    if _is_cyrillic_word(token) and len(token) > STEM_PREFIX_LEN:
        return token[:STEM_PREFIX_LEN]
    return token


def _confusable_translate(token: str) -> str:
    return "".join(_CYRILLIC_LATIN_CONFUSABLES.get(ch, ch) for ch in token.upper())


def _looks_like_year(token: str) -> bool:
    return len(token) == 4 and token.isdigit() and 1900 <= int(token) <= 2099


def _has_date_context(raw_tokens: list[str]) -> bool:
    return any(
        token.startswith(root)
        for token in raw_tokens
        for root in MONTH_ROOTS + YEAR_WORD_ROOTS
    )


def _extract_order_number(text: str, raw_tokens: list[str]) -> tuple[str | None, str | None]:
    hash_match = _HASH_ORDER_RE.search(text)
    if hash_match:
        digits = hash_match.group(1)
        return normalize_order_number(digits), digits

    has_keyword = any(
        token.startswith(root) for token in raw_tokens for root in ORDER_KEYWORD_ROOTS
    )
    if has_keyword:
        date_context = _has_date_context(raw_tokens)
        for token in raw_tokens:
            # 4-8 digits: realistic order-number lengths. Longer digit runs
            # are phone or card numbers ("оплата карткою 1234... за
            # замовлення") and must not become an order lookup.
            if token.isdigit() and 4 <= len(token) <= 8:
                # "замовлення від 12 серпня 2026" — the year is a date,
                # not an order number.
                if date_context and _looks_like_year(token):
                    continue
                return normalize_order_number(token), token

    return None, None


def _extract_size(raw_tokens: list[str], consumed_order_digits: str | None) -> str | None:
    for idx, token in enumerate(raw_tokens):
        # A short letter token right after a digit is a unit suffix, not a
        # size: "35l"/"35 л" is litres, "1l" is one litre, "2 м" is metres.
        if len(token) <= 3 and idx > 0 and raw_tokens[idx - 1].isdigit():
            continue
        if token in CYRILLIC_SIZE_CODES:
            return CYRILLIC_SIZE_CODES[token]
        candidate = _confusable_translate(token) if len(token) <= 3 else token
        canonical, kind, _ = normalize_size(candidate)
        if kind in ("letter", "range", "one"):
            return canonical

    numeric_candidates: list[tuple[int, str]] = []
    for idx, token in enumerate(raw_tokens):
        if token == consumed_order_digits:
            continue
        canonical, kind, _ = normalize_size(token)
        if kind == "numeric":
            numeric_candidates.append((idx, canonical))

    if not numeric_candidates:
        return None

    keyword_idx = next(
        (i for i, t in enumerate(raw_tokens) if t.startswith(SIZE_KEYWORD_ROOT)),
        None,
    )
    if keyword_idx is not None:
        return min(numeric_candidates, key=lambda pair: abs(pair[0] - keyword_idx))[1]

    if len(numeric_candidates) == 1:
        has_disqualifier = any(
            token in QUANTITY_DISQUALIFIER_TOKENS
            or any(token.startswith(root) for root in QUANTITY_DISQUALIFIER_ROOTS)
            for token in raw_tokens
        )
        if not has_disqualifier:
            return numeric_candidates[0][1]

    return None


# Words that share a colour root's prefix but are not colours: "синтетичний"
# starts with "син" yet does not mean navy.
COLOUR_EXCLUDE_PREFIXES = ("синт",)


def _extract_colour(raw_tokens: list[str]) -> str | None:
    for token in raw_tokens:
        if any(token.startswith(prefix) for prefix in COLOUR_EXCLUDE_PREFIXES):
            continue
        for root, colour in COLOUR_ROOTS.items():
            if token.startswith(root):
                return colour
    return None


def _extract_numbers(raw_tokens: list[str], consumed_order_digits: str | None) -> tuple[int, ...]:
    numbers: list[int] = []
    for token in raw_tokens:
        if token.isdigit():
            if token == consumed_order_digits:
                continue
            numbers.append(int(token))
        elif token in CARDINALS:
            numbers.append(CARDINALS[token])
    return tuple(numbers)


def _word_tokens(raw_tokens: list[str]) -> tuple[str, ...]:
    words = []
    for token in raw_tokens:
        if token.isdigit() or "-" in token:
            continue
        words.append(_stem(token) if _is_cyrillic_word(token) else token)
    return tuple(words)


def normalize(question: str) -> NormalizedQuery:
    text = _clean_text(question)
    raw_tokens = _tokenize(text)
    order_number, consumed = _extract_order_number(text, raw_tokens)
    size = _extract_size(raw_tokens, consumed)
    colour = _extract_colour(raw_tokens)
    numbers = _extract_numbers(raw_tokens, consumed)
    tokens = _word_tokens(raw_tokens)

    return NormalizedQuery(
        text=text,
        tokens=tokens,
        numbers=numbers,
        order_number=order_number,
        size=size,
        colour=colour,
    )


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    print("-- stemming check --")
    for word in ["светр", "светрі", "светра"]:
        print(f"{word!r} -> {_stem(word)!r}")

    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/questions.jsonl")
    print(f"\n-- normalizing {path} --")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        nq = normalize(row["q"])
        print(f"{row['id']}: {row['q']!r}")
        print(f"  tokens={nq.tokens}")
        print(f"  numbers={nq.numbers} order={nq.order_number} "
              f"size={nq.size} colour={nq.colour}")
