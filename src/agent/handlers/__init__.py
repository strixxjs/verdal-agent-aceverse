"""Handlers turn a routed question into a final Ukrainian answer.

Each handler takes (query, resolution, store, facts) and returns an Answer.
No network, no LLM. Every value comes from the store or facts.json.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Answer:
    text: str          # the Ukrainian answer shown to the customer
    branch: str        # which path produced it, for the latency breakdown
    confident: bool    # False -> caller may prefer the LLM tail