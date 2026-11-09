"""Verifies the LLM client against a local HTTPS server with a SELF-SIGNED certificate:
  * the Bearer key is sent as "Authorization: Bearer <key>"
  * INTERNAL_LLM_VERIFY_SSL=false makes the call succeed; true correctly fails
  * llm_json copes with <think> blocks and code fences

    python test_llm_http.py      (needs the `openssl` command-line tool to make a throwaway certificate)
"""
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import config

SEEN = {}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        SEEN["auth"], SEEN["path"], SEEN["model"] = self.headers.get("Authorization"), self.path, body.get("model")
        content = "<think>hmm</think>```json\n{\"status\": \"ready\", \"numbers\": [1, 2, 3]}\n```"
        reply = {"id": "x", "object": "chat.completion", "created": 0, "model": body.get("model"),
                 "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    if not shutil.which("openssl"):
        print("SKIP: openssl not available")
        return 0
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp / "k.pem"),
                    "-out", str(tmp / "c.pem"), "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], check=True, capture_output=True)
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp / "c.pem"), str(tmp / "k.pem"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    config.LLM_URL = f"https://127.0.0.1:{srv.server_port}/v1"
    config.LLM_KEY, config.LLM_MODEL = "secret-test-key", "qwen-test"
    import llm

    ok = True

    config.LLM_VERIFY_SSL = False
    out = llm.llm_json(llm.get_llm(), "sys", "user")
    good = out == {"status": "ready", "numbers": [1, 2, 3]}
    print(f"[{'ok' if good else 'FAIL'}] verify=false: call succeeds, JSON extracted from <think>/fences -> {out}")
    ok &= good
    good = SEEN.get("auth") == "Bearer secret-test-key"
    print(f"[{'ok' if good else 'FAIL'}] Authorization header = {SEEN.get('auth')!r}, path = {SEEN.get('path')}, model = {SEEN.get('model')}")
    ok &= good

    config.LLM_VERIFY_SSL = True
    try:
        llm.get_llm().invoke("hi")
        print("[FAIL] verify=true should have rejected the self-signed certificate")
        ok = False
    except Exception as exc:
        print(f"[ok] verify=true correctly rejects the self-signed cert ({type(exc).__name__})")

    config.LLM_VERIFY_SSL, config.LLM_CA_BUNDLE = True, str(tmp / "c.pem")
    try:
        llm.get_llm().invoke("hi")
        print("[ok] INTERNAL_LLM_CA_BUNDLE (the proper alternative) works with verification ON")
    except Exception as exc:
        print(f"[FAIL] CA bundle path failed: {type(exc).__name__}: {exc}")
        ok = False

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
