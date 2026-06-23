"""
股票池/ARKK 成分变化监控。

默认只记录变化，不自动覆盖交易股票池。Paper 验证期保持 universe 稳定更重要；
如果确认要跟随 ARK 每日持仓变化，可设置 AUTO_REFRESH_UNIVERSE=true。
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv


LIVE_DIR = Path(__file__).parent
RESEARCH_DIR = LIVE_DIR.parent / "research"
DATA_DIR = LIVE_DIR.parent / "data"
UNIVERSE_PATH = DATA_DIR / "universe.json"
AUDIT_DIR = LIVE_DIR / "audit"
CHANGES_PATH = AUDIT_DIR / "universe_changes.jsonl"

sys.path.insert(0, str(RESEARCH_DIR))
from build_universe import build_universe as build_universe_dict, save_universe as save_universe_dict

load_dotenv(LIVE_DIR / ".env")


def normalize_symbols(symbols: list[str]) -> list[str]:
    return sorted({str(sym).upper().strip() for sym in symbols if str(sym).strip()})


def symbol_hash(symbols: list[str]) -> str:
    normalized = normalize_symbols(symbols)
    payload = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def compare_symbol_lists(previous: list[str], current: list[str]) -> dict:
    prev = normalize_symbols(previous)
    curr = normalize_symbols(current)
    prev_set = set(prev)
    curr_set = set(curr)
    added = sorted(curr_set - prev_set)
    removed = sorted(prev_set - curr_set)
    return {
        "changed": bool(added or removed),
        "added": added,
        "removed": removed,
        "previous_count": len(prev),
        "current_count": len(curr),
        "previous_hash": symbol_hash(prev),
        "current_hash": symbol_hash(curr),
    }


def append_change_snapshot(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _load_existing_universe(path: Path) -> dict:
    if not path.exists():
        return {"symbols": [], "benchmarks": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _infer_source_status(universe: dict) -> dict:
    return universe.get("source_status") or {"ARKK_ARKW": universe.get("arkk_source", "unknown")}


def monitor_universe(
    universe_path: Path = UNIVERSE_PATH,
    changes_path: Path = CHANGES_PATH,
    build_universe_fn: Callable[[], dict] = build_universe_dict,
    auto_refresh: bool = False,
) -> dict:
    previous = _load_existing_universe(universe_path)
    current = build_universe_fn()
    diff = compare_symbol_lists(previous.get("symbols", []), current.get("symbols", []))
    record = {
        "run_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_status": _infer_source_status(current),
        "auto_refresh": bool(auto_refresh),
        **diff,
    }
    append_change_snapshot(changes_path, record)

    if auto_refresh:
        universe_path.parent.mkdir(parents=True, exist_ok=True)
        universe_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="检查交易股票池/ARKK 成分变化")
    parser.add_argument("--auto-refresh", action="store_true", help="检测后覆盖 data/universe.json")
    args = parser.parse_args()

    auto_refresh = args.auto_refresh or os.getenv("AUTO_REFRESH_UNIVERSE", "false").lower() == "true"
    result = monitor_universe(auto_refresh=auto_refresh)
    if result["changed"]:
        print(
            "股票池发生变化: "
            f"+{len(result['added'])} -{len(result['removed'])} "
            f"{result['previous_hash']} -> {result['current_hash']}"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"股票池无变化: {result['current_count']} 只 hash={result['current_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
