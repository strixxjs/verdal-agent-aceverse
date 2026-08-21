"""Policy questions: returns, exchange, shipping, payment, damage.

Numbers come from facts.json['policy_facts'], extracted offline from the
English policy text. Nothing here is hardcoded from the store data directly,
so if a policy value changes, regenerating facts.json updates the answers.
"""

from __future__ import annotations

from typing import Any

from agent.handlers import Answer
from agent.normalize import NormalizedQuery
from agent.resolve import Resolution
from agent.store import Store


def _fact(facts: dict[str, Any], key: str, default: Any = None) -> Any:
    return facts.get("policy_facts", {}).get(key, default)


def _missing_fact() -> Answer:
    """A required policy number is absent from facts.json (offline extraction
    returned null). Never interpolate None into customer text — degrade so
    the caller can fall back to the tail, which has the raw policy text."""
    return Answer(
        text="Уточніть, будь ласка, ваше питання.",
        branch="policy_missing_fact",
        confident=False,
    )


def handle_returns(facts: dict[str, Any]) -> Answer:
    days = _fact(facts, "return_window_days")
    refund = _fact(facts, "refund_business_days")
    if days is None or refund is None:
        return _missing_fact()
    text = (
        f"Повернути товар можна протягом {days} днів від отримання, "
        f"якщо він не ношений, не праний і з бирками. "
        f"Кошти повертаються на початковий спосіб оплати протягом "
        f"{refund} робочих днів після надходження посилки на склад. "
        f"Зворотну пересилку оплачує покупець, окрім бракованих чи "
        f"помилково надісланих товарів."
    )
    return Answer(text=text, branch="policy_returns", confident=True)


def handle_exchange(facts: dict[str, Any]) -> Answer:
    days = _fact(facts, "return_window_days")
    if days is None:
        return _missing_fact()
    text = (
        f"Один безкоштовний обмін розміру на замовлення протягом {days} днів "
        f"від отримання, якщо потрібний розмір є в наявності. Заміну "
        f"надсилаємо, щойно оригінал передано перевізнику. Обмін кольору "
        f"оформлюється як повернення плюс нове замовлення."
    )
    return Answer(text=text, branch="policy_exchange", confident=True)


def _uk_days(raw: str | None) -> str:
    """facts.json stores delivery time in English ('3-5 business days').
    Render the Ukrainian tail without touching the offline artifact."""
    if not raw:
        return "—"
    return raw.replace("business days", "робочих днів").replace("days", "днів")


def handle_shipping(query: NormalizedQuery, facts: dict[str, Any]) -> Answer:
    std = _fact(facts, "standard_shipping_cost")
    std_days = _uk_days(_fact(facts, "standard_delivery_days"))
    exp = _fact(facts, "express_shipping_cost")
    exp_days = _uk_days(_fact(facts, "express_delivery_days"))
    threshold = _fact(facts, "free_shipping_threshold")
    outside_eu = _fact(facts, "ships_outside_eu")

    tokens = query.tokens

    # Non-EU shipping question. The store ships EU-only, so any non-EU
    # destination gets the same honest "no". Roots cover the destinations a
    # Ukrainian-speaking customer is most likely to ask about, plus the
    # generic "за кордон / за межі / поза ЄС" phrasings.
    non_eu_signal = any(
        t.startswith(root)
        for t in tokens
        for root in ("брита", "кордо", "закор", "поза", "меж",
                     "украї", "сша", "штат", "амери", "канад",
                     "швейц", "норве", "англі")
    )
    if non_eu_signal and outside_eu is False:
        return Answer(
            text="На жаль, ми не доставляємо за межі Європейського Союзу.",
            branch="policy_shipping_non_eu",
            confident=True,
        )

    # Express-specific.
    if any(t.startswith("експр") for t in tokens):
        if exp is None:
            return _missing_fact()
        return Answer(
            text=f"Експрес-доставка: {exp_days}, вартість {exp} EUR.",
            branch="policy_shipping_express",
            confident=True,
        )

    # Free-shipping threshold.
    if any(t.startswith("безко") for t in tokens):
        if threshold is None:
            return _missing_fact()
        return Answer(
            text=f"Доставка безкоштовна для замовлень від {threshold} EUR.",
            branch="policy_shipping_free",
            confident=True,
        )

    # General shipping.
    if std is None or exp is None or threshold is None:
        return _missing_fact()
    text = (
        f"Стандартна доставка по ЄС: {std_days}, вартість {std} EUR "
        f"(безкоштовно від {threshold} EUR). "
        f"Експрес: {exp_days}, {exp} EUR."
    )
    return Answer(text=text, branch="policy_shipping", confident=True)


def handle_payment(facts: dict[str, Any]) -> Answer:
    cod = _fact(facts, "cash_on_delivery")
    text = (
        "Приймаємо Visa, Mastercard, Apple Pay, Google Pay і PayPal. "
        "Кошти списуються при відправленні замовлення, а не при оформленні."
    )
    if cod is False:
        text += " Накладеним платежем не працюємо."
    return Answer(text=text, branch="policy_payment", confident=True)


def handle_damaged(facts: dict[str, Any]) -> Answer:
    hours = _fact(facts, "damage_report_hours")
    if hours is None:
        return _missing_fact()
    text = (
        f"Повідомте про пошкодження чи дефект протягом {hours} годин після "
        f"отримання, додавши фото. Ми оплачуємо зворотну пересилку і "
        f"надсилаємо заміну, або повертаємо повну вартість, якщо товару "
        f"немає в наявності."
    )
    return Answer(text=text, branch="policy_damaged", confident=True)


def handle(intent: str, query: NormalizedQuery, resolution: Resolution,
           store: Store, facts: dict[str, Any]) -> Answer:
    if intent == "POLICY_RETURNS":
        return handle_returns(facts)
    if intent == "POLICY_EXCHANGE":
        return handle_exchange(facts)
    if intent == "POLICY_SHIPPING":
        return handle_shipping(query, facts)
    if intent == "POLICY_PAYMENT":
        return handle_payment(facts)
    if intent == "POLICY_DAMAGED":
        return handle_damaged(facts)
    # Should not happen — router only sends these five here.
    return Answer(
        text="Уточніть, будь ласка, ваше питання.",
        branch="policy_unknown",
        confident=False,
    )