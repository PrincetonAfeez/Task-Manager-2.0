"""Closed-loop HTTP load generator for capacity measurements.

Standard library plus ``requests`` only. Each virtual user holds its own authenticated
session and issues requests back to back for a fixed duration; latency is measured
client-side per request, and the server-side figure is read from the ``Server-Timing``
header so the two can be compared.

    python scripts/loadtest.py --base http://localhost:8010 --username loadtest \
        --password ... --api-key ... --users 16 --duration 30 --out results.json

This is a measurement tool, not a benchmark suite: run it against a disposable
environment seeded with ``manage.py seed_load``.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
TIMING_RE = re.compile(r"dur=([0-9.]+)")
TASK_ID_RE = re.compile(r'data-task-id="(\d+)"')


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


class VirtualUser:
    def __init__(self, base: str, username: str, password: str, api_key: str):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        page = self.session.get(f"{self.base}/accounts/login/", timeout=30)
        token = CSRF_RE.search(page.text).group(1)
        response = self.session.post(
            f"{self.base}/accounts/login/",
            data={"csrfmiddlewaretoken": token, "username": username, "password": password},
            headers={"Referer": f"{self.base}/accounts/login/"},
            timeout=30,
        )
        if "/accounts/login" in response.url:
            raise SystemExit("login failed - check the load-test credentials")
        self.csrf = self.session.cookies.get("csrftoken", "")
        self.task_ids = [int(pk) for pk in TASK_ID_RE.findall(response.text)] or [1]
        self.counter = 0

    def _post_headers(self):
        return {"X-CSRFToken": self.session.cookies.get("csrftoken", self.csrf), "Referer": self.base + "/"}

    # ----------------------------------------------------------------- scenarios

    def dashboard(self):
        return self.session.get(f"{self.base}/", timeout=60)

    def task_list(self):
        return self.session.get(f"{self.base}/tasks/", timeout=60)

    def task_search(self):
        return self.session.get(f"{self.base}/tasks/", params={"q": "deploy"}, timeout=60)

    def board(self):
        return self.session.get(f"{self.base}/board/", timeout=60)

    def api_list(self):
        return requests.get(
            f"{self.base}/api/v1/tasks/",
            headers={"Authorization": f"Api-Key {self.api_key}"},
            timeout=60,
        )

    def quick_add(self):
        self.counter += 1
        return self.session.post(
            f"{self.base}/quick-add/create/",
            data={"quick_add": f"Load task {threading.get_ident()} {self.counter} #ops !3"},
            headers={**self._post_headers(), "HX-Request": "true"},
            timeout=60,
        )

    def toggle(self):
        self.counter += 1
        task_id = self.task_ids[self.counter % len(self.task_ids)]
        return self.session.post(
            f"{self.base}/tasks/{task_id}/toggle/",
            headers={**self._post_headers(), "HX-Request": "true"},
            timeout=60,
        )

    def api_create(self):
        self.counter += 1
        return requests.post(
            f"{self.base}/api/v1/tasks/",
            json={"title": f"API load {self.counter}", "task_list": self.api_list_id},
            headers={"Authorization": f"Api-Key {self.api_key}"},
            timeout=60,
        )


SCENARIOS = [
    ("GET /", "dashboard", {200}),
    ("GET /tasks/", "task_list", {200}),
    ("GET /tasks/?q=", "task_search", {200}),
    ("GET /board/", "board", {200}),
    ("GET /api/v1/tasks/", "api_list", {200}),
    ("POST /quick-add/create/", "quick_add", {200}),
    # 409 is a correct answer under contention: two users toggling one task collide on
    # the optimistic-concurrency check by design.
    ("POST /tasks/<id>/toggle/", "toggle", {200, 409}),
    ("POST /api/v1/tasks/", "api_create", {201}),
]


def run_scenario(users: list[VirtualUser], method: str, ok: set[int], duration: float, warmup: float):
    latencies: list[float] = []
    server: list[float] = []
    errors: dict[str, int] = {}
    lock = threading.Lock()
    start_measuring = time.perf_counter() + warmup
    stop_at = start_measuring + duration

    def loop(user: VirtualUser):
        call = getattr(user, method)
        local, local_server, local_errors = [], [], {}
        while time.perf_counter() < stop_at:
            began = time.perf_counter()
            try:
                response = call()
                status = response.status_code
                timing = TIMING_RE.search(response.headers.get("Server-Timing", ""))
            except requests.RequestException as exc:
                status, timing = type(exc).__name__, None
            elapsed = (time.perf_counter() - began) * 1000
            if began < start_measuring:
                continue
            if status in ok:
                local.append(elapsed)
                if timing:
                    local_server.append(float(timing.group(1)))
            else:
                local_errors[str(status)] = local_errors.get(str(status), 0) + 1
        with lock:
            latencies.extend(local)
            server.extend(local_server)
            for key, value in local_errors.items():
                errors[key] = errors.get(key, 0) + value

    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        list(pool.map(loop, users))

    return {
        "requests": len(latencies),
        "throughput_rps": round(len(latencies) / duration, 1),
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "p99_ms": round(percentile(latencies, 99), 1),
        "server_p50_ms": round(percentile(server, 50), 1),
        "server_p95_ms": round(percentile(server, 95), 1),
        "mean_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8010")
    parser.add_argument("--username", default="loadtest")
    parser.add_argument("--password", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--users", type=int, default=16)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--only", default="", help="Comma-separated scenario method names.")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    users = [VirtualUser(args.base, args.username, args.password, args.api_key) for _ in range(args.users)]
    listing = requests.get(
        f"{args.base}/api/v1/lists/", headers={"Authorization": f"Api-Key {args.api_key}"}, timeout=30
    ).json()
    for user in users:
        user.api_list_id = listing["results"][0]["id"]

    only = {name for name in args.only.split(",") if name}
    results = {}
    for label, method, ok in SCENARIOS:
        if only and method not in only:
            continue
        results[label] = run_scenario(users, method, ok, args.duration, args.warmup)
        row = results[label]
        print(
            f"{label:28} {row['throughput_rps']:>7} rps  p50 {row['p50_ms']:>7}  "
            f"p95 {row['p95_ms']:>7}  p99 {row['p99_ms']:>7}  server p95 {row['server_p95_ms']:>7}  "
            f"errors {row['errors'] or 0}"
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"users": args.users, "duration_s": args.duration, "results": results}, handle, indent=2)


if __name__ == "__main__":
    main()
