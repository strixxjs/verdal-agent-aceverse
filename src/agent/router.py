"""Route a NormalizedQuery + Resolution to an Intent.

Keyword/signal driven over query.tokens (already stemmed) and the
Resolution produced by resolve.py. Pure, no I/O, no network. Priority is
strict: the first matching rule wins and the function returns immediately,
which is what lets POLICY_DAMAGED and REFUSE override a product match that
resolve.py may have found anyway (e.g. a damage complaint that happens to
name a product is still a damage complaint).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .normalize import STEM_PREFIX_LEN, NormalizedQuery
from .resolve import Resolution


class Intent(Enum):
    ORDER_STATUS = "ORDER_STATUS"
    ORDER_TRACKING = "ORDER_TRACKING"
    ORDER_ITEMS = "ORDER_ITEMS"
    ORDER_ADDRESS = "ORDER_ADDRESS"
    PRODUCT_PRICE = "PRODUCT_PRICE"
    PRODUCT_STOCK = "PRODUCT_STOCK"
    PRODUCT_COMPARE = "PRODUCT_COMPARE"
    COMPUTE_TOTAL = "COMPUTE_TOTAL"
    POLICY_RETURNS = "POLICY_RETURNS"
    POLICY_EXCHANGE = "POLICY_EXCHANGE"
    POLICY_SHIPPING = "POLICY_SHIPPING"
    POLICY_PAYMENT = "POLICY_PAYMENT"
    POLICY_DAMAGED = "POLICY_DAMAGED"
    NO_DATA = "NO_DATA"
    REFUSE = "REFUSE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Route:
    intent: Intent
    confident: bool


# Root tables. Each root is compared against query.tokens (already stemmed
# by normalize._stem: first STEM_PREFIX_LEN Cyrillic chars, or the whole
# word if shorter) after being truncated the same way, so a root like
# "пошкодж" correctly matches a token stemmed from "пошкоджена" regardless
# of which string is longer.
TRACKING_ROOTS = ("трек", "track", "відсте", "накладн")
ADDRESS_ROOTS = ("адрес", "куди", "країн", "місто", "місті")
ITEMS_ROOTS = ("вход", "товар", "позиц", "містит", "перелі", "всеред", "списо")

DAMAGE_ROOTS = ("плям", "брак", "дефект", "розірв", "пошкодж", "зламан")
# "зворот" covers "зворотна пересилка / зворотна доставка" — who pays for
# the return leg is answered verbatim by the returns policy.
RETURN_ROOTS = ("поверн", "відмов", "зворот")
EXCHANGE_ROOTS = ("обмін", "поміня", "підійд")
SHIPPING_ROOTS = ("достав", "відправ", "британ", "кордон", "експрес")
PAYMENT_ROOTS = ("оплат", "картк", "наложен", "платіж", "спишуть")

DISCOUNT_ROOTS = ("знижк", "скидк")
FREE_ROOTS = ("безко",)
# Personal-request markers: a discount *demand* is refused, a discount
# *question* is answered as "no such data" (store.json has no discounts).
GIVE_ROOTS = ("дай", "надай", "зроби", "хочу", "прошу", "можна", "можеш")

RESTOCK_ROOTS = ("завез",)

# "цін" (not "ціна") so oblique forms match: ціну, ціни, ціною.
PRICE_ROOTS = ("цін", "кошт", "почім", "скіль", "варт")

COMPARE_ROOTS = ("дешевш", "дорожч", "кращ", "порівн")
CONJUNCTION_TOKENS = ("чи", "або", "і", "та")

TOGETHER_ROOTS = ("разом",)

# "скільки йде доставка" asks for delivery TIME, not cost — routed to the
# shipping policy even when a product is named, unless an explicit cost
# word is present.
DURATION_ROOTS = ("йде", "йти", "днів", "дні", "довго", "трива", "швидк")
COST_ROOTS = ("кошт", "варт", "цін", "почім", "євро", "eur")

SIZE_ROOT = "розмір"
NEGATION_TOKENS = ("не",)
# "не той/такий розмір" — the demonstrative must directly follow the
# negation, otherwise polite phrasing ("не підкажете, чи є 43 розмір")
# would be misread as a wrong-size complaint.
WRONG_SIZE_DEMONSTRATIVES = frozenset({"той", "та", "те", "ті", "ту", "мій"})


def _has_root(tokens: tuple[str, ...], roots: tuple[str, ...]) -> bool:
    stems = {root[:STEM_PREFIX_LEN] for root in roots}
    return any(token.startswith(stem) for token in tokens for stem in stems)


def _has_exact(tokens: tuple[str, ...], words: tuple[str, ...]) -> bool:
    wanted = set(words)
    return any(token in wanted for token in tokens)


def _wrong_size_phrase(tokens: tuple[str, ...]) -> bool:
    """'не той розмір' style phrasing: negation + demonstrative + size word.

    The demonstrative must be adjacent to the negation ("не той", "не такий"),
    otherwise a polite "Не підкажете, чи є 43 розмір?" would be misrouted.
    """
    if not _has_root(tokens, (SIZE_ROOT,)):
        return False
    for i, token in enumerate(tokens[:-1]):
        if token in NEGATION_TOKENS:
            nxt = tokens[i + 1]
            if nxt in WRONG_SIZE_DEMONSTRATIVES or nxt.startswith("так"):
                return True
    return False


def _is_refuse(tokens: tuple[str, ...]) -> bool:
    # A discount is refused only when it is asked FOR ("дай знижку");
    # a question ABOUT discounts routes to NO_DATA instead (see route()).
    if _has_root(tokens, DISCOUNT_ROOTS) and _has_root(tokens, GIVE_ROOTS):
        return True
    return _has_root(tokens, FREE_ROOTS) and _has_root(tokens, GIVE_ROOTS)


def _is_compare(tokens: tuple[str, ...]) -> bool:
    if _has_root(tokens, ("порівн",)):
        return True
    return _has_root(tokens, COMPARE_ROOTS) and _has_exact(tokens, CONJUNCTION_TOKENS)


def _is_compute(query: NormalizedQuery, resolution: Resolution) -> bool:
    tokens = query.tokens
    if _has_root(tokens, TOGETHER_ROOTS):
        return True
    if _has_root(tokens, FREE_ROOTS) and _has_root(tokens, SHIPPING_ROOTS):
        return True
    # A resolved product or a bare price given in the question itself (e.g.
    # "шапку за 25 євро") both count as the "thing" being combined with
    # shipping cost.
    if (
        (resolution.product is not None or query.numbers)
        and _has_root(tokens, SHIPPING_ROOTS)
        and _has_root(tokens, PRICE_ROOTS)
    ):
        return True
    return False


def route(query: NormalizedQuery, resolution: Resolution) -> Route:
    tokens = query.tokens

    # 1. ORDER — gated on order context, never falls through below.
    if query.order_number is not None or resolution.order is not None:
        if query.order_number is not None and resolution.order is None:
            return Route(Intent.ORDER_STATUS, True)
        if _has_root(tokens, TRACKING_ROOTS):
            return Route(Intent.ORDER_TRACKING, True)
        if _has_root(tokens, ADDRESS_ROOTS):
            return Route(Intent.ORDER_ADDRESS, True)
        if _has_root(tokens, ITEMS_ROOTS):
            return Route(Intent.ORDER_ITEMS, True)
        return Route(Intent.ORDER_STATUS, True)

    # 2. POLICY_DAMAGED — overrides any product match, checked independent
    #    of resolution.product.
    if _has_root(tokens, DAMAGE_ROOTS):
        return Route(Intent.POLICY_DAMAGED, True)

    # 3. REFUSE — hard override, independent of resolution. A question
    #    ABOUT discounts (rather than a demand for one) is honest NO_DATA:
    #    store.json carries no discount information.
    if _is_refuse(tokens):
        return Route(Intent.REFUSE, True)
    if _has_root(tokens, DISCOUNT_ROOTS):
        return Route(Intent.NO_DATA, True)

    # 4. PRODUCT_COMPARE — Resolution only ever carries one product, so this
    #    is flagged rather than answered.
    if _is_compare(tokens):
        return Route(Intent.PRODUCT_COMPARE, False)

    # 5. Shipping duration ("скільки йде доставка светра?") is a policy
    #    question even when a product is named — delivery time does not
    #    depend on the product. Explicit cost words override this.
    if (
        _has_root(tokens, SHIPPING_ROOTS)
        and _has_root(tokens, DURATION_ROOTS)
        and not _has_root(tokens, COST_ROOTS)
    ):
        return Route(Intent.POLICY_SHIPPING, True)

    # 6. COMPUTE_TOTAL
    if _is_compute(query, resolution):
        return Route(Intent.COMPUTE_TOTAL, True)

    # 7. POLICY (independent of product resolution)
    if _has_root(tokens, RETURN_ROOTS):
        return Route(Intent.POLICY_RETURNS, True)
    if _has_root(tokens, EXCHANGE_ROOTS) or _wrong_size_phrase(tokens):
        return Route(Intent.POLICY_EXCHANGE, True)
    if resolution.product is None and _has_root(tokens, SHIPPING_ROOTS):
        return Route(Intent.POLICY_SHIPPING, True)
    if _has_root(tokens, PAYMENT_ROOTS):
        return Route(Intent.POLICY_PAYMENT, True)

    # 8. NO_DATA — store.json never has restock dates, under any phrasing.
    if _has_root(tokens, RESTOCK_ROOTS):
        return Route(Intent.NO_DATA, True)

    # 9. PRODUCT — stock is the default: explicit STOCK_ROOTS/"є" or no
    #    clear signal at all both land here, matching the spec's rule that
    #    an unmarked product question defaults to stock.
    if resolution.product is not None:
        if _has_root(tokens, PRICE_ROOTS):
            return Route(Intent.PRODUCT_PRICE, True)
        return Route(Intent.PRODUCT_STOCK, True)

    # 10. UNKNOWN — nothing matched, let the LLM tail handle it.
    return Route(Intent.UNKNOWN, False)


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    from .normalize import normalize
    from .resolve import AliasIndex, resolve
    from .store import Store

    store = Store.load("data/store.json")
    index = AliasIndex.load(store, "data/facts.json")

    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/questions.jsonl")
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        query = normalize(row["q"])
        resolution = resolve(query, store, index)
        result = route(query, resolution)

        counts[result.intent.value] = counts.get(result.intent.value, 0) + 1

        product = resolution.product.title if resolution.product else None
        order = resolution.order.display if resolution.order else None
        print(
            f"{row['id']}: intent={result.intent.value} confident={result.confident} "
            f"product={product!r} order={order!r}"
        )

    print("\n-- intent distribution --")
    for intent, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{intent}: {count}")