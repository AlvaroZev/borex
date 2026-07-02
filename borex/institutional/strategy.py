from __future__ import annotations

from dataclasses import dataclass, field

from borex.alexg.swings import detect_swings
from borex.data.mtf import MultiTimeframeContext
from borex.institutional.fvg import recent_fvg_fill
from borex.institutional.liquidity import detect_liquidity_sweep
from borex.institutional.risk import (
    atr,
    atr_stop_loss,
    atr_take_profit,
    passes_rr_filter,
)
from borex.institutional.scoring import compute_score
from borex.institutional.session import session_at
from borex.institutional.structure import (
    StructureBias,
    detect_break_of_structure,
    detect_structure_bias,
)
from borex.institutional.volume import relative_activity
from borex.institutional.vwap import vwap_state
from borex.models.candle import Candle, Signal, SignalAction
from borex.strategy.base import Strategy


@dataclass
class InstitutionalFlowStrategy(Strategy):
    """
    Institutional Flow — modelo usado por desks institucionales en FX:

    1. Liquidity sweep (stop hunt) en swing levels
    2. VWAP como benchmark de ejecución (acumulación/distribución)
    3. Fair Value Gap (imbalance) como zona de entrada
    4. Market structure (BOS + HH/HL o LL/LH)
    5. Volume/activity spike en la reversión
    6. Session filter (London / NY / overlap)
    7. MTF confluence (opcional)
    """

    name: str = "institutional_flow"
    min_score: float = 65.0
    min_rr: float = 2.0
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    vwap_period: int = 20
    swing_lookback: int = 5
    sweep_lookback: int = 30
    min_bars: int = 60
    require_sweep_or_fvg: bool = True

    _last_signal_index: int = field(default=-999, repr=False)

    def on_bar(
        self,
        index: int,
        candles: list[Candle],
        mtf: MultiTimeframeContext | None = None,
    ) -> Signal | None:
        if index < self.min_bars:
            return None

        window = candles[: index + 1]
        swings = detect_swings(window, self.swing_lookback)
        if len(swings) < 4:
            return None

        bias = detect_structure_bias(swings, index)
        if bias == StructureBias.NEUTRAL:
            return None

        sweep = detect_liquidity_sweep(
            window, index, swings, lookback=self.sweep_lookback
        )
        vwap = vwap_state(window, index, self.vwap_period)
        bos = detect_break_of_structure(window, index, swings, bias)
        vol_ok = relative_activity(window, index)
        sess = session_at(window[index])

        if bias == StructureBias.BULLISH:
            action = SignalAction.BUY
            fvg = recent_fvg_fill(window, index, "bullish")
            struct_level = sweep.swept_level if sweep and sweep.direction == "bullish" else None
        else:
            action = SignalAction.SELL
            fvg = recent_fvg_fill(window, index, "bearish")
            struct_level = sweep.swept_level if sweep and sweep.direction == "bearish" else None

        if self.require_sweep_or_fvg and sweep is None and fvg is None:
            return None

        score = compute_score(
            action, sweep, vwap, bias, bos, fvg, vol_ok, sess, mtf, index
        )

        if mtf is not None and score.mtf < 8.0:
            return None

        if score.total < self.min_score:
            return None

        entry = candles[index].close
        atr_val = atr(window, index, self.atr_period)
        if atr_val <= 0:
            return None

        sl = atr_stop_loss(entry, action, atr_val, self.atr_sl_mult, struct_level)
        vwap_target = vwap.vwap if vwap is not None else None
        tp = atr_take_profit(entry, sl, action, self.min_rr, vwap_target)

        if not passes_rr_filter(entry, sl, tp, self.min_rr):
            return None

        if index - self._last_signal_index < 8:
            return None
        self._last_signal_index = index

        tags = ["inst", score.grade, bias.value]
        if sweep:
            tags.append("sweep")
        if fvg:
            tags.append("fvg")
        if bos:
            tags.append("bos")
        tags.append(sess.value)

        return Signal(
            action=action,
            pattern="|".join(tags),
            index=index,
            price=entry,
            timestamp=candles[index].timestamp,
            stop_loss=sl,
            take_profit=tp,
            score=score.total,
        )
