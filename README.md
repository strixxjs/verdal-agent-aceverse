# Verdal store agent

Answers customer questions about the Verdal store from `data/store.json`.
Questions come in Ukrainian; store data is in English; answers are Ukrainian.

Built for the Aceverse take-home. Part 1 is this running system; Part 2 is a
design document (`PART2.md`).

## Run

```bash
pip install -e .
make run
```

`make run` reads `data/questions.jsonl` and writes `results.jsonl`:

```json
{"id": "q001", "answer": "...", "ms": 0}
```

To run against a different question file of the same schema:

```bash
python -m agent.cli --in path/to/questions.jsonl --out results.jsonl
```

Set `PYTHONPATH=src` if you skip `pip install -e .`:

```bash
PYTHONPATH=src python -m agent.cli --in <file> --out results.jsonl
```

No API key is required to run. See "The LLM tail" below.

## The constraint

p95 <= 500 ms, from the input string to the complete answer. Measured p95 is
**208 ms** on the 39 sample questions — see `LATENCY.md` for the per-branch
numbers and why different questions take different paths.

## How it works

A question flows through one path, chosen by confidence:

1. **normalize** — Ukrainian text to tokens, with truncation stemming, plus
   extraction of order number, size, colour and quantities (digits and
   cardinal words like "два").
2. **resolve** — tokens to a concrete product / variant / order, using
   alias data and fuzzy matching. Returns nothing rather than guessing when
   confidence is low.
3. **route** — intent classification (order status, price, stock, a policy,
   an arithmetic total, a refusal, "no data", or unknown).
4. **handle** — a deterministic handler produces the answer from the store
   and policy facts. No network.
5. **tail** — only if the router is not confident: one LLM call with a hard
   deadline, degrading to a safe answer if it cannot deliver in time.

36 of 39 questions never leave the deterministic path and answer in ~1 ms.

## Why not RAG

The obvious first design — chunk the store, embed, retrieve, let an LLM
answer — fails the constraint and the task. The data is a few kilobytes, so
there is nothing to retrieve; two network round trips per question cannot fit
500 ms; and semantic similarity is exactly wrong for variant-level precision
(size M navy is out of stock while M beige is not — a retriever blurs that
distinction). Full reasoning in `DECISIONS.md`.

## The LLM tail

The tail uses Groq (free tier, `openai/gpt-oss-120b`). It runs **offline** to
generate `data/facts.json` (Ukrainian product aliases + policy numbers), which
is committed. The hot path reads that file and needs no key.

At runtime the tail is a fallback for unclassified questions only. Measured
LLM latency is ~1000 ms — over the whole budget — so the tail is deadline-
capped at 350 ms and degrades to a safe answer rather than completing inside
the hot path. A missing or rate-limited key never breaks a run; those
questions return a safe response marked `fallback_degraded`.

To enable the tail, copy `.env.example` to `.env` and set `GROQ_API_KEY`.
To regenerate aliases after changing `store.json`: `make build`.

## What is deliberately not done

- **Ambiguous product references are not guessed.** q037 ("шапку за 25 євро")
  degrades instead of answering: "шапка" matches both Merino Beanie and Sun
  Hat, so the honest response is to ask, not to pick one and compute a price
  for it. This is intended behaviour, not a gap.
- **Two-product comparison** (q017) goes to the tail. The resolver returns one
  product; a comparison needs two. Rather than answer about one and look
  confident, the router marks it not-confident. Handling two products in one
  resolution is the clear next step.
- **Prices come from the store, never from the question.** If a customer names
  a wrong price, the agent computes from `store.json`, not from what was said.
- **No multi-turn memory, no voice layer** — out of scope for Part 1.

## Layout