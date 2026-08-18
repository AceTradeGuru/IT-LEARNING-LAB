"""Strategy registry."""

from __future__ import annotations

from typing import Dict, Type

from ..config import Instrument, StrategyParams
from .base import IndicatorCache, Strategy
from .momentum import MomentumBreakout
from .scalp import ScalpPullback

REGISTRY: Dict[str, Type[Strategy]] = {
    MomentumBreakout.name: MomentumBreakout,
    ScalpPullback.name: ScalpPullback,
}


def make_strategy(name: str, params: StrategyParams, instrument: Instrument) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[name](params, instrument)


__all__ = ["Strategy", "IndicatorCache", "MomentumBreakout", "ScalpPullback", "REGISTRY", "make_strategy"]
