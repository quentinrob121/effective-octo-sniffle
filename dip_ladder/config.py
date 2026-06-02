from __future__ import annotations

from dataclasses import dataclass

# Defaults match the screenshot reasoning: deeper dips = larger adds.
DEFAULT_LEVELS_PCT: tuple[float, ...] = (0.15, 0.25, 0.35, 0.50)
DEFAULT_WEIGHTS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0)
LADDER_CLIENT_ID_PREFIX = "ladder"


@dataclass(frozen=True)
class LadderConfig:
    """Dip-ladder geometry. Levels and weights must be the same length.

    Levels are *drops* expressed as positive fractions (0.15 = -15%).
    Weights are the relative sizing across levels (1, 2, 3, 5 -> deepest is
    5x the shallowest).
    """

    levels_pct: tuple[float, ...] = DEFAULT_LEVELS_PCT
    weights: tuple[float, ...] = DEFAULT_WEIGHTS
    # Hard cap: never let the sum of all ladder limit orders exceed this in $.
    # 30K accommodates the canonical -15/-25/-35/-50 with 10/20/30/50 shares
    # at a $360 reference (~$24K total). Tighten this for smaller accounts.
    max_total_usd: float = 30_000.0

    def __post_init__(self) -> None:
        if len(self.levels_pct) != len(self.weights):
            raise ValueError("levels_pct and weights must have the same length")
        if not self.levels_pct:
            raise ValueError("ladder must have at least one level")
        if any(p <= 0 or p >= 1 for p in self.levels_pct):
            raise ValueError("each level must be in (0, 1)")
        if any(w <= 0 for w in self.weights):
            raise ValueError("each weight must be > 0")


@dataclass(frozen=True)
class PlannedRung:
    level_pct: float
    limit_price: float
    qty: int
    weight: float


def plan_ladder(
    ref_price: float,
    base_qty: int | None = None,
    base_usd: float | None = None,
    config: LadderConfig | None = None,
) -> list[PlannedRung]:
    """Compute the rungs without placing any orders.

    Provide exactly one of ``base_qty`` (the shallowest level's share count;
    deeper levels scale by weight) or ``base_usd`` (the shallowest level's
    dollar budget at its limit price).
    """
    if (base_qty is None) == (base_usd is None):
        raise ValueError("Provide exactly one of base_qty or base_usd.")
    if ref_price <= 0:
        raise ValueError("ref_price must be positive")
    config = config or LadderConfig()

    rungs: list[PlannedRung] = []
    base_weight = config.weights[0]
    for level_pct, weight in zip(config.levels_pct, config.weights):
        limit_price = round(ref_price * (1 - level_pct), 2)
        if base_qty is not None:
            qty = max(1, int(round(base_qty * (weight / base_weight))))
        else:
            base_dollars = base_usd * (weight / base_weight)  # type: ignore[operator]
            qty = max(1, int(base_dollars // limit_price))
        rungs.append(
            PlannedRung(
                level_pct=level_pct,
                limit_price=limit_price,
                qty=qty,
                weight=weight,
            )
        )
    total = sum(r.limit_price * r.qty for r in rungs)
    if total > config.max_total_usd:
        raise ValueError(
            f"ladder total ${total:,.0f} exceeds max ${config.max_total_usd:,.0f}; "
            "shrink base size or raise max_total_usd"
        )
    return rungs


