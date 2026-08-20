# Project context

An agent that answers customer questions about the Verdal store using
`data/store.json`.

Input: `questions.jsonl` (`{"id","q"}`). Output: `results.jsonl` (`{"id","answer","ms"}`).

**Questions are in Ukrainian. Store data is in English. Answers must be Ukrainian.**

## Hard constraint

p95 response time <= 500 ms, measured from the input question string to the
complete answer. This is the defining constraint of the task, not a preference.
Any design that puts a network call on the hot path of every question is wrong
by definition.

## Architecture

Three tiers. Each question takes exactly one of them.

1. **Deterministic** (~1-3 ms) — recognised intent, structured lookup against
   in-memory indexes, templated answer. Zero network. Target: >=90% of questions.
2. **Computed** (~1-3 ms) — same, plus arithmetic derived from policies
   (line totals, free-shipping threshold, delivery cost).
3. **Tail** (<=350 ms) — questions the router could not classify confidently.
   One Groq call with the whole `store.json` in the prompt. Hard deadline.
   Timeout, 429 or missing key -> deterministic safe answer.

The LLM also runs **offline** (`make build`) to generate `data/aliases.json`.
That file is committed. `make run` never regenerates it.

## Stack

Python 3.12, plain CLI. Dependencies: rapidfuzz, httpx, python-dotenv, pyyaml.

## Layout

src/agent/store.py           - load store.json, build in-memory indexes
src/agent/normalize.py       - ukrainian text normalization
src/agent/resolve.py         - text -> product / variant / order
src/agent/router.py          - intent classification
src/agent/handlers/          - order, product, policy, compute
src/agent/fallback.py        - LLM tail with deadline
src/agent/timing.py          - measurement and percentiles
src/agent/build_aliases.py   - offline phase

## Conventions

- All store access goes through indexes built once at startup.
  No linear scans over lists on the hot path.
- Every handler returns `Answer(text: str, branch: str, confident: bool)`.
  `branch` is required for the per-path latency breakdown.
- Exceptions on the hot path are caught, logged and turned into a safe answer.
  Never swallowed silently.
- Timing uses `time.perf_counter()`, measured per question.
- Code, comments and docstrings in English. Answer templates in Ukrainian.

## Do NOT

- Do not hardcode answers, question ids or aliases for the 39 sample questions.
  The evaluator will supply a larger file with different phrasing.
- Do not add a vector store,