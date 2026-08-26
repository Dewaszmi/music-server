#!/usr/bin/env python3
import json
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from import_youtube import run_import
from youtube_search import ResolveError

WEB_DIR = SCRIPT_DIR / "web"
HOST = "0.0.0.0"
PORT = 8765

jobs_lock = threading.Lock()
jobs: dict[int, dict] = {}
job_queue: queue.Queue[int] = queue.Queue()
listeners_lock = threading.Lock()
listeners: list[queue.Queue] = []
next_id = 1


def snapshot(job: dict) -> dict:
    return {
        "id": job["id"],
        "query": job["query"],
        "status": job["status"],
        "artist": job.get("artist"),
        "total": job.get("total", 0),
        "downloaded": job.get("downloaded", 0),
        "failed": job.get("failed", 0),
        "current": job.get("current"),
        "error": job.get("error"),
        "log": job.get("log", [])[-40:],
    }


def jobs_payload() -> str:
    with jobs_lock:
        items = [snapshot(job) for job in sorted(jobs.values(), key=lambda j: j["id"], reverse=True)]
    return json.dumps({"jobs": items})


def publish() -> None:
    payload = jobs_payload()
    with listeners_lock:
        dead = []
        for listener in listeners:
            try:
                listener.put_nowait(payload)
            except Exception:
                dead.append(listener)
        for listener in dead:
            listeners.remove(listener)


def append_log(job: dict, message: str) -> None:
    job.setdefault("log", []).append(message)


def worker() -> None:
    while True:
        job_id = job_queue.get()
        with jobs_lock:
            job = jobs[job_id]
            job["status"] = "running"
        publish()

        def on_event(event: str, payload: dict) -> None:
            with jobs_lock:
                if event == "resolved":
                    job["artist"] = payload.get("artist")
                    job["total"] = payload.get("total", 0)
                    append_log(job, f"Found {job['total']} release(s)")
                elif event == "release_start":
                    job["current"] = f"{payload['release_type']} · {payload['title']}"
                    append_log(
                        job,
                        f"[{payload['index']}/{payload['total']}] {payload['release_type']} {payload['title']}",
                    )
                elif event == "log":
                    append_log(job, payload.get("message") or "")
                elif event == "release_done":
                    job["downloaded"] = payload["downloaded"]
                    job["failed"] = payload["failed"]
                    job["current"] = None
                elif event == "release_failed":
                    job["downloaded"] = payload["downloaded"]
                    job["failed"] = payload["failed"]
                    job["current"] = None
                    reason = payload.get("error") or payload["title"]
                    append_log(job, f"Failed: {reason}")
                elif event == "finished":
                    job["downloaded"] = payload["downloaded"]
                    job["failed"] = payload["failed"]
            publish()

        try:
            run_import(job["query"], on_event=on_event)
            with jobs_lock:
                job["status"] = "done"
                job["current"] = None
                append_log(job, "Finished")
        except (ResolveError, RuntimeError, Exception) as exc:
            with jobs_lock:
                job["status"] = "error"
                job["error"] = str(exc)
                job["current"] = None
                append_log(job, f"Error: {exc}")
        finally:
            publish()
            job_queue.task_done()


def _docker_scan(container: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, "navidrome", "scan"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"Scan failed in {container}")
    return output or f"Scan started in {container}"


def navidrome_scan() -> str:
    compose = subprocess.run(
        ["docker", "compose", "exec", "-T", "navidrome", "navidrome", "scan"],
        cwd=SCRIPT_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if compose.returncode == 0:
        return ((compose.stdout or "") + (compose.stderr or "")).strip() or "Scan started"

    ps = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if ps.returncode == 0:
        for line in ps.stdout.splitlines():
            ident, image, name = (line.split("\t") + ["", "", ""])[:3]
            blob = f"{image} {name}".lower()
            if "navidrome" in blob:
                return _docker_scan(ident)

    details = (compose.stderr or compose.stdout or "").strip()
    raise RuntimeError(
        "Could not reach Navidrome. Start the navidrome compose service, "
        "or run a container named navidrome." + (f" ({details})" if details else "")
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict | list) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        data = json.loads(raw.decode())
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            html = (WEB_DIR / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/jobs":
            self._send(200, jobs_payload().encode(), "application/json; charset=utf-8")
            return
        if path == "/api/events":
            self._stream_events()
            return
        self._json(404, {"error": "not found"})

    def _stream_events(self) -> None:
        listener: queue.Queue = queue.Queue()
        with listeners_lock:
            listeners.append(listener)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        listener.put(jobs_payload())
        try:
            while True:
                payload = listener.get()
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            with listeners_lock:
                if listener in listeners:
                    listeners.remove(listener)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/jobs":
            try:
                data = self._read_json()
            except (json.JSONDecodeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            query = str(data.get("query") or "").strip()
            if not query:
                self._json(400, {"error": "query is required"})
                return
            global next_id
            with jobs_lock:
                job_id = next_id
                next_id += 1
                job = {
                    "id": job_id,
                    "query": query,
                    "status": "queued",
                    "artist": None,
                    "total": 0,
                    "downloaded": 0,
                    "failed": 0,
                    "current": None,
                    "error": None,
                    "log": [f"Queued: {query}"],
                }
                jobs[job_id] = job
            job_queue.put(job_id)
            publish()
            self._json(201, snapshot(job))
            return
        if path == "/api/scan":
            try:
                message = navidrome_scan()
            except Exception as exc:
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"ok": True, "message": message})
            return
        self._json(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    threading.Thread(target=worker, daemon=True).start()
    server = Server((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
