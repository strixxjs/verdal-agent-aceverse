"""LLM tail: the last resort for questions the router could not classify.

Hard wall-clock deadline. If the model does not answer within the deadline,
or the key is missing, or Groq rate-limits us, we return a safe deterministic
refusal marked with branch 'fallback_degraded'. The run never fails because
of the tail.

The deadline is enforced with a worker thread + future timeout, NOT with the
httpx timeout alone: httpx.Timeout caps each phase (connect/write/read)
separately, so a call could legally take ~1 s total while every phase stays
under 350 ms — that would blow the p95 budget. The per-phase httpx timeout is
kept only so the worker thread cannot hang on a dead connection.

Reached by <=10% of questions by design (see DECISIONS.md 3 and 7). The whole
store.json goes into the prompt because it is small — one network round trip,
not two.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import httpx
from dotenv import load_dotenv

from agent.handlers import Answer

load_dotenv()

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
DEADLINE_S = 0.35  # hard wall-clock cap. Measured LLM latency ~1000ms >>
                   # 500ms p95 budget, so the tail degrades within budget
                   # rather than blowing p95. See DECISIONS.md 3 and 7.

# Tail calls run here so future.result(timeout=...) can enforce the
# wall-clock deadline. A timed-out request keeps running in its worker for
# up to the per-phase httpx timeouts and is then dropped.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-tail")

SAFE_TEXT = (
    "Вибачте, не можу впевнено відповісти на це питання. "
    "Уточніть, будь ласка, або зверніться до підтримки магазину."
)

_SYSTEM = (
    "You are a customer-support agent for the Verdal online store. "
    "Answer in Ukrainian, briefly and factually, using ONLY the store data "
    "provided. If the data does not contain the answer, say you don't have "
    "that information — never invent prices, stock, orders or policies."
)


def _degraded(reason: str) -> Answer:
    """Safe answer when the tail cannot deliver. The reason is logged so
    degradations are visible (timeout vs 429 vs bad key), never silent."""
    logger.warning("llm tail degraded: %s", reason)
    return Answer(text=SAFE_TEXT, branch="fallback_degraded", confident=False)


def _store_context(store_path: str | Path) -> str:
    # store.json is a few KB; sending it whole is one round trip.
    return Path(store_path).read_text(encoding="utf-8")


def _post_groq(key: str, context: str, question: str) -> httpx.Response:
    """The blocking network call, run inside the worker pool. The httpx
    per-phase timeout only bounds the worker's lifetime; the real deadline
    is enforced by future.result() in handle()."""
    return httpx.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 800,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Store data (JSON):\n{context}\n\n"
                        f"Customer question (Ukrainian): {question}"
                    ),
                },
            ],
        },
        timeout=httpx.Timeout(DEADLINE_S),
    )


def handle(question: str, store_path: str | Path,
           api_key: str | None = None) -> Answer:
    key = api_key if api_key is not None else os.getenv("GROQ_API_KEY")
    if not key:
        return _degraded("no api key")

    try:
        context = _store_context(store_path)
    except OSError:
        return _degraded("store unreadable")

    future = _POOL.submit(_post_groq, key, context, question)
    try:
        response = future.result(timeout=DEADLINE_S)
    except FutureTimeoutError:
        future.cancel()
        return _degraded("deadline exceeded")
    except httpx.TimeoutException:
        return _degraded("transport timeout")
    except httpx.HTTPError:
        return _degraded("transport error")

    if response.status_code == 429:
        return _degraded("rate limited")
    if response.status_code != 200:
        return _degraded(f"http {response.status_code}")

    try:
        content = response.json()["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, ValueError):
        return _degraded("bad response shape")

    # content may be null (filtered / non-standard finish reasons) — that is
    # data, not an exception; it must not crash the hot path.
    text = content.strip() if isinstance(content, str) else ""
    if not text:
        return _degraded("empty answer")

    return Answer(text=text, branch="fallback_llm", confident=True)


if __name__ == "__main__":
    # Manual check: works, and degrades safely with a broken key.
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "Що дешевше: рюкзак чи куртка?"

    print("-- with real key --")
    print(handle(q, "data/store.json"))

    print("\n-- with broken key (must degrade, not crash) --")
    print(handle(q, "data/store.json", api_key="broken"))