"""One command to start FieldPilot for a demo:

    python run_demo.py

It starts a Cloudflare quick tunnel, reads the public https address it prints, passes it to the
backend as PUBLIC_BASE_URL (no copying into .env), starts the server on port 8000, checks the
tunnel works, and opens the app in your browser. Ctrl+C stops everything.

Requires cloudflared (Windows: winget install --id Cloudflare.cloudflared).
Use --no-tunnel to use PUBLIC_BASE_URL from .env instead.
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

PORT = 8000
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_tunnel_url(lines, timeout: float = 60.0) -> str | None:
    """Return the first trycloudflare address in an iterable of output lines."""
    deadline = time.time() + timeout
    for line in lines:
        m = TUNNEL_RE.search(line)
        if m:
            return m.group(0)
        if time.time() > deadline:
            break
    return None


def start_tunnel() -> tuple[subprocess.Popen, str]:
    exe = shutil.which("cloudflared")
    if not exe:
        sys.exit("cloudflared not found. Install it (Windows: winget install --id "
                 "Cloudflare.cloudflared), open a NEW terminal, and run this again.")
    proc = subprocess.Popen([exe, "tunnel", "--url", f"http://localhost:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1)
    print("Starting Cloudflare tunnel...")
    url = find_tunnel_url(iter(proc.stdout.readline, ""))
    if not url:
        proc.terminate()
        sys.exit("Could not get a tunnel address from cloudflared. Try again.")
    # Keep draining cloudflared's output so it never blocks.
    threading.Thread(target=lambda: [None for _ in iter(proc.stdout.readline, "")],
                     daemon=True).start()
    return proc, url


def wait_until_public(url: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    tunnel = None
    if "--no-tunnel" not in sys.argv:
        tunnel, url = start_tunnel()
        os.environ["PUBLIC_BASE_URL"] = url  # overrides .env (load_dotenv never overrides)
        print(f"Tunnel: {url}")

    import uvicorn  # imported after PUBLIC_BASE_URL is set

    server = uvicorn.Server(uvicorn.Config("backend.main:app", host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    public = os.environ.get("PUBLIC_BASE_URL", "")
    if public:
        print("Checking the tunnel reaches this laptop (new tunnels can take ~30 s)...")
        ok = wait_until_public(public)
        print("Tunnel OK." if ok else "WARNING: tunnel not reachable yet. Voice may fail; "
              "text mode still works.")
    app = f"http://localhost:{PORT}"
    print("\n" + "=" * 60)
    print(f"  FieldPilot is running:  {app}")
    print(f"  Health check:           {app}/api/diag")
    print("  Press Ctrl+C to stop.")
    print("=" * 60 + "\n")
    webbrowser.open(app)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        server.should_exit = True
        if tunnel:
            tunnel.terminate()


if __name__ == "__main__":
    main()
