"""Resolve a NormalizedQuery to a concrete product, variant and/or order.

Alias-driven and fuzzy, never hardcoded to specific questions. Returning
None is strictly better than guessing: a wrong product match downstream is
worse than falling through to the LLM tail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from .normalize import COLOUR_ROOTS, NormalizedQuery, normalize
from .store import Order, Product, SizeKind, Store, Variant, normalize_size

# rapidfuzz token_set_ratio is 0-100. Below this, treat the match as not
# confident: product stays None and router.py routes to the LLM tail
# instead of answering about a possibly-wrong item.
#
# Data-driven, measured against the __main__ output over the 39 sample
# questions: the highest best_fuzzy among non-product questions (policy,
# order, refusal) was 57.1; the lowest best_fuzzy among genuine product
# questions was 62.5. 60.0 sits in that gap. Re-measure and re-tune this
# constant if the evaluator's larger question set narrows or removes the
# gap.
CONFIDENCE_THRESHOLD: float = 60.0

# A single-word alias shorter than this carries too little context to trust
# as an exact substring match on its own (e.g. a 2-3 letter word could sit
# inside an unrelated longer word). Multi-word phrases are exempt: their
# extra words already provide the disambiguating context.
EXACT_MIN_SINGLE_WORD_LEN = 4

_SIZE_LIKE_KINDS: frozenset[SizeKind] = frozenset({"letter", "numeric", "range", "one"})


@dataclass(frozen=True, slots=True)
class Resolution:
    product: Product | None
    variant: Variant | None
    order: Order | None
    score: float


@dataclass(frozen=True, slots=True)
class AliasIndex:
    exact: dict[str, str]                        # lowered phrase -> product_id
    fuzzy_corpus: tuple[tuple[str, str], ...]     # (product_id, cleaned token string)
    exact_pattern: re.Pattern                     # one alternation over all phrases
    fuzzy_candidates: tuple[str, ...]             # corpus strings, prebuilt once

    @classmethod
    def load(cls, store: Store, facts_path: str | Path = "data/facts.json") -> "AliasIndex":
        payload = json.loads(Path(facts_path).read_text(encoding="utf-8"))
        aliases: dict[str, list[str]] = payload.get("aliases", {})

        exact: dict[str, str] = {}
        fuzzy_corpus: list[tuple[str, str]] = []

        for product_id, product in store.products_by_id.items():
            phrases = list(aliases.get(product_id, [])) + [product.title]
            for phrase in phrases:
                lowered = phrase.lower()
                is_short_single_word = " " not in lowered and len(lowered) < EXACT_MIN_SINGLE_WORD_LEN
                if not is_short_single_word:
                    exact[lowered] = product_id

                # A single-token alias can never win the fuzzy path: with only
                # one token to contribute, token_set_ratio's intersection can
                # fully absorb it (diff2 empty), forcing a degenerate ratio of
                # 100 regardless of whether the match is real (e.g. "рукавиці"
                # stems to the same "рукав" prefix as an unrelated "рукаві").
                # Long single-token aliases are already covered by the exact
                # step above; short ones are rejected there too. Requiring
                # >=2 tokens means the alias always keeps a token outside the
                # intersection, so its score reflects genuine overlap.
                cleaned = _clean_tokens(normalize(phrase).tokens)
                if len(cleaned) >= 2:
                    fuzzy_corpus.append((product_id, " ".join(cleaned)))

        # Precompile the exact-match scan into one alternation, built once at
        # startup: the hot path must not loop over ~300 phrases per question
        # (see the "no linear scans on the hot path" convention). Longest
        # phrases first so at equal start positions the longest wins.
        if exact:
            alternation = "|".join(
                re.escape(p) for p in sorted(exact, key=len, reverse=True)
            )
            exact_pattern = re.compile(rf"\b(?:{alternation})\b")
        else:
            exact_pattern = re.compile(r"(?!x)x")  # matches nothing

        return cls(
            exact=exact,
            fuzzy_corpus=tuple(fuzzy_corpus),
            exact_pattern=exact_pattern,
            fuzzy_candidates=tuple(c for _, c in fuzzy_corpus),
        )


def _is_noise_token(token: str) -> bool:
    """A token that carries no product-name signal.

    Size codes and colour words are already captured in their own
    NormalizedQuery fields and used for variant resolution; leaving them in
    the product-name fuzzy string is noise, and short tokens can spuriously
    inflate token_set_ratio.
    """
    if len(token) <= 1:
        return True
    _, size_kind, _ = normalize_size(token)
    if size_kind in _SIZE_LIKE_KINDS:
        return True
    if any(token.startswith(root) for root in COLOUR_ROOTS):
        return True
    return False


def _clean_tokens(tokens: tuple[str, ...]) -> list[str]:
    return [t for t in tokens if not _is_noise_token(t)]


def _exact_match(query: NormalizedQuery, index: AliasIndex) -> str | None:
    """Longest alias phrase that appears as a whole word/phrase in query.text.

    One pass with the precompiled alternation built at startup; picks the
    longest match across all positions to keep the old semantics.
    """
    best_phrase: str | None = None
    for match in index.exact_pattern.finditer(query.text):
        phrase = match.group(0)
        if best_phrase is None or len(phrase) > len(best_phrase):
            best_phrase = phrase
    return index.exact[best_phrase] if best_phrase is not None else None


def _fuzzy_match(query: NormalizedQuery, index: AliasIndex) -> tuple[str | None, float]:
    """Best fuzzy candidate product_id and its raw rapidfuzz ratio (0-100).

    Pure diagnostic lookup: CONFIDENCE_THRESHOLD is not applied here, so
    callers that need an accept/reject decision must check the ratio
    themselves. Kept separate from _resolve_product so __main__ can report
    the real fuzzy score for every question, including rejected ones.
    """
    query_tokens = _clean_tokens(query.tokens)
    if not query_tokens or not index.fuzzy_corpus:
        return None, 0.0

    query_str = " ".join(query_tokens)
    match = process.extractOne(
        query_str, index.fuzzy_candidates, scorer=fuzz.token_set_ratio
    )
    if match is None:
        return None, 0.0

    _, ratio, idx = match
    product_id, _ = index.fuzzy_corpus[idx]
    return product_id, ratio


def _resolve_product(query: NormalizedQuery, store: Store, index: AliasIndex) -> tuple[Product | None, float]:
    exact_product_id = _exact_match(query, index)
    if exact_product_id is not None:
        return store.products_by_id.get(exact_product_id), 1.0

    product_id, ratio = _fuzzy_match(query, index)
    if product_id is None or ratio < CONFIDENCE_THRESHOLD:
        return None, 0.0

    return store.products_by_id.get(product_id), ratio / 100.0


def resolve(query: NormalizedQuery, store: Store, index: AliasIndex) -> Resolution:
    if query.order_number is not None:
        order = store.order(query.order_number)
        return Resolution(
            product=None,
            variant=None,
            order=order,
            score=1.0 if order is not None else 0.0,
        )

    product, score = _resolve_product(query, store, index)
    if product is None:
        return Resolution(product=None, variant=None, order=None, score=score)

    variant: Variant | None = None
    size = query.size
    if size is not None and not product.size_axis_matches(size):
        # "35" extracted from "Hiking Backpack 35L" is part of the name,
        # not a size this product has — drop it rather than mismatch.
        size = None
    if size is not None or query.colour is not None:
        matches = product.find(size, query.colour)
        if len(matches) == 1:
            variant = matches[0]

    return Resolution(product=product, variant=variant, order=None, score=score)


if __name__ == "__main__":
    import sys

    store = Store.load("data/store.json")
    index = AliasIndex.load(store, "data/facts.json")

    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/questions.jsonl")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        query = normalize(row["q"])
        resolution = resolve(query, store, index)

        product_title = resolution.product.title if resolution.product else None
        variant_raw = resolution.variant.raw if resolution.variant else None
        order_display = resolution.order.display if resolution.order else None

        fuzzy_product_id, fuzzy_ratio = _fuzzy_match(query, index)
        fuzzy_title = store.products_by_id[fuzzy_product_id].title if fuzzy_product_id else None

        print(
            f"{row['id']}: {row['q']!r}\n"
            f"  product={product_title!r} variant={variant_raw!r} order={order_display!r} "
            f"accepted_score={resolution.score:.2f} "
            f"best_fuzzy={fuzzy_ratio:.1f} best_fuzzy_product={fuzzy_title!r}"
        )