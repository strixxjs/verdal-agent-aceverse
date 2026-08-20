# Decisions

Format: question / chosen / alternatives / why rejected / when it breaks.

---

## 1. Overall architecture: RAG or deterministic routing

**Chosen:** deterministic routing plus structured lookup, with the LLM used
only on the tail of unclassified questions.

**Alternatives:** classic RAG — chunk `store.json`, index into a vector store,
embed the question, retrieve top-k, let the LLM generate the answer.

**Why rejected:**
- The budget does not add up. Embedding over an API costs 50-150 ms of
  network, generation 300-2000 ms. Two network round trips per question.
  p95 <= 500 ms is unreachable.
- This is not a retrieval problem. 18 products, 20 orders, 5 policies —
  a few kilobytes, the entire dataset fits in memory. A vector store solves
  a problem this data does not have.
- Semantics lose where variant-level precision is required. For "merino
  sweater, size M, navy" the correct answer is `M / navy`, stock 0 — not
  available. Meanwhile `M / beige` has 12 in stock. Cosine similarity between
  those two strings is nearly identical. Getting it wrong means telling a
  customer an item is in stock when it is not.
- Arithmetic is outside what RAG does: 49 + 12 = 61 (base layer plus standard
  shipping), 89 x 2 = 178 > 150, therefore shipping is free.

**When it breaks:** if the catalogue grows to tens of thousands of SKUs, or if
questions become open-ended advisory ones ("what should I wear hiking in
autumn"). A semantic tier is then justified — as an additional router branch,
not as a replacement for it.

---

## 2. Role of the LLM: responder or compiler

**Chosen:** the LLM runs offline, before the measured run. It generates
`data/aliases.json` — Ukrainian names and synonyms for every product.
The file is committed to the repository.

**Alternatives:** LLM on the hot path of every question; a hand-written
alias dictionary.

**Why rejected:**
- Hot path: see decision 1.
- A hand-written dictionary cannot be defended honestly. The evaluator
  supplies a different question file, so a dictionary written against the
  visible 39 questions is tuning to the test. Offline generation produces
  roughly 15 aliases per product, including phrasings I would not have
  anticipated.

**When it breaks:** if the evaluator names a product in a way the offline
generation did not cover. Such questions fall to the tail — which is exactly
why the tail must be functional rather than decorative.

---

## 3. Guaranteeing p95: measurement or construction

**Chosen:** a hard deadline on every path. The deterministic path performs no
network calls at all. The tail uses an `httpx` timeout of 350 ms, after which
a safe deterministic answer is returned.

**Alternatives:** rely on the provider's average latency fitting the budget.

**Why rejected:** p95 is not the mean. Network variance from Warsaw to Groq,
provider-side queueing and 429 responses under rate limits all hit the tail
of the distribution — which is precisely the number being promised. A budget
that rests on hope does not hold.

**When it breaks:** once the tail exceeds roughly 15% of questions, the 350 ms
path starts to define p95 instead of the 3 ms main path.

---

## 4. LLM provider

**Chosen:** Groq, `llama-3.3-70b-versatile`, free tier.

**Alternatives:** Google AI Studio (1500 requests/day vs 1000, 1M tokens/min
vs 12K), OpenAI or Anthropic (paid), a local model via Ollama.

**Why rejected:** Google is more generous on volume, but Groq's LPU hardware
is 3-10x faster — and the binding constraint here is latency, not volume.
A local model would tie the result to the evaluator's hardware.

**When it breaks:** the 30 requests/minute cap. On a large question file that
is one request every two seconds. This is why the tail must stay under ~10%
of questions and degrade on 429 rather than fail.

---

## 5. Running without an API key

**Chosen:** a missing or invalid `GROQ_API_KEY` does not break the run.
The tail returns a deterministic refusal and `make run` exits successfully.

**Alternatives:** require the key and fail fast on configuration error.

**Why rejected:** the evaluator runs one command in an environment I do not
control. Failing on configuration scores zero regardless of code quality.
Degradation must be predictable and visible — marked in the output through
the `fallback_degraded` branch.

---

## 6. Web framework

**Chosen:** plain CLI, no HTTP layer.

**Alternatives:** FastAPI with an endpoint.

**Why rejected:** the task asks for a single command mapping
`questions.jsonl` to `results.jsonl`. Nobody in that scenario needs HTTP;
it adds dependencies and startup time. This is exactly the kind of feature
that was not asked for.

---

## 7. Free-tier TPM ceiling as an architectural fact

**Observed during build:** Groq free tier caps at 8000 tokens per minute,
not just 30 requests per minute. The offline build hit 429 repeatedly and
needed retry-with-backoff to finish.

**Why it matters for the hot path:** the tail sends the whole store.json in
the prompt. store.json is ~4000 tokens; at 8000 TPM a handful of tail calls
per minute is the ceiling. On the evaluator's larger question file, if too
many questions reach the tail, they serialize behind the rate limit and blow
the latency budget.

**Consequence:** this is not a tuning detail, it is the reason the deterministic
path must catch the overwhelming majority of questions. The tail is a safety
net for genuinely unclassifiable input, not a second answering strategy.
Reinforces decision 3: the budget holds by construction, and the construction
depends on keeping the tail small.