"""Arithmetic questions: line totals, shipping cost, free-shipping threshold.

Prices always come from store.json, never from the number the customer said
in the question. A customer may quote a wrong price; a store agent must not
repeat it. The question's own numbers are used only for quantities.
"""

from __future__ import annotations

from typing import Any

from agent.handlers import Answer
from agent.normalize import STEM_PREFIX_LEN, NormalizedQuery
from agent.resolve import Resolution
from agent.store import Product, Store


def _price(product: Product) -> float:
    return product.price


def _fmt(value: float) -> str:
    shown = int(value) if value == int(value) else round(value, 2)
    return f"{shown}"


def _quantity(query: NormalizedQuery) -> int:
    """Quantity the customer mentioned, default 1.

    numbers already includes cardinal words ('два' -> 2). We take the first
    number that is a plausible small quantity (1..10) and is not the size.
    """
    for n in query.numbers:
        if 1 <= n <= 10 and n != _size_as_int(query):
            return n
    return 1


def _size_as_int(query: NormalizedQuery) -> int | None:
    if query.size and query.size.isdigit():
        return int(query.size)
    return None


def _root(word: str) -> str:
    """Truncate a root the same way normalize._stem truncates tokens.

    query.tokens are stemmed to STEM_PREFIX_LEN characters, so a root longer
    than that ("доставка") can never match via startswith — it must be cut
    to the same length first. Same rule as router._has_root."""
    return word[:STEM_PREFIX_LEN]


def _shipping_signal(query: NormalizedQuery) -> str | None:
    """Which shipping tier the question refers to, if any."""
    tokens = query.tokens
    if any(t.startswith(_root("експрес")) for t in tokens):
        return "express"
    if any(
        t.startswith(_root("доставка")) or t.startswith(_root("стандартна"))
        for t in tokens
    ):
        return "standard"
    return None


def _incomplete() -> Answer:
    return Answer(
        text="Уточніть, будь ласка, товар і тип доставки для розрахунку.",
        branch="compute_incomplete",
        confident=False,
    )


def handle(query: NormalizedQuery, resolution: Resolution, store: Store,
           facts: dict[str, Any]) -> Answer:
    # No silent numeric defaults: a missing policy fact must degrade to the
    # tail, not compute with 0 (threshold 0 would make EVERY order "free
    # shipping").
    pf = facts.get("policy_facts", {})
    std = pf.get("standard_shipping_cost")
    exp = pf.get("express_shipping_cost")
    threshold = pf.get("free_shipping_threshold")

    product = resolution.product
    qty = _quantity(query)
    tier = _shipping_signal(query)

    # --- free-shipping-threshold question (q020, q031) ---
    wants_free = any(t.startswith("безко") for t in query.tokens)
    if wants_free:
        if threshold is None:
            return _incomplete()
        if product is not None:
            goods = _price(product) * qty
            if goods >= threshold:
                return Answer(
                    text=(
                        f"{qty}×{product.title} = {_fmt(goods)} EUR, "
                        f"це від {threshold} EUR — доставка безкоштовна."
                    ),
                    branch="compute_free_shipping",
                    confident=True,
                )
            need = threshold - goods
            return Answer(
                text=(
                    f"{qty}×{product.title} = {_fmt(goods)} EUR. "
                    f"Безкоштовна доставка від {threshold} EUR — "
                    f"не вистачає {_fmt(need)} EUR."
                ),
                branch="compute_free_shipping",
                confident=True,
            )
        # No product — just state the rule.
        return Answer(
            text=f"Доставка безкоштовна для замовлень від {threshold} EUR.",
            branch="compute_free_shipping_rule",
            confident=True,
        )

    # --- goods + shipping total (q007, q037) ---
    if product is not None and tier is not None:
        goods = _price(product) * qty
        ship = exp if tier == "express" else std
        if ship is None or threshold is None:
            return _incomplete()
        ship_word = "експрес" if tier == "express" else "стандартна"
        # store policy: free over threshold
        if goods >= threshold:
            return Answer(
                text=(
                    f"{qty}×{product.title} = {_fmt(goods)} EUR, "
                    f"доставка безкоштовна (від {threshold} EUR). "
                    f"Разом: {_fmt(goods)} EUR."
                ),
                branch="compute_total_free_ship",
                confident=True,
            )
        total = goods + ship
        qty_part = f"{qty}×" if qty > 1 else ""
        return Answer(
            text=(
                f"{qty_part}{product.title} {_fmt(goods)} EUR + "
                f"{ship_word} доставка {_fmt(ship)} EUR = {_fmt(total)} EUR."
            ),
            branch="compute_total",
            confident=True,
        )

    # --- goods-only line total ("скільки разом за два светри") ---
    # Only with an explicit quantity: a qty of 1 here usually means the
    # question names several products ("светр і шапка разом") and the
    # resolver carries just one — that belongs to the tail, not a confident
    # single-product answer.
    if product is not None and qty > 1:
        goods = _price(product) * qty
        return Answer(
            text=f"{qty}×{product.title} = {_fmt(goods)} EUR.",
            branch="compute_line_total",
            confident=True,
        )

    # --- couldn't assemble the arithmetic confidently ---
    return _incomplete()