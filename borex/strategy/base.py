from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from borex.models.candle import Candle, Signal

if TYPE_CHECKING:
    from borex.data.mtf import MultiTimeframeContext


class Strategy(ABC):
    """Interfaz base para estrategias. Implementa `on_bar` para generar señales."""

    name: str = "strategy"

    @abstractmethod
    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        """Evalúa la vela en `index` y devuelve una señal o None."""
        ...
