# Latency budget

Constraint: p95 <= 500 ms, from the input question string to the complete
answer, measured on the warm pass (the first pass is discarded). Percentiles
use the standard nearest-rank method (value at rank `ceil(q/100 * n)`).

Measured on the 39 sample questions, warm pass, no API key (the tail
degrades deterministically):

| metric | value   |
|--------|---------|
| p50    | <0.1 ms |
| p95    | <0.2 ms |
| p99    | <0.2 ms |
| max    | 0.2 ms  |

With a live key the two tail questions are bounded by the 350 ms
wall-clock deadline instead, which puts the worst-case p95 on this file at
~350 ms — still under budget. No single question can exceed the deadline.

## Why different questions take different paths

The system routes each question to exactly one of two classes with latency
profiles that differ by orders of magnitude. The p95 is therefore governed
almost entirely by *how many* questions land in the slow class, not by how
fast any single component is.

### Deterministic path — 37 of 39 questions, 0.02–0.2 ms

No network. The question is normalized, resolved against in-memory indexes,
routed by keyword, and answered from a template or a small arithmetic step.
Per-branch measured cost:

| branch group        | typical p50  | what it does |
|---------------------|--------------|--------------|
| order_*             | ~0.02 ms     | dict lookup by order number |
| product_price/stock | 0.04–0.15 ms | alias resolve + variant lookup |
| policy_*            | ~0.13 ms     | template filled from facts.json |
| compute_*           | 0.04–0.17 ms | price × qty + shipping arithmetic |
| refuse / no_data    | ~0.15 ms     | fixed safe response |

The order branches are the fastest because an order number is an exact key —
no fuzzy matching runs at all. Product/policy branches run the precompiled
alias scan and rapidfuzz over the alias corpus, both built once at startup.

### LLM tail — 2 of 39 questions

Reached only when the router is not confident (q017 product comparison) or a
handler cannot answer (q037 ambiguous product reference).

The tail has a hard 350 ms **wall-clock** deadline, enforced by a worker
thread + future timeout — an httpx timeout alone caps each connection phase
separately and would let a slow-but-alive call run to ~1 s total. Measured
LLM round-trip when it does answer is ~1000 ms — well over the whole 500 ms
budget — so the tail is not allowed to complete inside the hot path unless it
is genuinely fast; otherwise it degrades to a safe answer within the
deadline. Without a key the degradation is immediate (~0.2 ms) and the reason
is logged.

The warm pass never calls the LLM at all (`fallback_skipped`), so it cannot
spend the free-tier quota (30 RPM / 8000 TPM) that the measured pass needs.

This is the core design decision: the LLM cannot meet the budget, so the
architecture is built so that ~90%+ of questions never reach it, and the few
that do degrade predictably rather than blowing p95.

## What sets the p95

With 2 of 39 questions (~5%) in the tail and the rest at ~0.1 ms, the 95th
percentile sits inside the tail band whenever the tail is slower than the
deterministic path. If the deterministic path caught 100% of questions, p95
would be ~0.1 ms. The tail is what the budget is really spent on — which is
why keeping the tail small (see DECISIONS.md 3, 7) is the whole game.

## On the evaluator's larger file

The absolute p95 depends on the tail fraction. If the evaluator's questions
resolve at the same ~5% tail rate, p95 stays within the tail band (<=350 ms
with a key, <1 ms without). If a larger share falls to the tail, p95 rises
toward the 350 ms deadline but cannot exceed it per question — the deadline
is a wall-clock ceiling, so no single question can push past ~350 ms
regardless of network conditions.
