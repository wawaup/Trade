"""
本地/云端 Paper 复盘对比工具。

默认从云端 review_api 拉取只读 JSON，再和本地计算/快照结果比较。
测试期可先用 --local-payload 文件校准报告格式。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from dotenv import load_dotenv


LIVE_DIR = Path(__file__).parent
sys.path.insert(0, str(LIVE_DIR))
load_dotenv(LIVE_DIR / ".env")


def fetch_remote_payload(url: str, token: str) -> dict:
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _check_equal(name: str, remote_value, local_value) -> dict:
    return {
        "status": "match" if remote_value == local_value else "mismatch",
        "remote": remote_value,
        "local": local_value,
    }


def compare_payloads(remote: dict, local: dict) -> dict:
    checks = {
        "target_symbols": _check_equal(
            "target_symbols",
            remote.get("target_symbols", []),
            local.get("target_symbols", []),
        ),
        "universe_hash": _check_equal(
            "universe_hash",
            remote.get("universe", {}).get("hash", ""),
            local.get("universe", {}).get("hash", ""),
        ),
        "universe_count": _check_equal(
            "universe_count",
            remote.get("universe", {}).get("count", ""),
            local.get("universe", {}).get("count", ""),
        ),
    }
    ok = all(row["status"] == "match" for row in checks.values())
    return {
        "ok": ok,
        "remote_run_id": remote.get("latest_run", {}).get("run_id", ""),
        "remote_status": remote.get("latest_run", {}).get("status", ""),
        "checks": checks,
    }


def build_local_payload(root: Path = LIVE_DIR) -> dict:
    from review_api import build_latest_review_payload

    return build_latest_review_payload(root)


def format_markdown_report(result: dict) -> str:
    lines = [
        "# Trade Quant 每日校准报告",
        "",
        f"- 云端 run_id: {result.get('remote_run_id', '')}",
        f"- 云端状态: {result.get('remote_status', '')}",
        f"- 对比结论: {'通过' if result.get('ok') else '存在差异'}",
        "",
        "## 检查项",
    ]
    for name, row in result.get("checks", {}).items():
        lines.append(f"- {name}: {row.get('status')}")
        lines.append(f"  - remote: {row.get('remote')}")
        lines.append(f"  - local: {row.get('local')}")
    return "\n".join(lines)


def _load_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="对比云端 Paper 运行结果与本地复盘结果")
    parser.add_argument("--url", default=os.getenv("REVIEW_API_URL", "http://127.0.0.1:8765/review/latest"))
    parser.add_argument("--token", default=os.getenv("REVIEW_API_TOKEN", ""))
    parser.add_argument("--local-payload", type=Path, help="本地复盘 JSON 文件")
    parser.add_argument("--remote-payload", type=Path, help="跳过 HTTP，直接读取云端 payload JSON 文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    args = parser.parse_args()

    if args.remote_payload:
        remote = _load_json_file(args.remote_payload)
    else:
        if not args.token:
            raise SystemExit("缺少 REVIEW_API_TOKEN，无法拉取云端复盘接口")
        remote = fetch_remote_payload(args.url, args.token)

    if args.local_payload:
        local = _load_json_file(args.local_payload)
    else:
        local = build_local_payload()

    result = compare_payloads(remote, local)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_markdown_report(result))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
