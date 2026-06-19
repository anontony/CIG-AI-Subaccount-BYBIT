import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", "./data"))
DEFAULT_SKILL_DIR = RUNTIME_DIR / "bybit_skill"
SEED_SKILL_DIR = PROJECT_ROOT / "bybit_skill_seed"
MANIFEST_URL = os.getenv("BYBIT_SKILL_MANIFEST_URL", "https://api.bybit.com/skill/manifest")
RAW_BASE_URL = os.getenv("BYBIT_SKILL_RAW_BASE_URL", "https://raw.githubusercontent.com/bybit-exchange/skills/main")
USER_AGENT = os.getenv("BYBIT_SKILL_USER_AGENT", "bybit-skill/1.4.3 cig-ai-subaccount")
STATUS_FILE = "skill_status.json"


class SkillSyncError(RuntimeError):
    pass


def _now() -> int:
    return int(time.time())


def _skill_dir() -> Path:
    return Path(os.getenv("BYBIT_SKILL_DIR", str(DEFAULT_SKILL_DIR))).resolve()


def _status_path(skill_dir: Path) -> Path:
    return skill_dir / STATUS_FILE


def _safe_read(path: Path, limit: int = 120_000) -> str:
    try:
        data = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if len(data) > limit:
        return data[:limit]
    return data


def _parse_version(text: str) -> str:
    # Supports the current Bybit format and normal YAML-like version fields.
    patterns = [
        r"metadata\s+version[\s\S]{0,80}?\n\s*([0-9]+\.[0-9]+\.[0-9]+)",
        r"version\s*[:=]\s*['\"]?([0-9]+\.[0-9]+\.[0-9]+)",
        r"\b([0-9]+\.[0-9]+\.[0-9]+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return "0.0.0"


def _semver_tuple(v: str) -> tuple[int, int, int]:
    parts = (v or "0.0.0").split(".")[:3]
    nums = []
    for p in parts:
        try:
            nums.append(int(re.sub(r"\D.*$", "", p) or "0"))
        except Exception:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


def _validate_manifest_path(rel: str) -> None:
    rel = str(rel or "")
    if rel == "SKILL.md":
        return
    if ".." in rel or rel.startswith(("/", "~")) or "\\" in rel or not rel.endswith(".md"):
        raise SkillSyncError(f"invalid_path:{rel}")
    if re.fullmatch(r"modules/[a-z0-9-]+\.md", rel):
        return
    raise SkillSyncError(f"invalid_path:{rel}")


def _validate_manifest(manifest: Dict[str, Any]) -> Dict[str, str]:
    files = manifest.get("files") or {}
    if not isinstance(files, dict) or not files:
        raise SkillSyncError("manifest_files_empty")
    clean: Dict[str, str] = {}
    for path, checksum in files.items():
        _validate_manifest_path(str(path))
        checksum = str(checksum or "")
        if not checksum.startswith("sha256:"):
            raise SkillSyncError(f"unsupported_checksum:{path}")
        clean[str(path)] = checksum.split(":", 1)[1].lower().strip()
    return clean


def ensure_seeded(skill_dir: Optional[Path] = None) -> Path:
    skill_dir = skill_dir or _skill_dir()
    skill_dir.mkdir(parents=True, exist_ok=True)
    if not (skill_dir / "SKILL.md").exists():
        if SEED_SKILL_DIR.exists():
            for src in SEED_SKILL_DIR.rglob("*.md"):
                rel = src.relative_to(SEED_SKILL_DIR)
                dst = skill_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        else:
            (skill_dir / "modules").mkdir(exist_ok=True)
            (skill_dir / "SKILL.md").write_text(FALLBACK_SKILL_MD, encoding="utf-8")
            (skill_dir / "modules" / "market.md").write_text(FALLBACK_MARKET_MD, encoding="utf-8")
            (skill_dir / "modules" / "spot.md").write_text(FALLBACK_SPOT_MD, encoding="utf-8")
            (skill_dir / "modules" / "derivatives.md").write_text(FALLBACK_DERIVATIVES_MD, encoding="utf-8")
            (skill_dir / "modules" / "account.md").write_text(FALLBACK_ACCOUNT_MD, encoding="utf-8")
    return skill_dir


def read_status() -> Dict[str, Any]:
    skill_dir = ensure_seeded()
    skill_md = _safe_read(skill_dir / "SKILL.md")
    version = _parse_version(skill_md)
    status: Dict[str, Any] = {}
    try:
        status = json.loads(_status_path(skill_dir).read_text(encoding="utf-8"))
    except Exception:
        status = {}
    modules = sorted(str(p.relative_to(skill_dir)) for p in (skill_dir / "modules").glob("*.md")) if (skill_dir / "modules").exists() else []
    return {
        "ok": True,
        "project": "CIG AI Subaccount",
        "skill_dir": str(skill_dir),
        "local_version": version,
        "modules": modules,
        "last_result": status.get("last_result"),
        "last_checked_at": status.get("last_checked_at"),
        "manifest_url": MANIFEST_URL,
        "raw_base_url": RAW_BASE_URL,
    }


def _write_status(skill_dir: Path, result: Dict[str, Any]) -> None:
    payload = {"last_checked_at": _now(), "last_result": result}
    _status_path(skill_dir).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def check_and_update_skill(*, force: bool = False) -> Dict[str, Any]:
    """Fetch Bybit manifest, verify sha256, then atomically update the local skill cache.

    Fails closed: invalid paths/checksums abort the whole update. Network failures are reported
    as non-fatal so the bot can continue using the current local version.
    """
    skill_dir = ensure_seeded()
    skill_md = _safe_read(skill_dir / "SKILL.md")
    local_version = _parse_version(skill_md)
    headers = {"User-Agent": USER_AGENT}
    result: Dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=18.0, headers=headers, follow_redirects=True) as client:
            manifest_resp = await client.get(MANIFEST_URL)
            manifest_resp.raise_for_status()
            manifest = manifest_resp.json()
            remote_version = str(manifest.get("version") or "0.0.0")
            file_hashes = _validate_manifest(manifest)

            if not force and _semver_tuple(remote_version) <= _semver_tuple(local_version):
                result = {"status": "current", "local_version": local_version, "remote_version": remote_version}
                _write_status(skill_dir, result)
                return result

            tmp = skill_dir / ".skill-update-tmp"
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True, exist_ok=True)

            verified_files = []
            for rel, expected_hash in file_hashes.items():
                url = f"{RAW_BASE_URL.rstrip('/')}/{rel}"
                r = await client.get(url)
                r.raise_for_status()
                content = r.content
                actual = hashlib.sha256(content).hexdigest().lower()
                if actual != expected_hash:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise SkillSyncError(f"checksum_mismatch:{rel}")
                dst = tmp / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(content)
                verified_files.append(rel)

            for rel in verified_files:
                dst = skill_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp / rel), str(dst))
            shutil.rmtree(tmp, ignore_errors=True)

            result = {
                "status": "updated" if _semver_tuple(remote_version) > _semver_tuple(local_version) else "refreshed",
                "from": local_version,
                "to": remote_version,
                "files": verified_files,
            }
            _write_status(skill_dir, result)
            return result
    except Exception as exc:
        result = {"status": "error", "local_version": local_version, "reason": f"{type(exc).__name__}: {exc}"}
        try:
            _write_status(skill_dir, result)
        except Exception:
            pass
        return result


async def background_update_once() -> None:
    # Run in background at app startup. Never raise; never block bot startup.
    try:
        await check_and_update_skill(force=False)
    except Exception:
        pass


FALLBACK_SKILL_MD = """---
name: bybit-trading
version: 1.4.3
author: Bybit
updated: 2026-06-17
---
# Bybit Trading Skill - Local Fallback

Safety rules:
- Use a dedicated Bybit sub-account for AI trading.
- API key should only have Read + Trade. Never enable Withdraw.
- Verify time and wallet balance before trading.
- Use category=spot for spot orders and category=linear for USDT perpetual/futures.
- Confirm/live execution must pass local Risk Guard.
"""
FALLBACK_MARKET_MD = """# Market module fallback
Use ticker, kline, instruments info, orderbook and market time to build a market snapshot.
"""
FALLBACK_SPOT_MD = """# Spot module fallback
Spot buy/sell uses category=spot. Spot has no short and no leverage by default.
"""
FALLBACK_DERIVATIVES_MD = """# Derivatives module fallback
Linear futures uses category=linear. Long/short can use leverage and TP/SL.
"""
FALLBACK_ACCOUNT_MD = """# Account module fallback
Use UNIFIED wallet balance. Mask credentials in logs and UI.
"""
