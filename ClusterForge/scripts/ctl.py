"""Drive an ClusterForge control action and follow the job to completion.

Usage:  python scripts\\ctl.py <server> <start|stop|restart|kill> [--no-warn]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = "http://127.0.0.1:8090"


def call(path: str, payload: dict | None = None):
    url = PANEL + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return json.loads(body), exc.code
        except json.JSONDecodeError:
            return {"error": body}, exc.code


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    server, action = sys.argv[1], sys.argv[2]
    payload: dict = {}
    if "--no-warn" in sys.argv:
        payload["warn_minutes"] = []

    print(f"--> {action} {server}")
    body, status = call(f"/api/server/{server}/{action}", payload)
    if status >= 400:
        print(f"REFUSED ({status}): {body.get('message') or body.get('error')}")
        return 1

    job_id = body["job"]["id"]
    print(f"    job {job_id}\n")

    seen = 0
    while True:
        job, _ = call(f"/api/server/{server}/jobs/{job_id}")
        steps = job.get("steps", [])
        for step in steps[seen:]:
            mark = {"error": "!!", "warn": " !"}.get(step["level"], "  ")
            print(f"  {mark} [{step['t']:>6.1f}s] {step['message']}")
        seen = len(steps)

        if job.get("state") != "running":
            print()
            print(f"    {job['state'].upper()} after {job.get('elapsed')}s")
            if job.get("error"):
                print(f"    error: {job['error']}")
            return 0 if job["state"] == "done" else 1
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
