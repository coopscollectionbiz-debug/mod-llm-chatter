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
        if "stagegen" in text:
            label, line = "general", "patrol turns at the bridge"
        elif "stageparty" in text:
            label, line = "party", "mana sec"
        elif "stagefollow" in text or "@follow" in text:
            label, line = "unexpected-control", ""
        else:
            label, line = "other", ""
        # Only fixture classes, never prompt bodies.
        print(label, flush=True)
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
