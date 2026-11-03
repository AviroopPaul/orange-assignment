"""Estimated model cost, from Google's Standard tier prices checked on 2 Nov 2026."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_million_usd: float
    output_per_million_usd: float


PRICES: dict[str, Price] = {
    "gemini-3.5-flash-lite": Price(0.30, 2.50),
    "gemini-2.5-flash": Price(0.30, 2.50),
    "gemini-3-flash-preview": Price(0.50, 3.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Output includes thinking tokens, as Google bills them. None means no known price."""
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        input_tokens * price.input_per_million_usd + output_tokens * price.output_per_million_usd
    ) / 1_000_000
