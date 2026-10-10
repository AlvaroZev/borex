from pathlib import Path

p = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live\borex_live\service.py")
t = p.read_text(encoding="utf-8")
old = """        self._runtime[\"state_catchup\"] = {
            \"days\": days,
            \"bars\": n,
            \"start\": str(master[start_i].timestamp),
            \"end\": str(master[-1].timestamp),
            \"signals\": opens,
            \"exits\": closes,
            \"cash\": snap.get(\"cash\"),
            \"open_trades\": len(snap.get(\"open_trades\") or []),
            \"closed_trades\": len(snap.get(\"closed_trades\") or []),
        }
        logger.info(
            \"State catch-up done | bars=%d signals=%d exits=%d cash=%.2f open=%d closed=%d\",
            n,
            opens,
            closes,
            float(snap.get(\"cash\") or 0.0),
            len(snap.get(\"open_trades\") or []),
            len(snap.get(\"closed_trades\") or []),
        )
"""
# Insert cash rebase before state_catchup runtime dict — do it inside the session before commit
anchor = """            session.commit()
            snap = repo.dashboard_snapshot()

        self._runtime[\"state_catchup\"] = {
"""
insert = """            # Re-base DB free cash to MT5 after pretend epoch so live math stays coherent.
            # Trade history stays; only the cash ledger is aligned to broker available funds.
            mt5_bal = 0.0
            try:
                if self.mt5.connected and not self.mt5.dry_run:
                    mt5_bal = float(self.mt5.account_balance() or 0.0)
            except Exception:
                mt5_bal = 0.0
            open_margin = sum(float(t.margin or 0.0) for t in repo.open_trades())
            if mt5_bal > 0:
                new_cash = max(0.0, mt5_bal - open_margin)
                old_cash = float(repo.get_portfolio(self.cfg.capital).cash)
                repo.set_cash(new_cash)
                logger.info(
                    "Catch-up cash rebase: DB %.2f -> %.2f (mt5_bal=%.2f open_margin=%.2f)",
                    old_cash,
                    new_cash,
                    mt5_bal,
                    open_margin,
                )
            session.commit()
            snap = repo.dashboard_snapshot()

        self._runtime[\"state_catchup\"] = {
"""
if anchor not in t:
    raise SystemExit("anchor missing")
# Only replace the catchup-specific commit/snap (last occurrence before state_catchup runtime)
idx = t.rfind(anchor)
if idx < 0:
    raise SystemExit("anchor rfind failed")
t = t[:idx] + insert + t[idx + len(anchor) :]
p.write_text(t, encoding="utf-8")
print("cash rebase inserted")
