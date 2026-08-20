# normalize.py implementation plan

## Context

`normalize.py` is currently an empty file (as are `resolve.py` and
`router.py` — this is the second layer in the build order after `store.py`).
Its job per SPEC.md is to turn a raw Ukrainian question string into a
`NormalizedQuery` that `resolve.py` and `router.py` can consume, without any
I/O, network, or store access. It must handle Ukrainian inflection through
generic truncation stemming (not a per-word-form dictionary), and pull out
order numbers, size tokens, colour tokens, and standalone numbers using
context, not hardcoded question strings — the evaluator runs a different,
larger question file with different phrasing.

Confirmed with the user: `numbers` also captures Ukrainian cardinal number
words (один..десять, oblique forms like "трьох"/"трьома" included) via a
small closed lookup table, in addition to digit runs — needed for q020's
free-shipping arithmetic. This lookup is additive only: it never removes or
alters the token used for product resolution (q033's "трьох" stays in
`tokens` untouched so "набір із трьох" still resolves to the socks product).

## Public contract

```python
@dataclass(frozen=True, slots=True)
class NormalizedQuery:
    text: str                    # cleaned (lowercased, whitespace-collapsed) question
    tokens: tuple[str, ...]      # word tokens, Ukrainian ones truncation-stemmed
    numbers: tuple[int, ...]     # every standalone integer: digit runs + cardinal words
    order_number: str | None     # bare digits, via store.normalize_order_number
    size: str | None             # letter / numeric / range / "one", via store.normalize_size
    colour: str | None           # canonical English colour matching store.json variants

def normalize(question: str) -> NormalizedQuery: ...
```

Reuse from `store.py` (import only, no edits to that file):
- `normalize_order_number` — required by SPEC.
- `normalize_size` — classifies a candidate token as letter/numeric/range/one/other
  and gives the numeric span. Reusing this avoids re-deriving the same
  regexes (`_RANGE_RE`, `_NUMERIC_RE`, `LETTER_SIZES`, `ONE_SIZE_TOKENS`) a
  second time and keeps the two layers' idea of "a size" identical.

## Tokenization

- Lowercase and collapse whitespace to build `text`.
- Split on whitespace and punctuation, EXCEPT:
  - a hyphen between two digit groups stays joined (`"39-42"`, `"43-46"` —
    needed for range sizes).
  - a hyphen between two word characters splits (`"софтшел-куртка"` →
    `["софтшел", "куртка"]`, `"трек-номер"` → `["трек", "номер"]`) so the
    stemmer and later fuzzy match see the individual roots.
  - an apostrophe inside a word stays joined (Ukrainian apostrophe words).
- `#` is stripped as a token boundary but the digits that follow it are what
  order-number detection looks for first.
- Tokens that are pure digit runs feed `numbers`/`order_number` detection but
  are excluded from the `tokens` word list (they're not words to stem or
  fuzzy-match).

## Stemming (truncation, not a dictionary)

- Applies only to alphabetic tokens using the Cyrillic range; Latin tokens
  (English product-name words customers type verbatim, e.g. "Alpine",
  "Trail") pass through lowercased and unstemmed — truncating English words
  would break exact alias matches for no benefit.
- Rule: if a Cyrillic token's length > 5, keep only its first 5 characters;
  otherwise keep it as-is.
  - Verifies the SPEC example directly: "светр" (5) stays "светр"; "светрі"
    and "светра" (6 each) truncate to "светр".
- This is deliberately generic (no per-word list) — residual mismatches are
  expected and are exactly what `resolve.py`'s fuzzy match (rapidfuzz) is
  there to absorb, per SPEC's "stemming plus fuzzy match" framing.

## Telling order number / size / quantity / colour apart

All four pull from the same token stream but use different, non-overlapping
signals so they don't collide on ambiguous bare numbers:

**order_number** (highest precedence, checked first):
1. `#\s*(\d+)` anywhere in the raw text → matched digits, run through
   `store.normalize_order_number`. Unambiguous, no context needed.
2. Else, a standalone digit run of **length >= 4** that appears near a token
   stemming to "замовл" (замовлення/замовленням/...) or "номер" → treated as
   an order id. The >=4 digit floor is what keeps this rule from misfiring
   on q037 ("Замовляю шапку за 25 євро..." — "замовл" stem present, but "25"
   is only 2 digits, so it's correctly left as a price, not an order id).
   Every order in store.json is a 4-digit number, so this generalizes rather
   than overfitting to sample data.
3. Else `None`.

Whichever digit run is consumed as `order_number` is excluded from `numbers`.

**size**: two-stage, most-specific-first, using `store.normalize_size`:
1. Homoglyph-normalize short tokens (len 1-3) that are visually-confusable
   Cyrillic letters for a Latin size code (e.g. Cyrillic "М" → Latin "M"),
   scoped narrowly to tokens that would then exactly match `LETTER_SIZES` —
   this only fires on real size codes, not on ordinary short Cyrillic words,
   because the check requires exact membership in `{XS,S,M,L,XL,XXL}` after
   substitution.
2. Run every token through `store.normalize_size`. If any token classifies
   as `"letter"`, `"range"`, or `"one"`, accept it immediately — these
   shapes are unambiguous (nothing else in the domain looks like "39-42" or
   a bare "L").
3. Otherwise, collect tokens that classify as `"numeric"` (2-3 digit, per
   `store.py`'s own regex) and are not already consumed as `order_number`.
   For each candidate:
   - if a token stems to "розм" (розмір/розміру) exists in the question,
     pick the numeric candidate nearest to it;
   - else, if exactly one numeric candidate exists and it is not adjacent to
     a disqualifying unit word (currency: "євро"/"грн"; percent: "%"/"відсот";
     capacity: "літр") accept it as a weak default;
   - else leave `size = None` — resolve.py has the product's real
     `variant_index` and can confirm/deny a numeric guess far better than
     this context-free layer can.
   - Validated by hand against all 39 sample questions: q013/q035 pick up
     via the "розм" keyword; q016/q032/q009 via unambiguous single
     candidates or direct letter matches; q011 ("35 літрів") and q037
     ("25 євро") and q027 ("20 відсотків") are correctly rejected by the
     unit-word disqualifiers.

**colour**: closed vocabulary lookup, same spirit as `store.py`'s
`LETTER_SIZES`/`ONE_SIZE_TOKENS` — not the "form dictionary" SPEC prohibits
(that prohibition targets open-ended general inflection handling; a fixed
8-colour palette translation is exactly the kind of small enumerable table
store.py itself already uses). Match by `token.startswith(root)` against
short invariant roots, independent of the generic 5-char stemmer so case
forms like "синьому" still match root "син":

```
син→navy, беж→beige, чорн→black, оливков→olive,
сір→grey, червон→red, іржав→rust, сталев→steel
```
Plus the bare English words (navy/beige/black/olive/grey/red/rust/steel) in
case a customer code-switches. First match wins; `None` if nothing hits.

**numbers**: every digit run not consumed by `order_number`, plus every
token matching the closed cardinal-word table (один/одна/одне→1,
два/дві→2, три/трьох/трьома→3, чотири/чотирьох→4, п'ять/п'яти→5, ... up to
десять→10, oblique forms included). This table is additive only — it never
removes or alters the matching token in `tokens`, so product-resolution
matching against phrases like "набір із трьох" (q033) is unaffected.

## File structure

Single file, pure functions, no I/O:

```
normalize.py
  NormalizedQuery (frozen dataclass)
  normalize(question: str) -> NormalizedQuery      # public entry point

  _tokenize(text: str) -> list[str]
  _stem(token: str) -> str
  _extract_order_number(text: str, tokens: list[str]) -> tuple[str | None, str | None]
      # returns (order_number, consumed_digit_run) so numbers extraction can exclude it
  _extract_size(tokens: list[str], stems: list[str]) -> str | None
  _extract_colour(tokens: list[str]) -> str | None
  _extract_numbers(tokens: list[str], consumed: str | None) -> tuple[int, ...]

  CARDINALS: dict[str, int]        # closed 1..10 map, oblique forms included
  COLOUR_ROOTS: dict[str, str]
  CYRILLIC_LATIN_CONFUSABLES: dict[str, str]
  STEM_PREFIX_LEN = 5
```

A `if __name__ == "__main__":` block (matching `store.py`'s pattern) runs
`normalize()` over `data/questions.jsonl` and prints each
`NormalizedQuery`, plus the explicit stemming check ("светра"/"светрі"/
"светр" → same stem), for manual verification per the SPEC build order
("normalize.py + resolve.py -> resolution over the 39 questions").

## Verification

1. Run `python -m src.agent.normalize` (or the `__main__` block) over
   `data/questions.jsonl` and manually inspect that all 39 questions get
   sane `order_number`/`size`/`colour`/`numbers`, spot-checking the cases
   above (q002, q007, q011, q013, q015, q020, q027, q029, q033, q035, q037).
2. Confirm "светр"/"светрі"/"светра" stem identically (SPEC's explicit
   verification requirement).
3. No file other than `src/agent/normalize.py` is modified.
