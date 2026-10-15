"""MapleDash: free-market analytics from everything MaplePrice has scanned.

    python -m mapledash            # opens the dashboard in your browser
"""
import argparse
import logging
import os
import webbrowser
from pathlib import Path

from .queries import Market
from .server import serve

DEFAULT_DB = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mapleprice" / "items.sqlite"


def main():
    p = argparse.ArgumentParser(prog="mapledash", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), help="MaplePrice's database")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if not Path(args.db).exists():
        raise SystemExit(f"No MaplePrice data at {args.db} yet: scan some shops with MaplePrice first.")
    server = serve(Market(Path(args.db).as_posix()), args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"MapleDash running at {url}  (close this window to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
