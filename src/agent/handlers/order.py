"""Order questions: status, tracking, items, shipping address."""

from __future__ import annotations

from agent.handlers import Answer
from agent.normalize import NormalizedQuery
from agent.resolve import Resolution
from agent.store import Order, Store

# Ukrainian phrasing for the English status codes in store.json.
_STATUS_UK = {
    "processing": "обробляється",
    "shipped": "відправлено",
    "delivered": "доставлено",
    "cancelled": "скасовано",
}


def _status_word(status: str) -> str:
    return _STATUS_UK.get(status, status)


def _not_found(query: NormalizedQuery) -> Answer:
    number = query.order_number or "—"
    return Answer(
        text=f"Замовлення #{number} не знайдено. Перевірте номер, будь ласка.",
        branch="order_not_found",
        confident=True,
    )


def handle_status(order: Order) -> Answer:
    text = f"Замовлення {order.display}: {_status_word(order.status)}."
    if order.status == "shipped" and order.tracking:
        text += f" Трек-номер: {order.tracking}."
    elif order.status == "cancelled":
        text += " Кошти повертаються на початковий спосіб оплати."
    return Answer(text=text, branch="order_status", confident=True)


def handle_tracking(order: Order) -> Answer:
    if order.tracking:
        return Answer(
            text=f"Трек-номер замовлення {order.display}: {order.tracking}.",
            branch="order_tracking",
            confident=True,
        )
    # Order exists but has no tracking yet (processing/cancelled).
    return Answer(
        text=(
            f"У замовлення {order.display} ще немає трек-номера "
            f"(статус: {_status_word(order.status)})."
        ),
        branch="order_tracking_none",
        confident=True,
    )


def handle_items(order: Order) -> Answer:
    lines = []
    for item in order.items:
        part = item.title
        if item.variant:
            part += f" ({item.variant})"
        if item.qty > 1:
            part += f" ×{item.qty}"
        lines.append(part)
    listing = "; ".join(lines) if lines else "немає позицій"
    return Answer(
        text=f"Замовлення {order.display} містить: {listing}.",
        branch="order_items",
        confident=True,
    )


def handle_address(order: Order) -> Answer:
    # store.json only carries the destination country code, nothing more.
    return Answer(
        text=(
            f"Замовлення {order.display} відправляється до країни: "
            f"{order.ships_to}. Точнішої адреси в системі немає."
        ),
        branch="order_address",
        confident=True,
    )


def handle(intent: str, query: NormalizedQuery, resolution: Resolution,
           store: Store) -> Answer:
    """Dispatch an order intent. Missing order -> honest not-found."""
    order = resolution.order
    if order is None:
        return _not_found(query)

    if intent == "ORDER_TRACKING":
        return handle_tracking(order)
    if intent == "ORDER_ITEMS":
        return handle_items(order)
    if intent == "ORDER_ADDRESS":
        return handle_address(order)
    return handle_status(order)