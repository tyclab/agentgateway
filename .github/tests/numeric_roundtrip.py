#!/usr/bin/env python3
"""Verify decimal MCP arguments/results through the release binary, using only loopback."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request


# The first literal exposed the default serde_json parser's one-ULP error.
LITERALS = "[0.012345678901234567,-0.012345678901234567,1.2345678901234567e-100,1.7976931348623157e308,5e-324,-0.0]"
EXPECTED = json.loads(LITERALS)


class Backend(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *_args):
        pass

    def reply(self, status, data=None):
        body = json.dumps(data).encode() if data is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(405)

    def do_DELETE(self):
        self.reply(204)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request["method"]
        if "id" not in request:
            self.reply(202)
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "numbers", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "roundtrip",
                        "description": "Round-trip numeric data",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "values": {"type": "array", "items": {"type": "number"}}
                            },
                            "required": ["values"],
                        },
                    }
                ]
            }
        elif method == "tools/call" and request["params"]["name"] == "roundtrip":
            values = request["params"]["arguments"]["values"]
            self.received.append(values)
            # Independent server-origin numbers distinguish the return path from an echo.
            payload = {"received": values, "server_values": json.loads(LITERALS)}
            result = {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
                "isError": False,
            }
        else:
            self.reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32601, "message": "Unknown method"},
                },
            )
            return
        self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rpc(url, payload, session=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode()
        session = response.headers.get("Mcp-Session-Id", session)
    chunks = [
        line[5:].strip() for line in body.splitlines() if line.startswith("data:")
    ]
    message = json.loads(chunks[-1] if chunks else body) if body else None
    if message is not None and "error" in message:
        raise AssertionError(message["error"])
    return session, message


def assert_numbers(values, direction):
    assert isinstance(values, list) and all(
        type(value) in (int, float) for value in values
    ), f"{direction} returned non-numeric JSON values: {values!r}"
    actual = [float(value).hex() for value in values]
    expected = [value.hex() for value in EXPECTED]
    assert actual == expected, (
        f"{direction} changed IEEE-754 values: {actual} != {expected}"
    )


def check(binary):
    Backend.received = []
    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="gateway-numbers-") as directory:
            root = Path(directory)
            port = free_port()
            config = {
                "config": {
                    name: "127.0.0.1:0"
                    for name in ("statsAddr", "adminAddr", "readinessAddr")
                },
                "gateways": {"default": {"port": port, "bindAddress": "127.0.0.1"}},
                "mcp": {
                    "prefixMode": "always",
                    "targets": [
                        {
                            "name": "numbers",
                            "mcp": {
                                "host": f"http://127.0.0.1:{backend.server_port}/mcp"
                            },
                        }
                    ],
                },
            }
            config_path = root / "config.yaml"
            config_path.write_text(json.dumps(config))  # JSON is valid YAML.
            log_path = root / "gateway.log"
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    [str(binary), "-f", str(config_path)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            try:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"gateway exited with {process.returncode}")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "gateway did not start within 15 seconds"
                            ) from None
                        time.sleep(0.05)
                url = f"http://127.0.0.1:{port}/mcp"
                session, initialized = rpc(
                    url,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "release-numbers", "version": "1"},
                        },
                    },
                )
                assert initialized and "result" in initialized, initialized
                rpc(
                    url,
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    session,
                )
                _, listed = rpc(
                    url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session
                )
                assert any(
                    tool["name"] == "numbers_roundtrip"
                    for tool in listed["result"]["tools"]
                )
                _, reply = rpc(
                    url,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "numbers_roundtrip",
                            "arguments": {"values": EXPECTED},
                        },
                    },
                    session,
                )
                assert Backend.received, (
                    "the request never reached the independent backend"
                )
                assert_numbers(Backend.received[-1], "client → backend")
                result = reply["result"]
                assert not result.get("isError"), result
                for field in ("received", "server_values"):
                    assert_numbers(
                        result["structuredContent"][field],
                        f"backend → client ({field})",
                    )
                    assert_numbers(
                        json.loads(result["content"][0]["text"])[field],
                        f"JSON text ({field})",
                    )
            except BaseException:
                print(log_path.read_text()[-4000:])
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
    print(
        f"Numeric round-trip passed: {len(EXPECTED)} decimals, both directions, real release binary"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    check(parser.parse_args().binary.resolve())
