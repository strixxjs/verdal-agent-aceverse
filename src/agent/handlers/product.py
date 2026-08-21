"""Product questions: price and stock, at the variant level where possible."""

from __future__ import annotations

from agent.handlers import Answer
from agent.normalize import NormalizedQuery
from agent.resolve import Resolution
from agent.store import Product, Store, Variant


def _price(product: Product) -> str:
    # store.json prices are whole euros; keep them clean.
    value = product.price
    shown = int(value) if value == int(value) else value
    return f"{shown} {product.currency}"


def handle_price(product: Product) -> Answer:
    return Answer(
        text=f"{product.title} коштує {_price(product)}.",
        branch="product_price",
        confident=True,
    )


def _describe_variant(variant: Variant) -> str:
    """Human label for a variant: 'M / beige' -> 'M / beige'."""
    return variant.raw


def handle_stock(query: NormalizedQuery, product: Product,
                 resolution: Resolution) -> Answer:
    """Answer availability.

    Three cases:
    1. A specific variant was resolved -> report that exact variant.
    2. Size/colour asked but no matching variant exists -> say it's unavailable,
       and (helpfully) whether the product exists in other variants.
    3. No size/colour asked -> report the product's overall availability.
    """
    # Drop a "size" the product cannot have on any variant — it is a token
    # from the product name ("Hiking Backpack 35L" -> "35"/"L"), not a
    # variant request. Mirrors the same guard in resolve.resolve().
    size = query.size
    if size is not None and not product.size_axis_matches(size):
        size = None
    asked_specific = size is not None or query.colour is not None

    # Case 1: a concrete variant resolved.
    if resolution.variant is not None:
        v = resolution.variant
        if v.in_stock:
            return Answer(
                text=f"{product.title} ({_describe_variant(v)}) — у наявності "
                     f"({v.stock} шт).",
                branch="product_stock_variant",
                confident=True,
            )
        return Answer(
            text=f"{product.title} ({_describe_variant(v)}) — зараз немає в наявності.",
            branch="product_stock_variant",
            confident=True,
        )

    # Case 2: a specific size/colour was asked but nothing matched.
    if asked_specific:
        matches = product.find(size, query.colour)
        if not matches:
            return Answer(
                text=(
                    f"{product.title} у такому варіанті "
                    f"(розмір/колір) немає. Уточніть, будь ласка, "
                    f"інший розмір чи колір."
                ),
                branch="product_stock_no_variant",
                confident=True,
            )
        # find() returned multiple (e.g. colour matched, size didn't narrow) —
        # report which of them are in stock.
        available = [m for m in matches if m.in_stock]
        if available:
            listing = ", ".join(_describe_variant(m) for m in available)
            return Answer(
                text=f"{product.title} — у наявності: {listing}.",
                branch="product_stock_partial",
                confident=True,
            )
        return Answer(
            text=f"{product.title} у запитаному варіанті зараз немає.",
            branch="product_stock_partial",
            confident=True,
        )

    # Case 3: no variant specified -> overall availability.
    in_stock = [v for v in product.variants if v.in_stock]
    if not in_stock:
        return Answer(
            text=f"{product.title} зараз повністю відсутній.",
            branch="product_stock_overall",
            confident=True,
        )
    listing = ", ".join(_describe_variant(v) for v in in_stock)
    return Answer(
        text=f"{product.title} — у наявності: {listing}.",
        branch="product_stock_overall",
        confident=True,
    )


def handle(intent: str, query: NormalizedQuery, resolution: Resolution,
           store: Store) -> Answer:
    product = resolution.product
    if product is None:
        # Router said product intent but nothing resolved — let caller fall back.
        return Answer(
            text="Не вдалося визначити товар. Уточніть назву, будь ласка.",
            branch="product_unresolved",
            confident=False,
        )

    if intent == "PRODUCT_PRICE":
        return handle_price(product)
    return handle_stock(query, product, resolution)