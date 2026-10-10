from pathlib import Path

p = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex_live\borex_live\execution\router.py")
t = p.read_text(encoding="utf-8")
old = """        if self.dry_run or not self.execute_mt5:
            return
        result = self.mt5.close_position(symbol, ticket=ticket or None)
"""
new = """        if self.dry_run or not self.execute_mt5 or int(ticket or 0) <= 0:
            return
        result = self.mt5.close_position(symbol, ticket=ticket or None)
"""
if old not in t:
    raise SystemExit("anchor missing")
p.write_text(t.replace(old, new, 1), encoding="utf-8")
print("ok")
