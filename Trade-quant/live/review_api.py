"""
云端 Paper 复盘只读接口。

默认绑定 127.0.0.1，通过 SSH tunnel 访问。接口只读取 audit/state/log/universe，
不接触 Alpaca 下单 API，也不返回 .env 密钥。
"""

import argparse
import csv
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


LIVE_DIR = Path(__file__).parent
AUDIT_DIR = LIVE_DIR / "audit"
LOG_FILE = LIVE_DIR / "trader.log"
STATE_FILE = LIVE_DIR / "state.json"
UNIVERSE_PATH = LIVE_DIR.parent / "data" / "universe.json"

load_dotenv(LIVE_DIR / ".env")


def authorize(header_value: str, expected_token: str) -> bool:
    if not expected_token:
        return False
    return header_value == f"Bearer {expected_token}"


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _latest_run(rows: list[dict]) -> dict:
    if not rows:
        return {}
    return sorted(rows, key=lambda row: row.get("run_at_utc", ""))[-1]


def _rows_for_run(path: Path, run_id: str) -> list[dict]:
    rows = _read_csv(path)
    if not run_id:
        return rows[-50:]
    return [row for row in rows if row.get("run_id") == run_id]


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_log_tail(path: Path, max_lines: int = 120) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def _symbol_hash(symbols: list[str]) -> str:
    normalized = sorted({str(sym).upper().strip() for sym in symbols if str(sym).strip()})
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()[:16]


def _latest_jsonl(path: Path) -> dict:
    if not path.exists():
        return {}
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        return {}
    try:
        return json.loads(rows[-1])
    except json.JSONDecodeError:
        return {}


def _universe_summary(root: Path) -> dict:
    universe_path = root.parent / "data" / "universe.json"
    universe = _read_json(universe_path)
    symbols = universe.get("symbols", [])
    return {
        "path": str(universe_path),
        "built_date": universe.get("built_date", ""),
        "count": len(symbols),
        "benchmarks": universe.get("benchmarks", []),
        "hash": _symbol_hash(symbols),
        "layer_counts": universe.get("layer_counts", {}),
    }


def build_latest_review_payload(root: Path = LIVE_DIR) -> dict:
    audit_dir = root / "audit"
    runs = _read_csv(audit_dir / "paper_runs.csv")
    latest = _latest_run(runs)
    run_id = latest.get("run_id", "")
    signals = _rows_for_run(audit_dir / "signals.csv", run_id)
    orders = _rows_for_run(audit_dir / "orders.csv", run_id)
    fills = _rows_for_run(audit_dir / "fills.csv", run_id)
    ranked = sorted(
        [row for row in signals if row.get("symbol") and row.get("symbol") != "NO_CANDIDATE"],
        key=lambda row: int(row.get("rank") or 999999),
    )
    target_symbols = [
        sym.strip()
        for sym in latest.get("target_symbols", "").split(",")
        if sym.strip()
    ]
    if not target_symbols:
        target_symbols = [row["symbol"] for row in ranked[:5]]
    return {
        "latest_run": latest,
        "signals": signals,
        "target_symbols": target_symbols,
        "orders": orders,
        "fills": fills,
        "state": _read_json(root / "state.json"),
        "universe": _universe_summary(root),
        "universe_change": _latest_jsonl(audit_dir / "universe_changes.jsonl"),
        "log_tail": _read_log_tail(root / "trader.log"),
    }


def build_universe_payload(root: Path = LIVE_DIR) -> dict:
    return {
        "universe": _universe_summary(root),
        "latest_change": _latest_jsonl(root / "audit" / "universe_changes.jsonl"),
    }


def _json_response(status: int, payload: dict) -> tuple[int, dict, str]:
    return status, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(payload, ensure_ascii=False)


def route_request(path: str, auth_header: str, expected_token: str, root: Path = LIVE_DIR) -> tuple[int, dict, str]:
    clean_path = urlparse(path).path
    if clean_path == "/health":
        return _json_response(200, {"status": "ok"})
    if clean_path in {"/review/latest", "/review/universe"}:
        if not authorize(auth_header, expected_token):
            return _json_response(401, {"error": "unauthorized"})
        if clean_path == "/review/latest":
            return _json_response(200, build_latest_review_payload(root))
        return _json_response(200, build_universe_payload(root))
    return _json_response(404, {"error": "not_found"})


class ReviewHandler(BaseHTTPRequestHandler):
    token = ""
    root = LIVE_DIR

    def do_GET(self):
        status, headers, body = route_request(
            self.path,
            self.headers.get("Authorization", ""),
            self.token,
            self.root,
        )
        encoded = body.encode("utf-8")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Trade Quant 只读复盘 API")
    parser.add_argument("--host", default=os.getenv("REVIEW_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("REVIEW_API_PORT", "8765")))
    args = parser.parse_args()

    token = os.getenv("REVIEW_API_TOKEN", "")
    if not token:
        raise SystemExit("缺少 REVIEW_API_TOKEN，拒绝启动复盘 API")

    ReviewHandler.token = token
    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    print(f"review_api listening on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
