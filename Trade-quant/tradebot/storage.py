import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Optional


class BacktestResultStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def append(self, result: dict) -> dict:
        with self._lock:
            results = self._read_all()
            stored = dict(result)
            results.append(stored)
            self._write_all(results)
            return stored

    def list_recent(self, limit: int = 20) -> list[dict]:
        try:
            safe_limit = max(1, int(limit))
        except (TypeError, ValueError):
            safe_limit = 20
        with self._lock:
            return self._read_all()[-safe_limit:]

    def get(self, result_id: str) -> Optional[dict]:
        wanted = str(result_id or "")
        with self._lock:
            for result in self._read_all():
                if str(result.get("resultId") or "") == wanted:
                    return result
        return None

    def clear(self) -> None:
        with self._lock:
            self._write_all([])

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    def _write_all(self, results: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(results, handle, ensure_ascii=False, indent=2)
            tmp_path.replace(self.path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
