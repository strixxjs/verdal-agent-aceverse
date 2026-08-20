"""Offline generation of aliases and policy facts.

Runs via `make build`, OUTSIDE the measured hot path. Reads store.json,
asks the LLM once for Ukrainian product aliases and once for structured
policy numbers, and writes data/facts.json. That file is committed; the
hot path reads it and never calls this module.

Rerun only when store.json changes. If the LLM is unavailable the previous
data/facts.json stays valid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"

ALIAS_SYSTEM = (
    "You map an English e-commerce catalogue to the Ukrainian phrases customers "
    "actually use when speaking. Return STRICT JSON only, no prose, no markdown."
)

FACTS_SYSTEM = (
    "You extract exact numbers from store policy text into structured JSON. "
    "Copy numbers verbatim from the text. Never invent a value that is not "
    "stated. Return STRICT JSON only, no prose, no markdown."
)


def _post(api_key: str, system: str, user: str) -> str:
    """One Groq call. Retries on 429 using the delay Groq reports."""

    for _ in range(6):
        response = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": MODEL,
                "temperature": 0,
                "max_tokens": 1200,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=60.0,
        )
        if response.status_code == 429:
            match = re.search(r"try again in ([\d.]+)s", response.text)
            wait = float(match.group(1)) + 1 if match else 15.0
            print(f"    rate limited, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"Groq {response.status_code}: {response.text}")
        return response.json()["choices"][0]["message"]["content"]

    raise RuntimeError("Groq: still rate limited after 6 retries")


def _parse_json(raw: str) -> Any:
    """Groq with json_object should return clean JSON, but strip fences defensively."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def build_aliases(api_key: str, products: list[dict]) -> dict[str, list[str]]:
    """Ask for Ukrainian aliases, one product per request.

    One request per product keeps every call well under the 8000 TPM free-tier
    limit. We deliberately do not send the sample questions — aliases tuned to
    the visible 39 questions would be tuning to the test.
    """
    known = {p["id"] for p in products}
    result: dict[str, list[str]] = {}

    for product in products:
        pid, title = product["id"], product["title"]
        user = (
            f'Product: "{title}".\n\n'
            "List Ukrainian phrases a customer might say to refer to this product: "
            "the natural translation, common synonyms, shortened forms and likely "
            "misspellings. Include the transliterated English name if customers "
            "would use it.\n\n"
            'Return JSON: {"aliases": [...]}. 6 to 10 lowercase Ukrainian strings. '
            "No English except brand-like names that stay untranslated."
        )
        data = _parse_json(_post(api_key, ALIAS_SYSTEM, user))

        seen: dict[str, None] = {}
        for alias in data.get("aliases", []):
            key = str(alias).strip().lower()
            if key:
                seen.setdefault(key, None)
        result[pid] = list(seen)
        print(f"  {pid}: {len(result[pid])} aliases")

    missing = known - set(result)
    if missing:
        print(f"  warning: no aliases for {sorted(missing)}", file=sys.stderr)

    return result


def build_policy_facts(api_key: str, policies: dict[str, str]) -> dict[str, Any]:
    """Extract the numbers the arithmetic handlers need, straight from policy text."""
    user = (
        "Extract these values from the policy text. Use exactly these JSON keys. "
        "If a value is not stated, use null. Copy numbers verbatim.\n\n"
        "{\n"
        '  "standard_shipping_cost": number,\n'
        '  "express_shipping_cost": number,\n'
        '  "free_shipping_threshold": number,\n'
        '  "standard_delivery_days": string,\n'
        '  "express_delivery_days": string,\n'
        '  "return_window_days": number,\n'
        '  "refund_business_days": number,\n'
        '  "damage_report_hours": number,\n'
        '  "ships_outside_eu": boolean,\n'
        '  "cash_on_delivery": boolean,\n'
        '  "currency": string\n'
        "}\n\n"
        f"Policy text:\n{json.dumps(policies, ensure_ascii=False, indent=2)}"
    )
    return _parse_json(_post(api_key, FACTS_SYSTEM, user))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default="data/store.json")
    parser.add_argument("--out", default="data/facts.json")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY is not set. This is the offline build step and it "
              "requires a key. The hot path does not.", file=sys.stderr)
        return 1

    store = json.loads(Path(args.store).read_text(encoding="utf-8"))

    print("generating product aliases...")
    aliases = build_aliases(api_key, store["products"])
    total = sum(len(v) for v in aliases.values())
    print(f"  {len(aliases)} products, {total} aliases total")

    print("extracting policy facts...")
    facts = build_policy_facts(api_key, store["policies"])
    print(f"  {len([v for v in facts.values() if v is not None])} values extracted")

    out = {
        "_generated_by": f"build_facts.py via Groq {MODEL}",
        "_note": "Offline artifact. Regenerate only when store.json changes.",
        "aliases": aliases,
        "policy_facts": facts,
    }
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())