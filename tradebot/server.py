import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tradebot.allocation import allocation_config_from_dict, allocation_config_to_dict, build_allocations, default_allocation_config
from tradebot.dashboard import build_dashboard_state


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
ALLOCATION_CONFIG = default_allocation_config()


def allocation_rows_to_dict(config):
    return [
        {
            "symbol": row.symbol,
            "totalPct": row.total_pct,
            "tPct": row.t_pct,
            "symbolBudget": row.symbol_budget,
            "coreBudget": row.core_budget,
            "tBudget": row.t_budget,
        }
        for row in build_allocations(config)
    ]


def build_config_response(raw_body: bytes):
    global ALLOCATION_CONFIG
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        config = allocation_config_from_dict(payload)
        rows = build_allocations(config)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}

    ALLOCATION_CONFIG = config
    return 200, {
        "allocation": allocation_config_to_dict(config),
        "allocationRows": [
            {
                "symbol": row.symbol,
                "totalPct": row.total_pct,
                "tPct": row.t_pct,
                "symbolBudget": row.symbol_budget,
                "coreBudget": row.core_budget,
                "tBudget": row.t_budget,
            }
            for row in rows
        ],
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self):
        if self.path == "/api/state":
            payload = json.dumps(build_dashboard_state(allocation_config=ALLOCATION_CONFIG), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/allocation":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_config_response(self.rfile.read(length))
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Trade dashboard running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
