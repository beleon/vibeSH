#!/usr/bin/env python3
"""Offline integration test: Tab completion end to end against a mock OpenAI server.

Deterministic and login-free — exercises the real readline completer, the local
backend, and the [COMPLETE] round trip. Run: uv run python tests/test_completion_mock.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

PORT = 8137
seen_complete = []


def ndjson_for(messages):
    last = messages[-1]["content"] if messages else ""
    if "[COMPLETE" in last:
        seen_complete.append(last)
        return '{"type": "complete", "candidates": ["checkout"]}'
    if "[BOOT]" in last:
        return ('{"type": "chunk", "text": "vibebox login: user\\n"}'
                '{"type": "prompt", "text": "user@vibebox:~$ "}')
    return ('{"type": "chunk", "text": "done\\n"}'
            '{"type": "prompt", "text": "user@vibebox:~$ "}')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        content = ndjson_for(json.loads(body).get("messages", []))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in [({"role": "assistant", "content": content}, None), ({}, "stop")]:
            obj = {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "local",
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    s = Session("--backend", "local", "--base-url", f"http://127.0.0.1:{PORT}/v1")
    s.drain(6)                      # boot
    s.send(b"git che")
    s.drain(1)
    s.send(b"\t")                   # Tab -> [COMPLETE] -> readline inserts "checkout"
    s.drain(3)
    after_tab = s.text()
    s.send(b"\rexit\r")
    s.drain(2)
    s.kill()
    srv.shutdown()

    report("completion-mock", [
        ("server received a [COMPLETE] request", len(seen_complete) >= 1),
        ("completion completed 'git che' -> 'git checkout'", "git checkout" in after_tab),
    ])


if __name__ == "__main__":
    main()
