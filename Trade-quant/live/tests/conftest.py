"""
测试全局隔离：确保 pytest 永远不会写到生产 live/*.json / trader.log / audit/。

背景（2026-07-11 审计致命-3）：
    alpaca_trader.py 顶层 logging.basicConfig(FileHandler(LOG_FILE)) 和 _save_state()
    都直接指向 live/ 目录下的真实文件。test_execution_safety.py 通过
    importlib.util.spec_from_file_location + exec_module 反复加载真实模块，
    部分用例以 dry_run=False 调 retry_halted_orders → 内部 _save_state(state) →
    把测试 fixture 整体写进真实 live/state.json。已实际造成生产 state.json 损坏。

修法：
    alpaca_trader.py 已把 STATE_FILE / LOG_FILE / AUDIT_DIR / HALT_PENDING_FILE /
    STATE_BACKUP_FILE / LOCK_FILE 六个路径改为从环境变量读取（默认值仍是原路径），
    此 conftest 在 collection 阶段（早于任何 test 模块 import alpaca_trader）
    把这些环境变量指向 pytest 会话级 tmp 目录。

作用范围：
    位于 live/tests/ 下，pytest 会自动加载给该目录及子目录下的所有测试。
    生产运行 alpaca_trader 时没有 pytest，env 不设置 → 使用默认真实路径。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# ── 1. 会话级 tmp 目录：所有测试共用一个，pytest 退出后清理 ─────────────────
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="trade-quant-tests-"))
(_SESSION_TMP / "audit").mkdir(exist_ok=True)

# ── 2. 覆盖 alpaca_trader 六个可外部化路径 —— 必须在任何 test 模块 import 前执行
os.environ["TRADE_QUANT_STATE_FILE"]         = str(_SESSION_TMP / "state.json")
os.environ["TRADE_QUANT_STATE_BACKUP_FILE"]  = str(_SESSION_TMP / "state.json.bak")
os.environ["TRADE_QUANT_LOG_FILE"]           = str(_SESSION_TMP / "trader.log")
os.environ["TRADE_QUANT_AUDIT_DIR"]          = str(_SESSION_TMP / "audit")
os.environ["TRADE_QUANT_HALT_PENDING_FILE"]  = str(_SESSION_TMP / "halt_pending.json")
os.environ["TRADE_QUANT_LOCK_FILE"]          = str(_SESSION_TMP / ".trader.lock")

# ── 3. 兜底：如果 alpaca_trader 已经被别的 test 抢先 import 并把常量绑定成生产路径，
#         这里把已加载模块里的六个常量再改一遍。防御性双保险，不依赖 import 顺序。
def _patch_already_loaded_module() -> None:
    for mod_name in list(sys.modules):
        if "alpaca_trader" not in mod_name:
            continue
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, "STATE_FILE"):
            continue
        mod.STATE_FILE         = Path(os.environ["TRADE_QUANT_STATE_FILE"])
        mod.STATE_BACKUP_FILE  = Path(os.environ["TRADE_QUANT_STATE_BACKUP_FILE"])
        mod.LOG_FILE           = Path(os.environ["TRADE_QUANT_LOG_FILE"])
        mod.AUDIT_DIR          = Path(os.environ["TRADE_QUANT_AUDIT_DIR"])
        mod.HALT_PENDING_FILE  = Path(os.environ["TRADE_QUANT_HALT_PENDING_FILE"])
        mod.LOCK_FILE          = Path(os.environ["TRADE_QUANT_LOCK_FILE"])


_patch_already_loaded_module()


# ── 4. 生产文件哈希守卫：会话开始与结束比对 live/state.json，若被写过则测试失败 ──
_LIVE_DIR = Path(__file__).resolve().parents[1]
_GUARDED_FILES = [
    _LIVE_DIR / "state.json",
    _LIVE_DIR / "state.json.bak",
    _LIVE_DIR / "halt_pending.json",
]


def _snapshot(paths: list[Path]) -> dict[str, tuple[int, bytes]]:
    snap: dict[str, tuple[int, bytes]] = {}
    for p in paths:
        if p.exists():
            data = p.read_bytes()
            snap[str(p)] = (len(data), data)
    return snap


_PRODUCTION_SNAPSHOT = _snapshot(_GUARDED_FILES)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """会话结束时校验生产文件未被测试改动；有变化则以硬错误告知开发者。"""
    for path_str, (before_len, before_bytes) in _PRODUCTION_SNAPSHOT.items():
        p = Path(path_str)
        if not p.exists():
            print(
                f"\n[conftest] ❌ 生产文件在测试中被删除：{p}",
                file=sys.stderr,
            )
            session.exitstatus = max(session.exitstatus, 1)
            continue
        after = p.read_bytes()
        if after != before_bytes:
            print(
                f"\n[conftest] ❌ 测试污染了生产文件：{p}\n"
                f"           修复前 {before_len} 字节 → 现在 {len(after)} 字节\n"
                f"           请从 state.json.bak 恢复，并检查新增测试是否绕过了路径隔离。",
                file=sys.stderr,
            )
            session.exitstatus = max(session.exitstatus, 1)

    # 清理 tmp（保留失败会话的 tmp 目录以便排查）
    if session.exitstatus == 0:
        shutil.rmtree(_SESSION_TMP, ignore_errors=True)
    else:
        print(f"\n[conftest] 保留测试 tmp 目录以便排查：{_SESSION_TMP}", file=sys.stderr)
