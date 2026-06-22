"""
服务健康检查脚本

用于独立监控 alpaca_trader.py 是否按计划运行。建议在 GCP VM 上通过
另一个 cron 在主交易脚本之后执行；如果最近一次 paper_runs.csv 记录过旧，
则发送“紧急报警-服务失效”邮件。
"""

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


LIVE_DIR = Path(__file__).parent
sys.path.insert(0, str(LIVE_DIR))

from alpaca_trader import AUDIT_DIR, PAPER, _audit_attachments, build_email_subject, send_email


@dataclass
class HealthResult:
    ok: bool
    status: str
    message: str
    last_run_id: str = ""
    last_run_at_utc: str = ""


def _parse_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip().replace("Z", "")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _last_run_row(runs_path: Path) -> Optional[dict]:
    if not runs_path.exists():
        return None
    with runs_path.open(newline="", encoding="utf-8") as f:
        rows = [row for row in csv.DictReader(f) if row.get("run_at_utc")]
    if not rows:
        return None
    rows.sort(key=lambda row: row.get("run_at_utc", ""))
    return rows[-1]


def check_service_health(runs_path: Path, now_utc: datetime, max_age_hours: float) -> HealthResult:
    row = _last_run_row(runs_path)
    if not row:
        return HealthResult(
            ok=False,
            status="service_stale",
            message=f"未找到运行审计记录: {runs_path}",
        )

    last_run_at = _parse_utc(row.get("run_at_utc", ""))
    if not last_run_at:
        return HealthResult(
            ok=False,
            status="service_stale",
            message=f"最近运行时间无法解析: {row.get('run_at_utc', '')}",
            last_run_id=row.get("run_id", ""),
            last_run_at_utc=row.get("run_at_utc", ""),
        )

    age_hours = (now_utc - last_run_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        return HealthResult(
            ok=False,
            status="service_stale",
            message=f"最近一次运行已过去 {age_hours:.1f}h，超过阈值 {max_age_hours:.1f}h",
            last_run_id=row.get("run_id", ""),
            last_run_at_utc=row.get("run_at_utc", ""),
        )

    return HealthResult(
        ok=True,
        status="ok",
        message=f"最近一次运行距今 {age_hours:.1f}h，服务正常",
        last_run_id=row.get("run_id", ""),
        last_run_at_utc=row.get("run_at_utc", ""),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Alpaca Trader 是否按计划运行")
    default_max_age_hours = float(os.getenv("WATCHDOG_MAX_AGE_HOURS", "24"))
    parser.add_argument("--max-age-hours", type=float, default=default_max_age_hours,
                        help="最近一次运行超过该小时数则报警")
    parser.add_argument("--runs-path", type=Path, default=AUDIT_DIR / "paper_runs.csv",
                        help="paper_runs.csv 路径")
    args = parser.parse_args()

    now = datetime.utcnow()
    result = check_service_health(args.runs_path, now, args.max_age_hours)
    if result.ok:
        print(result.message)
        return 0

    run_id = now.strftime("watchdog-%Y%m%dT%H%M%SZ")
    body = "\n".join([
        f"run_id: {run_id}",
        f"status: {result.status}",
        f"message: {result.message}",
        f"last_run_id: {result.last_run_id}",
        f"last_run_at_utc: {result.last_run_at_utc}",
        f"runs_path: {args.runs_path}",
    ])
    send_email(build_email_subject(result.status, run_id, PAPER), body, _audit_attachments())
    print(result.message)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
