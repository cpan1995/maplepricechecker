"""Item lookup: OCR'd item name -> NPC sell price, Free Market price, and the
lowest/highest prices we've seen it listed for in player shops.

Data comes from MeowDB's MapleStory Classic API and is cached in a local SQLite
file. NPC prices never change, so they're kept forever; FM prices and seen shop
prices expire after the TTL (24h unless changed in MapleDash's Settings tab).
Names are matched with fuzzy string scoring (handles OCR typos and names the
shop UI truncates with "...").
"""
import io
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
from PIL import Image
from rapidfuzz import fuzz

from .iconpack import BUNDLED
from .icons import feature
from .version import VERSION

API = "https://meowdb.com/msclassic/api"
USER_AGENT = f"mapleprice-overlay/{VERSION} (personal shop price helper)"
# Kept on a local drive: SQLite locking fails on network shares like \\wsl$.
DEFAULT_DB = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mapleprice" / "items.sqlite"

MIN_SCORE = 85      # candidates scoring below this are not considered a match
EXACT_SCORE = 95    # a full-name match this good beats any prefix matches
TIE_MARGIN = 3      # keep every candidate this close to the best score
MIN_PREFIX = 8      # shortest OCR text we'll treat as a possibly-cut-off name
TTL = 24 * 3600     # default expiry of FM prices and seen shop prices (see ItemDB.ttl)


def normalize(name):
    name = name.replace("’", "'").replace("`", "'").lower()
    return re.sub(r"\s+", " ", name).strip()


def strip_truncation(name):
    """'Hat Accuracy Scroll: Inter...' -> ('Hat Accuracy Scroll: Inter', True)."""
    stripped = re.sub(r"\s*(\.{2,}|…)\s*$", "", name)
    return stripped, stripped != name


@dataclass
class Item:
    id: int
    name: str
    price: int


@dataclass
class Market:
    usual: int | None   # 14-day median of player reports
    reports: int        # all reports behind `usual`
    sold: int = 0       # of those, reported sales and finished trades
    for_sale: int = 0   # live MeowDB listings
    cheapest_sell: int | None = None
    best_buy: int | None = None


@dataclass
class Seen:
    low: float          # per-unit prices seen in shops within TTL
    high: float
    count: int          # distinct prices seen


@dataclass
class Match:
    query: str
    items: list = field(default_factory=list)  # every plausible Item

    @property
    def found(self):
        return bool(self.items)


class MeowDB:
    def __init__(self, timeout=10):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # rows are looked up in parallel; let them share connections
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.timeout = timeout

    def _get(self, path, **params):
        r = self.session.get(f"{API}/{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def search(self, query):
        return [(i["id"], i["name"]) for i in self._get("search", q=query).get("items", [])]

    def item(self, item_id):
        """(Item, info): one request gives the NPC price, stats and icon key."""
        d = self._get(f"items/{item_id}")
        item = Item(d["id"], d["name"], int((d.get("stats") or {}).get("price") or 0))
        info = {k: d.get(k) for k in ("type", "subtype", "levelReq", "reqJob", "stats", "iconKey")}
        return item, info

    def icon_png(self, icon_key):
        r = self.session.get(f"{API}/assets/icons/{icon_key}", timeout=self.timeout)
        r.raise_for_status()
        return r.content

    def market(self, item_id):
        r = self.session.get(f"{API}/item-market-summary", params={"itemId": item_id, "condition": "clean"},
                             timeout=self.timeout)
        if r.status_code == 400:  # "This item can't be traded on the Free Market": no price, and none coming
            return Market(None, 0)
        r.raise_for_status()
        d = r.json()
        sold = (d.get("soldChecks") or 0) + (d.get("finishedTrades") or 0)
        return Market(d.get("usual"), (d.get("priceChecks") or 0) + sold, sold,
                      d.get("forSale") or 0, d.get("cheapestSell"), d.get("bestBuy"))


class ItemDB:
    """Local cache in front of MeowDB. Safe to call from several threads: SQLite
    access is serialised, network requests are not (they run outside the lock)."""

    def __init__(self, path=DEFAULT_DB, source=None, clock=time.time, mapper=map, icons=None):
        self.source = source or MeowDB()
        self.icons = icons if icons is not None else BUNDLED  # every item's icon, shipped with MaplePrice
        self.mapper = mapper  # e.g. a thread pool's map, to fetch candidates in parallel
        self.clock = clock
        self.lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL,
                fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS searches (
                query TEXT PRIMARY KEY, results TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS market_v2 (
                item_id INTEGER PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS icons (
                item_id INTEGER PRIMARY KEY, png BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS item_info (
                item_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS icon_colors (
                item_id INTEGER PRIMARY KEY, r REAL, g REAL, b REAL);
            CREATE TABLE IF NOT EXISTS seen (
                item_id INTEGER NOT NULL, unit_price REAL NOT NULL, seen_at REAL NOT NULL,
                PRIMARY KEY (item_id, unit_price));

            -- Permanent market log (never expires): one row per F8 scan, one per
            -- listing in it. This is what MapleDash analyses.
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                shop_title TEXT, seller TEXT, listings INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER, at REAL NOT NULL,
                item_id INTEGER, ocr_name TEXT NOT NULL, qty INTEGER NOT NULL,
                price INTEGER, unit_price REAL, confirmed INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS sightings_item ON sightings(item_id, at);
            CREATE INDEX IF NOT EXISTS sightings_scan ON sightings(scan_id);
            CREATE TABLE IF NOT EXISTS shop_icons (       -- icon as the shop shows it
                item_id INTEGER PRIMARY KEY, png BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        for table, column in (("scans", "channel INTEGER"), ("scans", "fm_room INTEGER"), ("scans", "map_name TEXT"),
                              ("sightings", "sold INTEGER NOT NULL DEFAULT 0"),
                              ("scans", "scanned_by TEXT")):  # added after the first release
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # already there
        self.conn.execute("PRAGMA journal_mode=WAL")  # MapleDash reads while we write
        self._import_old_sightings()

    def _import_old_sightings(self):
        """One-off: copy prices seen before the permanent log existed. Those only
        kept a per-piece price, not the stack size, so qty is 0 ("unknown")."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM meta WHERE key='imported_seen'").fetchone():
                return
            self.conn.execute(
                """INSERT INTO sightings (scan_id, at, item_id, ocr_name, qty, price, unit_price, confirmed)
                   SELECT NULL, s.seen_at, s.item_id, COALESCE(i.name, ''), 0, NULL, s.unit_price, 1
                   FROM seen s LEFT JOIN items i ON i.id = s.item_id""")
            self.conn.execute("INSERT INTO meta VALUES ('imported_seen', '1')")
            self.conn.commit()

    REPEAT_WINDOW = 30 * 60  # rescanning the same shop within this doesn't log its listings again

    def record_scan(self, title, seller, listings, channel=None, fm_room=None, map_name=None, scanned_by=None):
        """Log one scan, with where it happened (channel, FM room or map) so you can find
        the shop again, and who scanned it (your character's name off the status bar).
        `listings` is [(item_id or None, ocr_name, qty, price, confirmed, icon_png[, sold])].
        Listings already logged for this shop in the last REPEAT_WINDOW are skipped,
        unless they've sold since: that's logged once, as a sale."""
        now = self.clock()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO scans (at, shop_title, seller, listings, channel, fm_room, map_name, scanned_by)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now, title, seller, len(listings), channel, fm_room, map_name, scanned_by))
            scan_id = cur.lastrowid
            for item_id, name, qty, price, confirmed, icon, *sold in listings:
                sold = int(bool(sold and sold[0]))
                repeat = (seller or title) and self.conn.execute(
                    """SELECT 1 FROM sightings s JOIN scans c ON c.id = s.scan_id
                       WHERE s.at > ? AND s.ocr_name = ? AND s.qty = ? AND s.price IS ? AND s.sold = ?
                         AND c.seller IS ? AND c.shop_title IS ? LIMIT 1""",
                    (now - self.REPEAT_WINDOW, name, qty, price, sold, seller, title)).fetchone()
                if repeat:
                    continue
                self.conn.execute(
                    """INSERT INTO sightings (scan_id, at, item_id, ocr_name, qty, price, unit_price, confirmed, sold)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (scan_id, now, item_id, name, qty, price,
                     price / qty if price is not None and qty else None, int(bool(confirmed)), sold))
                if item_id is not None and icon:
                    self.conn.execute("INSERT OR IGNORE INTO shop_icons VALUES (?,?)", (item_id, icon))
            self.conn.commit()
        return scan_id

    def ttl(self):
        """Seconds FM prices and seen shop prices stay fresh. MapleDash can change
        it (meta 'ttl'), so it's read each time rather than once at startup."""
        value = self.meta("ttl")
        return float(value) if value is not None else TTL

    def meta(self, key):
        """A value from the meta table (settings and bookkeeping), or None."""
        row = self._row("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else None

    def set_meta(self, key, value):
        """Store a meta value; None removes it."""
        if value is None:
            self._write(("DELETE FROM meta WHERE key=?", (key,)))
        else:
            self._write(("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value))))

    def _row(self, sql, args):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def _write(self, *statements):
        with self.lock:
            for sql, args in statements:
                self.conn.execute(sql, args)
            self.conn.commit()

    def _search(self, query):
        key = normalize(query)
        row = self._row("SELECT results FROM searches WHERE query=?", (key,))
        if row:
            return [tuple(r[:2]) for r in json.loads(row[0])]
        results = self.source.search(query)
        self._write(("INSERT OR REPLACE INTO searches VALUES (?,?,?)", (key, json.dumps(results), self.clock())))
        return results

    def _fetch_item(self, item_id):
        item, info = self.source.item(item_id)
        self._write(
            ("INSERT OR REPLACE INTO items VALUES (?,?,?,?)", (item.id, item.name, item.price, self.clock())),
            ("INSERT OR REPLACE INTO item_info VALUES (?,?)", (item.id, json.dumps(info))),
        )
        return item, info

    def _item(self, item_id):
        row = self._row("SELECT id, name, price FROM items WHERE id=?", (item_id,))
        return Item(*row) if row else self._fetch_item(item_id)[0]

    def info(self, item_id):
        """Item type, requirements, base stats and icon key. These don't change: cached forever."""
        row = self._row("SELECT data FROM item_info WHERE item_id=?", (item_id,))
        info = json.loads(row[0]) if row else None
        if info is None or "iconKey" not in info:  # also upgrades entries cached before icon keys
            info = self._fetch_item(item_id)[1]
        return info

    def market(self, item_id):
        row = self._row("SELECT data FROM market_v2 WHERE item_id=? AND fetched_at>?",
                        (item_id, self.clock() - self.ttl()))
        if row:
            return Market(**json.loads(row[0]))
        m = self.source.market(item_id)
        self._write(("INSERT OR REPLACE INTO market_v2 VALUES (?,?,?)", (item_id, json.dumps(asdict(m)), self.clock())))
        return m

    def icon_png(self, item_id):
        """The item's icon from MeowDB as PNG bytes: from the pack bundled with MaplePrice, or
        (an item newer than the build) downloaded once and cached forever."""
        row = self._row("SELECT png FROM icons WHERE item_id=?", (item_id,))
        if row:
            return row[0]
        key = self.info(item_id)["iconKey"]
        png = self.icons.png(key)
        if png:
            return png
        png = self.source.icon_png(key)
        self._write(("INSERT OR REPLACE INTO icons VALUES (?,?)", (item_id, png)))
        return png

    def stored_icon(self, item_id):
        """The item's icon if it's on this PC (bundled, or downloaded before), else None. Never
        downloads anything: for the shop panel, which mustn't wait."""
        row = self._row("SELECT png FROM icons WHERE item_id=?", (item_id,))
        if row:
            return row[0]
        row = self._row("SELECT data FROM item_info WHERE item_id=?", (item_id,))
        info = json.loads(row[0]) if row else None
        return self.icons.png(info.get("iconKey")) if isinstance(info, dict) else None

    def icon_color(self, item_id):
        """Average icon colour (see icons.feature). Cached forever."""
        row = self._row("SELECT r, g, b FROM icon_colors WHERE item_id=?", (item_id,))
        if row:
            return None if row[0] is None else row
        color = feature(Image.open(io.BytesIO(self.icon_png(item_id))))
        self._write(("INSERT OR REPLACE INTO icon_colors VALUES (?,?,?,?)", (item_id, *(color or (None,) * 3))))
        return color

    def record_seen(self, item_id, unit_price):
        """Remember a shop listing price. Re-scanning the same price just refreshes it."""
        now = self.clock()
        self._write(("DELETE FROM seen WHERE seen_at<=?", (now - self.ttl(),)),
                    ("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (item_id, unit_price, now)))

    def seen(self, item_id):
        row = self._row("SELECT MIN(unit_price), MAX(unit_price), COUNT(*) FROM seen WHERE item_id=? AND seen_at>?",
                        (item_id, self.clock() - self.ttl()))
        return Seen(*row) if row[2] else None

    def lookup(self, ocr_name):
        base, truncated = strip_truncation(ocr_name.strip())
        match = Match(ocr_name)
        if len(base) < 3:
            return match

        q = normalize(base)
        candidates = self._search(base)
        if not candidates:
            # OCR typo can break the server-side search; retry on the first words.
            words = base.split()
            if len(words) > 2:
                candidates = self._search(" ".join(words[:2]))

        exact, scored = [], []
        for item_id, name in candidates:
            n = normalize(name)
            full = fuzz.ratio(q, n)
            # The shop cuts long names off, and OCR sometimes drops the "...",
            # so a name the OCR text is a prefix of counts as a match too.
            prefix = fuzz.ratio(q, n[: len(q)]) if len(q) >= MIN_PREFIX and len(n) > len(q) else 0
            if not truncated and full >= EXACT_SCORE:
                exact.append(item_id)
            score = max(full, prefix)
            if score >= MIN_SCORE:
                scored.append((score, item_id))
        if not exact and scored:
            best = max(s for s, _ in scored)
            scored = [i for s, i in scored if s >= best - TIE_MARGIN]

        match.items = list(self.mapper(self._item, exact or scored))
        match.items.sort(key=lambda i: i.name)
        return match
