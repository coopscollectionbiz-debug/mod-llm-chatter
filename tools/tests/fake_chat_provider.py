"""Loopback-only deterministic Chat Completions fixture for staging."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{"id": "staging-fixture", "object": "model"}]})
        else:
            self.send_json(404, {"error": "unknown fixture endpoint"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/v1/chat/completions" or not 0 < length <= 250_000:
            self.send_json(400, {"error": "invalid fixture request"})
            return
        request = json.loads(self.rfile.read(length))
        text = " ".join(str(message.get("content", "")) for message in request["messages"]).casefold()
        # Prompts quote earlier turns, so the fixture keys off the keyword that
        # appears LAST (the current turn), not the first one in the transcript.
        fixtures = {
            "who starts this quest": ("who_conversation", "no idea, check the board by the gate"),
            "stagehello": ("hello", "hey, whats up"),
            "stagegen": ("general", "patrol turns at the bridge"),
            "stageparty": ("party", "mana sec"),
            "stagefollow": ("unexpected-control", ""),
            "@follow": ("unexpected-control", ""),
        }
        found = max(((text.rfind(key), key) for key in fixtures if key in text), default=None)
        label, line = fixtures[found[1]] if found else ("other", "")
        # Only fixture classes and engine markers, never prompt bodies.
        surface = "surface" if "surface (" in text else "-"
        print(label, surface, flush=True)
        self.send_json(200, {
            "id": "chatcmpl-staging", "object": "chat.completion",
            "created": int(time.time()), "model": "staging-fixture",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"message": line}),
            }}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 11479), FixtureHandler) as server:
        server.serve_forever()
