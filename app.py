from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def is_turbodm_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.2) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return '"TurboDM"' in body
    except Exception:
        return False


def find_free_port(host: str, start_port: int) -> int:
    for port in range(start_port, start_port + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start_port


def open_desktop_window(url: str) -> None:
    """Open TurboDM in a clean standalone desktop app window (Edge/Chrome --app mode)."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for exe in candidates:
        if os.path.exists(exe):
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1320,820"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                continue
    webbrowser.open(url)


def _open_when_ready(host: str, port: int) -> None:
    url = f"http://{host}:{port}"
    for _ in range(50):
        if is_turbodm_running(host, port):
            open_desktop_window(url)
            return
        time.sleep(0.15)
    open_desktop_window(url)


def main() -> None:
    # Safeguard for pythonw.exe (windowless launch via Launch_TurboDM.vbs) where stdio is None
    if sys.stdout is None or sys.stderr is None:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(log_dir, exist_ok=True)
        log_fp = open(os.path.join(log_dir, "turbodm.log"), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_fp
        if sys.stderr is None:
            sys.stderr = log_fp

    parser = argparse.ArgumentParser(description="TurboDM — High-Speed Download Manager")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-window", action="store_true", help="Run server without launching UI window")
    args = parser.parse_args()

    # If TurboDM is already running on this port, just pop open the UI window!
    if is_turbodm_running(args.host, args.port):
        url = f"http://{args.host}:{args.port}"
        print(f"TurboDM is already running at {url}")
        if not args.no_window:
            open_desktop_window(url)
        return

    port = find_free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    print(f"Starting TurboDM High-Speed Download Manager at {url} ...")

    if not args.no_window:
        threading.Thread(target=_open_when_ready, args=(args.host, port), daemon=True).start()

    uvicorn.run(
        "backend.server:app",
        host=args.host,
        port=port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
