"""Entry point: questions.jsonl -> results.jsonl.

One command, per the task. Two passes: the first warms imports/indexes/caches,
the second is measured. results.jsonl records the second pass, per the task's
"first pass does not count, measure the second" wording.

Routing recap (see DECISIONS.md):
- deterministic handlers answer the vast majority in ~1-3 ms
- the LLM tail is reached only for router-unconfident questions and degrades
  within a hard deadline, so it cannot blow the p95 budget
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from agent.handlers import Answer
from agent.handlers import compute as h_compute
from agent.handlers import order as h_order
from agent.handlers import policy as h_policy
from agent.handlers import product as h_product
from agent import fallback as h_fallback
from agent.normalize import normalize
from agent.resolve import AliasIndex, resolve
from agent.router import route
from agent.store import Store

logger = logging.getLogger(__name__)

_ORDER_INTENTS = {"ORDER_STATUS", "ORDER_TRACKING", "ORDER_ITEMS", "ORDER_ADDRESS"}
_PRODUCT_INTENTS = {"PRODUCT_PRICE", "PRODUCT_STOCK"}
_POLICY_INTENTS = {
    "POLICY_RETURNS", "POLICY_EXCHANGE", "POLICY_SHIPPING",
    "POLICY_PAYMENT", "POLICY_DAMAGED",
}

# Every policy/compute number the handlers rely on. Validated at startup so
# a broken facts.json regeneration is visible immediately, not as silently
# degraded answers mid-run.
_REQUIRED_POLICY_FACTS = (
    "standard_shipping_cost", "express_shipping_cost",
    "free_shipping_threshold", "standard_delivery_days",
    "express_delivery_days", "return_window_days", "refund_business_days",
    "damage_report_hours", "ships_outside_eu", "cash_on_delivery",
)

_REFUSE_TEXT = (
    "На жаль, я не можу надати знижку чи змінити ціну — "
    "це рішення магазину. Можу допомогти з товарами, замовленнями чи умовами."
)

_NO_DATA_TEXT = (
    "На жаль, таких даних у мене немає. "
    "Уточніть, будь ласка, у підтримці магазину."
)

_ERROR_TEXT = (
    "Вибачте, сталася технічна помилка під час обробки питання. "
    "Спробуйте, будь ласка, переформулювати."
)


class Agent:
    """Holds the loaded store and indexes; answers one question at a time."""

    def __init__(self, store_path: str | Path, facts_path: str | Path):
        self.store = Store.load(store_path)
        self.index = AliasIndex.load(self.store, facts_path)
        self.facts: dict[str, Any] = json.loads(
            Path(facts_path).read_text(encoding="utf-8")
        )
        self.store_path = str(store_path)

        policy_facts = self.facts.get("policy_facts", {})
        missing = [k for k in _REQUIRED_POLICY_FACTS
                   if policy_facts.get(k) is None]
        if missing:
            logger.warning(
                "facts.json is missing policy facts %s — affected policy/"
                "compute answers will degrade to the tail", missing,
            )

    def _tail(self, question: str, allow_tail: bool) -> Answer:
        """The LLM tail, or its deterministic stand-in on the warm pass.

        The warm pass must not spend Groq quota (30 RPM / 8000 TPM) that the
        measured pass needs — it warms imports/indexes/caches only."""
        if not allow_tail:
            return Answer(text=h_fallback.SAFE_TEXT,
                          branch="fallback_skipped", confident=False)
        return h_fallback.handle(question, self.store_path)

    def answer(self, question: str, allow_tail: bool = True) -> Answer:
        query = normalize(question)
        resolution = resolve(query, self.store, self.index)
        r = route(query, resolution)
        intent = r.intent.value if hasattr(r.intent, "value") else str(r.intent)

        # Router not confident -> tail. (PRODUCT_COMPARE, UNKNOWN.)
        if not r.confident:
            return self._tail(question, allow_tail)

        if intent in _ORDER_INTENTS:
            return h_order.handle(intent, query, resolution, self.store)

        if intent in _PRODUCT_INTENTS:
            ans = h_product.handle(intent, query, resolution, self.store)
            # Product intent but nothing resolved -> tail rather than a shrug.
            if not ans.confident:
                return self._tail(question, allow_tail)
            return ans

        if intent in _POLICY_INTENTS:
            ans = h_policy.handle(intent, query, resolution, self.store,
                                  self.facts)
            # A missing policy fact degrades to the tail, which has the raw
            # policy text in its prompt.
            if not ans.confident:
                return self._tail(question, allow_tail)
            return ans

        if intent == "COMPUTE_TOTAL":
            ans = h_compute.handle(query, resolution, self.store, self.facts)
            if not ans.confident:
                return self._tail(question, allow_tail)
            return ans

        if intent == "REFUSE":
            return Answer(text=_REFUSE_TEXT, branch="refuse", confident=True)

        if intent == "NO_DATA":
            return Answer(text=_NO_DATA_TEXT, branch="no_data", confident=True)

        # Fell through everything -> tail.
        return self._tail(question, allow_tail)


def _read_questions(path: str | Path) -> list[dict[str, Any]]:
    """Read the JSONL input. A malformed line is skipped with a warning —
    one broken line must not kill the whole run before any answers."""
    items = []
    for lineno, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            logger.warning("skipping malformed JSON on line %d of %s",
                           lineno, path)
            continue
        if not isinstance(row, dict):
            logger.warning("skipping non-object JSON on line %d of %s",
                           lineno, path)
            continue
        items.append(row)
    return items


def _answer_safely(agent: Agent, item: dict[str, Any], allow_tail: bool) -> Answer:
    """Per-question isolation: one bad question (or a bug it triggers) must
    cost one safe answer, never the whole run. Caught, logged, degraded —
    per the hot-path convention."""
    q = item.get("q")
    if not isinstance(q, str) or not q.strip():
        logger.warning("malformed question line (id=%r): no usable 'q'",
                       item.get("id"))
        return Answer(text=_ERROR_TEXT, branch="input_error", confident=False)
    try:
        return agent.answer(q, allow_tail=allow_tail)
    except Exception:
        logger.exception("unhandled error answering id=%r", item.get("id"))
        return Answer(text=_ERROR_TEXT, branch="error", confident=False)


def _run_pass(agent: Agent, questions: list[dict[str, Any]],
              measure: bool) -> tuple[list[dict], dict[str, list[float]]]:
    results = []
    branch_times: dict[str, list[float]] = {}
    for item in questions:
        start = time.perf_counter()
        # The warm pass must not spend LLM quota; only the measured pass may.
        ans = _answer_safely(agent, item, allow_tail=measure)
        ms = (time.perf_counter() - start) * 1000
        if measure:
            branch_times.setdefault(ans.branch, []).append(ms)
        results.append({"id": item.get("id"), "answer": ans.text, "ms": round(ms)})
    return results, branch_times


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Verdal store agent")
    parser.add_argument("--in", dest="in_path", default="data/questions.jsonl")
    parser.add_argument("--out", dest="out_path", default="results.jsonl")
    parser.add_argument("--store", default="data/store.json")
    parser.add_argument("--facts", default="data/facts.json")
    parser.add_argument("--bench", action="store_true",
                        help="print per-branch latency breakdown")
    args = parser.parse_args()

    agent = Agent(args.store, args.facts)
    questions = _read_questions(args.in_path)

    # Pass 1: warm up (imports, indexes, first-touch caches). Discarded.
    _run_pass(agent, questions, measure=False)

    # Pass 2: measured. This is what results.jsonl records.
    results, branch_times = _run_pass(agent, questions, measure=True)

    with open(args.out_path, "w", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    from agent.timing import summarize
    all_ms = [row["ms"] for row in results]
    p = summarize([float(x) for x in all_ms])
    print(f"wrote {args.out_path}: {len(results)} answers", file=sys.stderr)
    print(f"p50={p.p50:.0f}ms p95={p.p95:.0f}ms p99={p.p99:.0f}ms "
          f"max={p.max:.0f}ms", file=sys.stderr)

    if args.bench:
        from agent.timing import format_by_branch
        print("\n" + format_by_branch(branch_times), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())