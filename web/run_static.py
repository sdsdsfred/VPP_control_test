"""Run a tiny static HTTP server and the simulation; the page polls latest.json.

Usage: python web/run_static.py
Then open http://localhost:8000/ in a browser.
"""
import http.server
import socketserver
import threading
import json
import os
import sys
from pathlib import Path

# ensure project root is importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# attempt import with diagnostics
try:
    import importlib
    print('run_static.py: sys.path[:3]=', sys.path[:3])
    print('run_static.py: PROJECT_ROOT=', PROJECT_ROOT)
    print('run_static.py: PROJECT_ROOT listing=', os.listdir(PROJECT_ROOT))
    print('run_static.py: vpp listing=', os.listdir(os.path.join(PROJECT_ROOT, 'vpp')))
    # main.py is at project root (d:\VPP\main.py)
    mod = importlib.import_module('main')
    run_demo = mod.run_demo
except Exception as e:
    print('run_static.py import error:', e)
    raise

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"
LATEST = STATIC_DIR / "latest.json"


def publisher_write(event: dict):
    try:
        with open(LATEST, 'w', encoding='utf-8') as f:
            json.dump(event, f)
    except Exception as e:
        print('Failed to write latest.json', e)


def start_static_server(port=8000):
    os.chdir(str(STATIC_DIR))
    handler = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("", port), handler)
    print(f"Serving static files from {STATIC_DIR} at http://localhost:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    # ensure latest.json exists
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if not LATEST.exists():
        LATEST.write_text('{}')

    # start static server in background thread
    t = threading.Thread(target=start_static_server, kwargs={'port':8000}, daemon=True)
    t.start()

    # run simulation and publish events by writing latest.json
    run_demo(publisher=publisher_write, seed=1)
