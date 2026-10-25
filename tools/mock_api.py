"""Local-only controllable Neptun substitute; no host ports are published."""
import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen


MODES = ("off", "red", "yellow", "oblast-red", "invalid", "error")


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class MockServer(HTTPServer):
    mode = "off"
    changed_at = timestamp()
    version = 1


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"mode": self.server.mode})
            return
        if self.path != "/api/v1/alerts":
            self.reply(404, {"error": "not found"})
            return
        mode = self.server.mode
        if mode == "error":
            self.reply(503, {"error": "simulated source outage"})
            return
        if mode == "invalid":
            self.reply(200, {"oblasts": []})
            return
        city = []
        if mode in {"red", "yellow"}:
            city = [{"key": "м. київ", "name": "м. Київ", "oblast": "",
                     "level": mode, "since": self.server.changed_at}]
        elif mode == "oblast-red":
            city = [{"key": "київська", "name": "Київська область", "oblast": "",
                     "level": "red", "since": self.server.changed_at}]
        self.reply(200, {"version": self.server.version,
                         "updatedAt": self.server.changed_at,
                         "raions": [], "oblasts": city})

    def do_POST(self):
        if self.path != "/control":
            self.reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError()
            mode = json.loads(self.rfile.read(length))["mode"]
            if mode not in MODES:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            self.reply(400, {"error": "invalid mode"})
            return
        self.server.mode = mode
        self.server.changed_at = timestamp()
        self.server.version += 1
        self.reply(200, {"mode": mode})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("check")
    setter = commands.add_parser("set")
    setter.add_argument("mode", choices=MODES)
    args = parser.parse_args()
    if args.command == "serve":
        print("Mock API listening on :8000; initial mode=off", flush=True)
        MockServer(("0.0.0.0", 8000), Handler).serve_forever()
    elif args.command == "check":
        with urlopen("http://127.0.0.1:8000/health", timeout=1) as response:
            print(response.read().decode("utf-8"))
    else:
        request = Request("http://127.0.0.1:8000/control",
                          data=json.dumps({"mode": args.mode}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=2) as response:
            print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
