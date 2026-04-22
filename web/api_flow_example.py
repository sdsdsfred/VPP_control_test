"""API flow example: upload scenario -> start simulation -> poll status.

Usage:
    python web/api_flow_example.py --base-url http://127.0.0.1:8000
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request


def request_json(base_url: str, path: str, method: str = "GET", payload=None):
    url = f"{base_url.rstrip('/')}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.getcode(), json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            parsed = json.loads(body) if body else {"error": str(e)}
        except json.JSONDecodeError:
            parsed = {"error": body or str(e)}
        return e.code, parsed
    except urllib.error.URLError as e:
        return None, {"error": str(e)}


def wait_for_server(base_url: str, timeout_seconds: float, interval_seconds: float):
    started = time.time()
    last_error = None
    while True:
        code, resp = request_json(base_url, "/api/simulation/status")
        if code is not None and code < 500:
            return True

        last_error = resp
        if time.time() - started > timeout_seconds:
            return False, last_error

        time.sleep(max(0.1, interval_seconds))


def start_server_process(server_script: str):
    script_path = server_script
    if not os.path.isabs(script_path):
        script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), script_path))

    if not os.path.exists(script_path):
        raise SystemExit(f"Server script not found: {script_path}")

    # Run server with the same Python interpreter to keep env consistent.
    return subprocess.Popen([sys.executable, script_path], cwd=os.path.dirname(script_path))


def stop_server_process(proc: subprocess.Popen):
    if proc.poll() is not None:
        return

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def build_demo_payload(hours: int = 24, n_groups: int = 4, seed: int = 42):
    rng = random.Random(seed)
    n_days = (hours + 23) // 24
    day_load_factors = [max(0.88, min(1.12, 1.0 + rng.uniform(-0.10, 0.10))) for _ in range(n_days)]
    day_pv_factors = [max(0.80, min(1.20, 1.0 + rng.uniform(-0.15, 0.15))) for _ in range(n_days)]
    day_actual_bias = [max(0.94, min(1.06, 1.0 + rng.uniform(-0.04, 0.04))) for _ in range(n_days)]
    pv_forecast_min = []
    pv_forecast_max = []
    load_forecast_min = []
    load_forecast_max = []
    actual_loads = []

    for h in range(hours):
        hod = h % 24
        day_idx = h // 24
        # Deterministic synthetic profile for API demonstration.
        if 0 <= hod <= 5:
            day_factor = 0.86 - 0.02 * hod
        elif 6 <= hod <= 13:
            day_factor = 0.78 + (hod - 6) / (13 - 6) * (1.00 - 0.78)
        elif 14 <= hod <= 18:
            day_factor = 1.08 + 0.08 * (1.0 - abs(hod - 16) / 2.0)
        elif 19 <= hod <= 21:
            day_factor = 0.98 - 0.03 * (hod - 19)
        else:  # 22 <= hod <= 23
            day_factor = 0.74 - 0.03 * (hod - 22)

        base_load = 920.0 * day_factor * day_load_factors[day_idx]
        uncertainty = base_load * 0.045  # total forecast band ~= 9%

        # Daily PV profile: 00:00-05:00 and 20:00-24:00 = 0,
        # 06:00-14:00 rising, 15:00-20:00 falling.
        pv_peak = 260.0
        if 0 <= hod <= 5 or hod >= 20:
            pv_mid = 0.0
        elif 6 <= hod <= 14:
            pv_mid = (hod - 6) / (14 - 6) * pv_peak
        else:  # 15 <= hod <= 19
            pv_mid = (20 - hod) / (20 - 14) * pv_peak

        pv_mid *= day_pv_factors[day_idx]

        pv_band = 20.0 if pv_mid > 0 else 0.0

        load_min = max(0.0, base_load - uncertainty)
        load_max = base_load + uncertainty

        pv_forecast_min.append(max(0.0, pv_mid - pv_band))
        pv_forecast_max.append(pv_mid + pv_band)
        load_forecast_min.append(load_min)
        load_forecast_max.append(load_max)

        forecast_mid = (load_min + load_max) / 2.0
        if hod == 14:
            # Requirement: 14:00 actual can exceed forecast by more than 15%.
            actual_ratio = 1.15 + rng.uniform(0.0, 0.04)
            actual_ratio = max(1.15, min(1.20, actual_ratio))
        else:
            actual_ratio = day_actual_bias[day_idx] + rng.uniform(-0.03, 0.03)
            actual_ratio = max(0.92, min(1.08, actual_ratio))
        actual_total = forecast_mid * actual_ratio

        weights = [1.0 + (g - (n_groups - 1) / 2) * 0.04 for g in range(n_groups)]
        weight_sum = sum(weights)
        group_values = [actual_total * w / weight_sum for w in weights]
        actual_loads.append(group_values)

    # Make day1/day2 physical actuals close to day3 for multi-day demos.
    if n_days >= 3:
        for d in (0, 1):
            for hod in range(24):
                src_idx = d * 24 + hod
                ref_idx = 2 * 24 + hod
                if src_idx >= hours or ref_idx >= hours:
                    continue

                ref_total = float(sum(actual_loads[ref_idx]))
                if ref_total <= 0:
                    continue

                target_total = ref_total * (1.0 + rng.uniform(-0.03, 0.03))
                src_total = float(sum(actual_loads[src_idx]))
                if src_total > 0:
                    scale = target_total / src_total
                    actual_loads[src_idx] = [v * scale for v in actual_loads[src_idx]]
                else:
                    actual_loads[src_idx] = [target_total / n_groups for _ in range(n_groups)]

    return {
        "pv_forecast_min": pv_forecast_min,
        "pv_forecast_max": pv_forecast_max,
        "load_forecast_min": load_forecast_min,
        "load_forecast_max": load_forecast_max,
        "actual_loads": actual_loads,
        "market_daily_cap": float(sum(load_forecast_max) * 0.9),
    }


def main():
    parser = argparse.ArgumentParser(description="Upload scenario, start simulation and poll status")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--hours", type=int, default=24, help="Number of hourly points in demo payload")
    parser.add_argument("--groups", type=int, default=4, help="Number of load groups")
    parser.add_argument("--delay-seconds", type=float, default=2.0, help="Simulation event spacing")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Status polling interval")
    parser.add_argument("--timeout-seconds", type=float, default=90.0, help="Polling timeout")
    parser.add_argument("--wait-server-seconds", type=float, default=0.0, help="Wait for server to become reachable before upload")
    parser.add_argument("--payload-seed", type=int, default=42, help="Random seed for demo payload generation")
    parser.add_argument("--auto-start-server", action="store_true", help="Automatically start app.py as a subprocess")
    parser.add_argument("--server-script", default="app.py", help="Server script path when --auto-start-server is set")
    parser.add_argument("--keep-server-running", action="store_true", help="Do not stop auto-started server after flow ends")
    args = parser.parse_args()

    server_proc = None
    try:
        if args.auto_start_server:
            print("[0/3] Auto-starting server subprocess...")
            server_proc = start_server_process(args.server_script)
            wait_seconds = args.wait_server_seconds if args.wait_server_seconds > 0 else 20.0
            ok = wait_for_server(args.base_url, wait_seconds, args.poll_interval)
            if ok is not True:
                _, last_error = ok
                raise SystemExit(
                    "Server subprocess started but API is still unreachable. "
                    f"last_error={last_error}"
                )
            print("  server is reachable")
        elif args.wait_server_seconds > 0:
            print("[0/3] Waiting for server...")
            ok = wait_for_server(args.base_url, args.wait_server_seconds, args.poll_interval)
            if ok is not True:
                _, last_error = ok
                raise SystemExit(
                    "Server is not reachable. Please start web service first, e.g. `python web/app.py`. "
                    f"last_error={last_error}"
                )
            print("  server is reachable")

        payload = build_demo_payload(hours=args.hours, n_groups=args.groups, seed=args.payload_seed)

        print("[1/3] Uploading scenario...")
        code, resp = request_json(args.base_url, "/api/input/scenario", method="POST", payload=payload)
        print(f"  status={code} response={resp}")
        if code is None:
            raise SystemExit(
                "Cannot connect to API server. Please start service first, e.g. `python web/app.py`, "
                f"or use --wait-server-seconds. error={resp.get('error')}"
            )
        if code >= 400 or not resp.get("ok"):
            raise SystemExit("Upload failed")

        print("[2/3] Starting simulation...")
        start_payload = {
            "seed": 1,
            "delay_seconds": args.delay_seconds,
            "use_injected": True,
        }
        code, resp = request_json(args.base_url, "/api/simulation/start", method="POST", payload=start_payload)
        print(f"  status={code} response={resp}")
        if code is None:
            raise SystemExit(f"Cannot connect to API server while starting simulation: {resp.get('error')}")
        if code >= 400 or not resp.get("ok"):
            raise SystemExit("Start failed")

        print("[3/3] Polling status...")
        started = time.time()
        while True:
            code, resp = request_json(args.base_url, "/api/simulation/status")
            running = bool(resp.get("running")) if code is not None and code < 400 else None
            print(f"  status={code} running={running} payload={resp}")

            if code is None:
                raise SystemExit(f"Cannot connect to API server while polling status: {resp.get('error')}")

            if code >= 400:
                raise SystemExit("Status polling failed")

            if not running:
                print("Simulation finished.")
                break

            if time.time() - started > args.timeout_seconds:
                raise SystemExit("Polling timeout reached")

            time.sleep(max(0.1, args.poll_interval))
    finally:
        if server_proc is not None and not args.keep_server_running:
            print("Stopping auto-started server subprocess...")
            stop_server_process(server_proc)


if __name__ == "__main__":
    main()
