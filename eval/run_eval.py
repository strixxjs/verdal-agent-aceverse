"""Behaviour-based evaluation.

Runs each case in cases.yaml through the real Agent pipeline and checks the
answer against must_include / must_exclude substrings. The criterion is
expected behaviour, not exact text — so these stay valid under rewording.

Exit code is non-zero if any case fails, so this can gate CI later.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.cli import Agent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CASES = Path(__file__).resolve().parent / "cases.yaml"


def _check(answer: str, case: dict) -> list[str]:
    """Return a list of failure descriptions; empty means the case passed."""
    failures = []
    low = answer.lower()

    for needle in case.get("must_include", []):
        if needle.lower() not in low:
            failures.append(f"missing '{needle}'")

    for needle in case.get("must_exclude", []):
        if needle.lower() in low:
            failures.append(f"should not contain '{needle}'")

    return failures


def main() -> int:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]

    agent = Agent(ROOT / "data" / "store.json", ROOT / "data" / "facts.json")

    passed = 0
    failed = 0
    for case in cases:
        answer = agent.answer(case["q"]).text
        failures = _check(answer, case)
        if failures:
            failed += 1
            print(f"FAIL {case['id']}")
            print(f"     q: {case['q']}")
            print(f"     a: {answer}")
            for f in failures:
                print(f"     - {f}")
        else:
            passed += 1
            print(f"ok   {case['id']}")

    total = passed + failed
    print(f"\n{passed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())