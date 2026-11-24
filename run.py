"""Run PocketLedger locally (works on Windows, no Docker).

    python run.py                # production-grade server (waitress) on http://127.0.0.1:5000
    python run.py --debug        # Flask dev server with auto-reload
    python run.py --host 0.0.0.0 # expose on your LAN (e.g. to install the PWA on your phone)
"""
import argparse

from pocketledger import create_app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    app = create_app()
    if not app.config.get("REQUIRE_LOGIN", True) and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: login is OFF and you are listening on %s. Anyone who can reach this address can read and change "
              "your finances. Set REQUIRE_LOGIN=true or bind to 127.0.0.1." % args.host)
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
        return
    from waitress import serve
    print(f"PocketLedger running on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    serve(app, host=args.host, port=args.port, threads=4)


if __name__ == "__main__":
    main()
