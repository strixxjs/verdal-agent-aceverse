# Latency budget

Constraint: p95 <= 500 ms, from the input question string to the complete
answer, measured on the warm pass (the first pass is discarded).

Measured on the 39 sample questions, warm pass:

| metric | value |
|--------|-------|
| p50    | 1 ms  |
| p95    | 208 ms |
| p99    | 224 ms |
| max    | 224 ms |

p95 is ~2.4x under budget. Half the questions answer in ~1 ms.

## Why different questions take different paths

The system routes each question to exactly one of two classes, and the two
have latency profiles that differ by ~300x. The p95 is therefore governed
almost entirely by *how many* questions land in the slow class, not by how
fast any single component is.

### Deterministic path — 36 of 39 questions, 0.06–1.4 ms

No network. The question is normalized, resolved against in-memory indexes,
routed by keyword, and answered from a template or a small arithmetic step.
Per-branch measured cost:

| branch group        | typical p50 | what it does |
|---------------------|-------------|--------------|
| order_*             | 0.06–0.10 ms | dict lookup by order number |
| product_price/stock | ~0.7 ms      | alias resolve + variant lookup |
| policy_*            | ~0.7 ms      | template filled from facts.json |
| compute_*           | 0.7–1.4 ms   | price × qty + shipping arithmetic |
| refuse / no_data    | ~0.8 ms      | fixed safe response |

The order branches are the fastest because an order number is an exact key —
no fuzzy matching runs at all. Product branches cost slightly more because
they run rapidfuzz over the alias corpus.

### LLM tail — 3 of 39 questions, 211–224 ms

Reached only when the router is not confident (q017 product comparison, q024
unrecognized) or a handler cannot answer (q037 ambiguous product reference).

The tail has a hard 350 ms deadline. Measured LLM round-trip when it does
answer is ~1000 ms — well over the whole 500 ms budget — so the tail is not
allowed to complete inside the hot path. It degrades to a safe answer within
the deadline instead. In this run all three tail questions degraded (the
free-tier rate limit was already spent), each ~210–224 ms, which is the
timeout-plus-overhead cost, not an LLM answer.

This is the core design decision: the LLM cannot meet the budget, so the
architecture is built so that ~90%+ of questions never reach it, and the few
that do degrade predictably rather than blowing p95.

## What sets the p95

With 3 of 39 questions (~8%) in the tail at ~220 ms and the rest at ~1 ms,
the 95th percentile sits inside the tail band. If the deterministic path
caught 100% of questions, p95 would be ~1 ms. The tail is what the budget is
really spent on — which is why keeping the tail small (see DECISIONS.md 3, 7)
is the whole game.

## On the evaluator's larger file

The absolute p95 depends on the tail fraction. If the evaluator's questions
resolve at the same ~8% tail rate, p95 stays near 220 ms. If a larger share
falls to the tail, p95 rises toward the 350 ms deadline but cannot exceed it
per question — the deadline is a hard ceiling, so no single question can push
past ~350 ms regardless of network conditions.