from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from exporter_v7 import V7Collector


class Handler(BaseHTTPRequestHandler):
    collector: V7Collector

    def do_GET(self) -> None:
        if self.path == "/healthz":
            try:
                healthy, detail = self.collector.health()
            except Exception:
                healthy, detail = False, "v7_runtime_health_check_failed"
            body = (detail + "\n").encode("utf-8")
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = self.collector.collect().encode("utf-8")
            self.send_response(200)
        except Exception as exc:
            body = ("collector_error " + type(exc).__name__ + "\n").encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V7-only Polymarket Prometheus exporter")
    parser.add_argument("--runs-base", type=Path, default=Path("runs"))
    parser.add_argument("--run-name", default="auto")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--top-opportunities", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_name = "paper_v7_live" if str(args.run_name).lower() == "auto" else str(args.run_name)
    if run_name != "paper_v7_live":
        raise SystemExit(f"legacy/non-V7 run name rejected: {run_name}")
    config = Path(args.config) if args.config else args.config_dir / "paper_v7.json"
    Handler.collector = V7Collector(args.runs_base / run_name, config, args.top_opportunities)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"polymarket V7 exporter listening on http://{args.host}:{args.port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
