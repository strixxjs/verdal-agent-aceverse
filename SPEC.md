# Specification

## Definition of done

1. `make run` on `data/questions.jsonl` writes `results.jsonl` as
   `{"id","answer","ms"}` — 39 lines, no exceptions raised.
2. `make run --in <any file with the same schema>` behaves identically.
3. p95 <= 500 ms on the warm pass (the first pass is discarded).
4. `make run` produces results when `GROQ_API_KEY` is absent or invalid.
5. `make eval` runs `eval/cases.yaml` and prints the pass count.

## Layer contracts

### store.py
Input: path to `store.json`.
Builds once at startup:
- `orders_by_number: dict[str, Order]` — key normalized, `#` stripped
- `products_by_id: dict[str, Product]`
- `variant_index: dict[product_id, list[ParsedVariant]]`
- `policies: dict[str, str]`

Verification: print index sizes, check against the data (18 products,
20 orders, 5 policies).

### normalize.py
Input: raw Ukrainian question string.
Output: `NormalizedQuery(text, tokens, numbers, order_number|None, size|None, colour|None)`.
Ukrainian inflection is handled by stemming plus fuzzy match, not by a form dictionary.

Verification: "светра", "светрі", "светр" collapse to the same stem.

### resolve.py
Input: `NormalizedQuery`.
Output: `Resolution(product|None, variant|None, order|None, score: float)`.
Order of attempts: exact match against `aliases.json` -> fuzzy match
(rapidfuzz) above threshold -> None.

Verification: no resolution below threshold ever returns a product.
Returning None is strictly better than returning the wrong product.

### router.py
Input: `NormalizedQuery` + `Resolution`.
Output: `Intent` + `confident: bool`.

Intents: ORDER_STATUS, ORDER_TRACKING, ORDER_ITEMS, ORDER_ADDRESS,
PRODUCT_PRICE, PRODUCT_STOCK, PRODUCT_COMPARE, COMPUTE_TOTAL,
POLICY_RETURNS, POLICY_EXCHANGE, POLICY_SHIPPING, POLICY_PAYMENT,
POLICY_DAMAGED, NO_DATA, REFUSE, UNKNOWN.

`UNKNOWN` or `confident=False` routes to the tail.

### handlers/*
Input: `NormalizedQuery` + `Resolution` + `Store`.
Output: `Answer(text, branch, confident)`.
No network access. No access to the LLM.

### fallback.py
Input: raw question + serialized `store.json`.
Deadline: 350 ms via `httpx.Timeout`. Any error, 429 or timeout returns
`Answer(text=<safe refusal>, branch="fallback_degraded", confident=False)`.

Verification: a run with a deliberately broken key completes and stays
within budget.

## Build order

Each layer is verified by an actual run before the next one starts.

1. store.py -> indexes
2. normalize.py + resolve.py -> resolution over the 39 questions,
   print everything that fails to resolve
3. router.py -> intent distribution over the 39 questions, print UNKNOWN
4. handlers -> answers
5. build_aliases.py -> offline generation, commit aliases.json
6. fallback.py -> tail
7. timing.py + cli.py -> results.jsonl, percentiles
8. eval/ -> scenarios

## Timing method

`make run` performs two passes. The first warms imports, indexes and caches.
The second is measured, and its `ms` values are what `results.jsonl` records.

This follows the task wording directly: the first pass does not count,
the second one is measured.

## Explicitly out of scope

- Multi-turn dialogue and cross-question memory. The task is one question,
  one answer.
- The voice layer. That is PART 2, a design document without code.
- Persistent storage. The data is static and read from a file.