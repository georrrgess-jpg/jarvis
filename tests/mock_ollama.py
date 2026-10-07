"""A tiny stand-in for the Ollama HTTP API, for tests and for UI work without a real model.

    python -m tests.mock_ollama            # serve on http://127.0.0.1:11434
    python -m tests.mock_ollama --port 11500 --no-models
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_REPLY = (
    "Certainly, sir. All primary systems are operating within normal parameters. "
    "The local neural core is responding in well under a second, and I have taken the liberty "
    "of keeping the coffee warm."
)


class MockOllama:
    def __init__(self, port: int = 0, models: list[str] | None = None, reply: str | None = None,
                 token_delay: float = 0.0) -> None:
        self.models = ["llama3.2:latest"] if models is None else models
        self.reply = reply or DEFAULT_REPLY
        self.token_delay = token_delay
        self.requests: list[tuple[str, dict]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output clean
                pass

            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, chunks) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for chunk in chunks:
                        data = (json.dumps(chunk) + "\n").encode()
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):
                owner.requests.append((self.path, {}))
                if self.path == "/api/tags":
                    models = [
                        {"name": m, "model": m, "modified_at": "2026-01-01T00:00:00Z", "size": 2_019_393_189,
                         "digest": "a80c4f17acd5", "details": {"family": "llama", "parameter_size": "3.2B",
                                                                 "quantization_level": "Q4_K_M"}}
                        for m in owner.models
                    ]
                    return self._json(200, {"models": models})
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.12.0-mock"})
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                body = self._body()
                owner.requests.append((self.path, body))
                model = body.get("model", "")
                if self.path in ("/api/chat", "/api/generate") and model not in owner.models:
                    return self._json(404, {"error": f"model '{model}' not found"})
                if self.path == "/api/generate":
                    return self._json(200, {"model": model, "response": "", "done": True})
                if self.path == "/api/chat":
                    if not body.get("stream", True):
                        return self._json(200, {"model": model, "message": {"role": "assistant", "content": owner.reply},
                                                "done": True})
                    return self._stream(owner._chat_chunks(model))
                if self.path == "/api/pull":
                    return self._stream(owner._pull_chunks(model or body.get("name", "")))
                return self._json(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _chat_chunks(self, model: str):
        for token in re.findall(r"\S+\s*|\s+", self.reply):
            if self.token_delay:
                time.sleep(self.token_delay)
            yield {"model": model, "created_at": "2026-01-01T00:00:00Z",
                   "message": {"role": "assistant", "content": token}, "done": False}
        yield {"model": model, "created_at": "2026-01-01T00:00:00Z", "message": {"role": "assistant", "content": ""},
               "done": True, "done_reason": "stop", "eval_count": 42}

    def _pull_chunks(self, model: str):
        yield {"status": "pulling manifest"}
        total = 2_000_000
        for done in range(0, total + 1, 400_000):
            if self.token_delay:
                time.sleep(self.token_delay)
            yield {"status": "pulling dde5aa3fc5ff", "digest": "sha256:dde5aa3fc5ff", "total": total, "completed": done}
        yield {"status": "verifying sha256 digest"}
        yield {"status": "writing manifest"}
        if model and model not in self.models:
            self.models.append(model if ":" in model else f"{model}:latest")
        yield {"status": "success"}

    def start(self) -> "MockOllama":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--no-models", action="store_true", help="start with no installed models")
    parser.add_argument("--delay", type=float, default=0.04, help="seconds between streamed tokens")
    args = parser.parse_args()
    mock = MockOllama(args.port, models=[] if args.no_models else None, token_delay=args.delay).start()
    print(f"Mock Ollama listening on {mock.url} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mock.stop()


if __name__ == "__main__":
    main()
