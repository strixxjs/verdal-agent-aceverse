"""Load store.json and build the in-memory indexes used on the hot path.

Everything here runs once at startup. The hot path only reads the indexes
built below; it never scans the raw lists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

SizeKind = Literal["letter", "numeric", "range", "one", "other", "none"]

LETTER_SIZES = frozenset({"XS", "S", "M", "L", "XL", "XXL"})
ONE_SIZE_TOKENS = frozenset({"one", "one size", "onesize", "os"})

_RANGE_RE = re.compile(r"^(\d{2})\s*-\s*(\d{2})$")
_NUMERIC_RE = re.compile(r"^\d{2,3}$")
_ORDER_DIGITS_RE = re.compile(r"\d+")


class StoreDataError(ValueError):
    """Raised when store.json does not match the expected shape."""


def normalize_size(token: str) -> tuple[str | None, SizeKind, tuple[int, int] | None]:
    """Normalize a raw size token.

    Returns (canonical, kind, numeric_span). The span is inclusive and is set
    for numeric and range sizes so that "44" can be matched against "43-46".
    """
    raw = token.strip()
    if not raw:
        return None, "none", None

    lowered = raw.lower()
    if lowered in ONE_SIZE_TOKENS:
        return "one", "one", None

    span = _RANGE_RE.match(raw)
    if span:
        low, high = int(span.group(1)), int(span.group(2))
        if low > high:
            low, high = high, low
        return f"{low}-{high}", "range", (low, high)

    if _NUMERIC_RE.match(raw):
        value = int(raw)
        return raw, "numeric", (value, value)

    upper = raw.upper()
    if upper in LETTER_SIZES:
        return upper, "letter", None

    return raw, "other", None


def normalize_order_number(token: str) -> str | None:
    """Reduce '#1005', '1005', 'номер 1005' to the bare digits '1005'."""
    match = _ORDER_DIGITS_RE.search(token)
    return match.group(0) if match else None


@dataclass(frozen=True, slots=True)
class Variant:
    raw: str
    size: str | None
    size_kind: SizeKind
    numeric_span: tuple[int, int] | None
    colour: str | None
    stock: int

    @property
    def in_stock(self) -> bool:
        return self.stock > 0

    def matches_size(self, wanted: str | None) -> bool:
        """A None query matches any variant. Numeric queries fall inside ranges."""
        if wanted is None:
            return True

        canonical, kind, span = normalize_size(wanted)
        if canonical is None:
            return True

        if self.size is not None and canonical == self.size:
            return True

        # "44" must match the "43-46" sock variant.
        if kind == "numeric" and self.size_kind == "range" and self.numeric_span:
            low, high = self.numeric_span
            return low <= span[0] <= high

        return False

    def matches_colour(self, wanted: str | None) -> bool:
        if wanted is None:
            return True
        if self.colour is None:
            # A variant with no colour axis cannot contradict a colour request.
            return False
        return self.colour == wanted.strip().lower()

    def matches(self, size: str | None = None, colour: str | None = None) -> bool:
        return self.matches_size(size) and self.matches_colour(colour)


def parse_variant(raw: str, stock: int) -> Variant:
    """Parse the variant string.

    store.json uses several schemas: 'M / navy', '40 / grey', 'one / black',
    '39-42', 'one', '34', 'L'. All of them must survive this function.
    """
    parts = [part.strip() for part in raw.split("/")]
    size_token = parts[0] if parts else ""
    colour = parts[1].lower() if len(parts) > 1 and parts[1] else None

    size, kind, span = normalize_size(size_token)
    return Variant(
        raw=raw,
        size=size,
        size_kind=kind,
        numeric_span=span,
        colour=colour,
        stock=stock,
    )


@dataclass(frozen=True, slots=True)
class Product:
    id: str
    title: str
    price: float
    currency: str
    variants: tuple[Variant, ...]

    @property
    def any_in_stock(self) -> bool:
        return any(variant.in_stock for variant in self.variants)

    def find(self, size: str | None = None, colour: str | None = None) -> list[Variant]:
        return [v for v in self.variants if v.matches(size, colour)]

    @property
    def colours(self) -> list[str]:
        seen: dict[str, None] = {}
        for variant in self.variants:
            if variant.colour:
                seen.setdefault(variant.colour, None)
        return list(seen)


@dataclass(frozen=True, slots=True)
class OrderItem:
    product_id: str
    title: str
    variant: str
    qty: int


@dataclass(frozen=True, slots=True)
class Order:
    number: str          # bare digits, e.g. "1005"
    display: str         # as printed in the data, e.g. "#1005"
    status: str
    tracking: str | None
    placed_at: str
    ships_to: str
    items: tuple[OrderItem, ...]

    @property
    def has_tracking(self) -> bool:
        return bool(self.tracking)


@dataclass(slots=True)
class Store:
    shop: str
    currency: str
    products_by_id: dict[str, Product]
    orders_by_number: dict[str, Order]
    policies: dict[str, str]
    variant_index: dict[str, tuple[Variant, ...]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Store":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))

        for key in ("shop", "products", "orders", "policies"):
            if key not in payload:
                raise StoreDataError(f"store.json is missing the '{key}' key")

        currency = payload.get("currency", "EUR")

        products_by_id: dict[str, Product] = {}
        variant_index: dict[str, tuple[Variant, ...]] = {}

        for entry in payload["products"]:
            variants = tuple(
                parse_variant(v["variant"], int(v["stock"]))
                for v in entry.get("variants", [])
            )
            product = Product(
                id=entry["id"],
                title=entry["title"],
                price=float(entry["price"]),
                currency=entry.get("currency", currency),
                variants=variants,
            )
            if product.id in products_by_id:
                raise StoreDataError(f"duplicate product id: {product.id}")
            products_by_id[product.id] = product
            variant_index[product.id] = variants

        orders_by_number: dict[str, Order] = {}
        for entry in payload["orders"]:
            display = entry["name"]
            number = normalize_order_number(display)
            if number is None:
                raise StoreDataError(f"order name without digits: {display!r}")

            items = tuple(
                OrderItem(
                    product_id=item["product_id"],
                    title=item["title"],
                    variant=item["variant"],
                    qty=int(item["qty"]),
                )
                for item in entry.get("items", [])
            )
            order = Order(
                number=number,
                display=display,
                status=entry["status"],
                tracking=entry.get("tracking"),
                placed_at=entry["placed_at"],
                ships_to=entry["ships_to"],
                items=items,
            )
            if number in orders_by_number:
                raise StoreDataError(f"duplicate order number: {display}")
            orders_by_number[number] = order

        return cls(
            shop=payload["shop"],
            currency=currency,
            products_by_id=products_by_id,
            orders_by_number=orders_by_number,
            policies=dict(payload["policies"]),
            variant_index=variant_index,
        )

    # --- hot path lookups -------------------------------------------------

    def order(self, token: str) -> Order | None:
        number = normalize_order_number(token)
        return self.orders_by_number.get(number) if number else None

    def product(self, product_id: str) -> Product | None:
        return self.products_by_id.get(product_id)

    def policy(self, name: str) -> str | None:
        return self.policies.get(name)

    def iter_products(self) -> Iterator[Product]:
        return iter(self.products_by_id.values())


if __name__ == "__main__":
    import sys

    store = Store.load(sys.argv[1] if len(sys.argv) > 1 else "data/store.json")

    print(f"shop={store.shop} currency={store.currency}")
    print(f"products={len(store.products_by_id)}")
    print(f"orders={len(store.orders_by_number)}")
    print(f"policies={len(store.policies)} -> {sorted(store.policies)}")

    kinds: dict[str, int] = {}
    total_variants = 0
    for product in store.iter_products():
        for variant in product.variants:
            kinds[variant.size_kind] = kinds.get(variant.size_kind, 0) + 1
            total_variants += 1
    print(f"variants={total_variants} by kind={kinds}")

    print("\n-- variant parsing spot checks --")
    for pid, size, colour in [
        ("p-001", "M", "navy"),    # expect stock 0
        ("p-001", "M", "beige"),   # expect stock 12
        ("p-007", "44", None),     # expect the 43-46 range to match
        ("p-015", "34", None),     # expect stock 0
        ("p-012", None, None),     # single 'one' variant
    ]:
        product = store.product(pid)
        found = product.find(size, colour)
        stocks = [(v.raw, v.stock) for v in found]
        print(f"{pid} size={size} colour={colour} -> {stocks}")

    print("\n-- order lookups --")
    for token in ["#1005", "1002", "#9999"]:
        order = store.order(token)
        print(f"{token} -> {order.status if order else 'NOT FOUND'}")