from pathlib import Path

p = Path(r"C:\Users\azeva\OneDrive\Documentos\work\trading\borex-main\scripts\_patch_live_state_catchup.py")
t = p.read_text(encoding="utf-8")
t = t.replace(
    """def must_replace(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"MISSING anchor: {label}")
    return text.replace(old, new, 1)
""",
    """def must_replace(text: str, old: str, new: str, label: str, *, optional: bool = False) -> str:
    if old not in text:
        if optional:
            print("skip", label)
            return text
        raise SystemExit(f"MISSING anchor: {label}")
    print("apply", label)
    return text.replace(old, new, 1)
""",
)
t = t.replace('"router.dataclass",\n    )', '"router.dataclass", optional=True,\n    )')
t = t.replace('"router.force_close",\n    )', '"router.force_close", optional=True,\n    )')
p.write_text(t, encoding="utf-8")
print("ok")
