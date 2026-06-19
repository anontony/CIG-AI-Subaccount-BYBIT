import os
from pathlib import Path
from typing import Iterable, List

from skill_sync import ensure_seeded, read_status


def _read(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return text[:limit]


def _pick_modules(text: str, mode: str) -> List[str]:
    t = (text or "").lower()
    modules = ["market", "account"]
    if mode == "strategy_loop":
        modules += ["spot", "derivatives", "strategy"]
    else:
        if any(k in t for k in ["spot", "giao ngay", "mua coin", "bán hết", "sell all"]):
            modules.append("spot")
        if any(k in t for k in ["future", "futures", "linear", "long", "short", "đòn", "leverage", "x"]):
            modules.append("derivatives")
        if any(k in t for k in ["bot", "dca", "grid", "twap", "iceberg"]):
            modules += ["trading-bot", "strategy"]
    # Keep order and uniqueness.
    out: List[str] = []
    for m in modules:
        if m not in out:
            out.append(m)
    return out


def build_skill_context(*, mode: str, command_or_prompt: str = "", max_chars: int | None = None) -> str:
    """Return a compact local/auto-updated Bybit Skill context for the AI system prompt."""
    if max_chars is None:
        try:
            max_chars = int(os.getenv("BYBIT_SKILL_CONTEXT_MAX_CHARS", "1800"))
        except Exception:
            max_chars = 1800
    skill_dir = ensure_seeded()
    status = read_status()
    chunks = [
        f"CIG AI Subaccount uses Bybit Skill local cache version: {status.get('local_version')}.",
        "Follow Safety > User Responsiveness > Convenience. Use Read+Trade API only; never Withdraw.",
        "Always keep CIG AI Subaccount Risk Guard as the final authority before execution.",
    ]
    main = _read(skill_dir / "SKILL.md", 700)
    if main:
        chunks.append("\n[SKILL.md excerpt]\n" + main)
    for module in _pick_modules(command_or_prompt, mode):
        path = skill_dir / "modules" / f"{module}.md"
        text = _read(path, 450)
        if text:
            chunks.append(f"\n[module:{module}.md excerpt]\n{text}")
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[skill context truncated]"
    return text
