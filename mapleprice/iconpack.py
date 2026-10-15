"""Every item's icon, bundled with MaplePrice: assets/icons.zip holds one PNG per MeowDB icon
key ("4010002.png"). So the shop panel, the hover card,
MapleDash and the icon colour check never wait on a download, and never show the shop
screenshot's crop (stack count drawn over it, sometimes blank) when there's a clean one.

Items added to the game after a build aren't in it: those are still downloaded once (data.py).

Refresh the pack after a game patch, from the repo root (Windows Python):
    py -3.12 -m mapleprice.iconpack
It downloads MeowDB's item list, keeps the icons the pack already has, and fetches only the
missing ones (Cloudflare-cached files, a short pause between them). Stopped part way, it carries
on from where it got to.
"""
import argparse
import logging
import sys
import threading
import time
import zipfile
from pathlib import Path

PACK = Path(__file__).resolve().parent / "assets" / "icons.zip"
PNG = b"\x89PNG\r\n\x1a\n"
log = logging.getLogger("mapleprice")


class IconPack:
    """Reads a pack. With no pack (a dev run before one's made), every lookup is just a miss."""

    def __init__(self, path=PACK):
        self.path = Path(path)
        self._zip = None
        self._lock = threading.Lock()  # one open file, shared by the row lookups

    def png(self, icon_key):
        """The icon's PNG bytes, or None if the pack hasn't got it."""
        if not icon_key:
            return None
        with self._lock:
            if self._zip is None:
                try:
                    self._zip = zipfile.ZipFile(self.path)
                except (OSError, zipfile.BadZipFile):
                    self._zip = False
            if not self._zip:
                return None
            try:
                return self._zip.read(f"{icon_key}.png")
            except KeyError:
                return None


BUNDLED = IconPack()


# --- making the pack ---------------------------------------------------------------------------

STAMP = (2020, 1, 1, 0, 0, 0)  # every entry's date: an unchanged pack rebuilds to the same bytes
SAVE_EVERY = 200               # icons between saves of the work in progress
PAUSE = 0.1                    # s between downloads


def catalog_keys(get):
    """{icon key: item name} for MeowDB's whole item list. `get(path)` returns parsed JSON."""
    keys, page = {}, 1
    while page <= 200:  # a stop, in case its paging never ends
        d = get(f"items?limit=500&page={page}")
        for i in d.get("items") or []:
            key = str(i.get("iconKey") or "").strip()
            if key.isalnum():
                keys.setdefault(key, i.get("name"))
        if not d.get("hasMore"):
            break
        page += 1
    return keys


def _entries(path):
    """{key: png} in a pack (or a work-in-progress one); {} if there's none or it's unreadable."""
    try:
        with zipfile.ZipFile(path) as z:
            return {n[:-4]: z.read(n) for n in z.namelist() if n.endswith(".png")}
    except (OSError, zipfile.BadZipFile):
        return {}


def _write(path, icons, mode="w"):
    with zipfile.ZipFile(path, mode, zipfile.ZIP_STORED) as z:  # PNGs are compressed already
        for key in sorted(icons):
            z.writestr(zipfile.ZipInfo(f"{key}.png", STAMP), icons[key])


def build(out=PACK, get=None, fetch_icon=None, pause=PAUSE, say=print):
    """Make (or bring up to date) the pack at `out`. Returns (icons in it, downloaded, missing on MeowDB)."""
    out = Path(out)
    work = out.with_name(out.name + ".partial")
    if get is None or fetch_icon is None:
        get, fetch_icon = _meowdb()
    keys = catalog_keys(get)
    if not keys:
        raise RuntimeError("MeowDB's item list came back empty: pack left as it was")
    have = {k: v for k, v in {**_entries(out), **_entries(work)}.items() if k in keys}
    todo = sorted(set(keys) - set(have))
    say(f"{len(keys)} items on MeowDB, {len(have)} icons already packed, {len(todo)} to download")
    _write(work, have)  # a fresh start for the work in progress (an interrupted save can't break it)
    batch, fetched, missing = {}, 0, []
    for n, key in enumerate(todo, 1):
        png = fetch_icon(key)
        if png and png.startswith(PNG):
            batch[key] = png
            fetched += 1
        else:
            missing.append(key)
        if len(batch) >= SAVE_EVERY or n == len(todo):
            _write(work, batch, "a")  # a stop here loses at most a batch
            have.update(batch)
            batch = {}
            say(f"  {n}/{len(todo)}")
        if pause:
            time.sleep(pause)
    _write(work, have)  # just the current items, sorted: the finished pack
    work.replace(out)
    say(f"{out}: {len(have)} icons, {out.stat().st_size // 1024} KB"
        + (f" ({len(missing)} MeowDB has no icon for: {', '.join(missing[:10])})" if missing else ""))
    return len(have), fetched, missing


def _meowdb():
    import requests
    from .data import API, USER_AGENT
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    def get(path):
        r = s.get(f"{API}/{path}", timeout=30)
        r.raise_for_status()
        return r.json()

    def fetch_icon(key):
        for attempt in range(4):
            try:
                r = s.get(f"{API}/assets/icons/{key}", timeout=30)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.content
            except requests.RequestException:
                if attempt == 3:
                    raise  # the work so far is saved: run it again to carry on
                time.sleep(2 ** attempt * 5)

    return get, fetch_icon


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Make or update MaplePrice's bundled icon pack.")
    p.add_argument("--out", type=Path, default=PACK)
    args = p.parse_args()
    try:
        build(args.out)
    except Exception as e:
        sys.exit(f"icon pack not finished: {e}")
