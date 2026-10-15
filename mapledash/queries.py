"""Analytics over MaplePrice's market log (read-only), plus the Settings tab's
few writes: the price TTL and wiping the database.

Only confirmed sightings (the OCR was sure of the price) with a known item count
towards prices. Prices are per piece ("unit"). Sightings imported from before
the log existed have qty 0 (stack size unknown): they show in price history but
not in deals or flips, which need to know it's a single item. Sold rows
(greyed out in the shop) aren't listings: they're kept apart as sales, never
counted in listing counts, prices, deals or supply. FM prices come from MeowDB via
MaplePrice's cache, so they're as fresh as the last time you scanned that item.
"""
import json
import sqlite3
import statistics
import time
from urllib.parse import quote

from mapleprice.iconpack import BUNDLED

DAY = 24 * 3600
DEAL = 0.8             # listed at <= 80% of the FM price
DEAL_MIN_REPORTS = 2   # an FM price from a single report is too shaky to call deals on
FLIP_MIN_SIGHTINGS = 4
FLIP_MIN_SPREAD = 0.15
CHEAP = 0.9            # a shop's listing counts as cheap at <= 90% of FM
DEFAULT_TTL = DAY      # mapleprice.data.TTL: FM / seen-price expiry until changed here
TTL_RANGE = (1, 30 * 24)  # hours


def percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def median(values):
    return statistics.median(values) if values else None


class Market:
    def __init__(self, path, clock=time.time, icons=None):
        self.path = path
        self.icons = icons if icons is not None else BUNDLED  # every item's icon, shipped with MaplePrice
        self.uri = quote(str(path).replace("\\", "/"), safe="/:")  # e.g. a username with spaces or '#'
        self.clock = clock

    def _conn(self):
        conn = sqlite3.connect(f"file:{self.uri}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- shared lookups ---------------------------------------------------

    def _meta(self, conn, item_ids):
        """{item_id: {name, npc, fm, fm_reports, fm_at}} from MaplePrice's caches."""
        ids = list(set(item_ids))
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        meta = {r["id"]: {"id": r["id"], "name": r["name"], "npc": r["price"], "fm": None,
                          "fm_reports": 0, "fm_at": None}
                for r in conn.execute(f"SELECT id, name, price FROM items WHERE id IN ({marks})", ids)}
        for r in conn.execute(f"SELECT item_id, data, fetched_at FROM market_v2 WHERE item_id IN ({marks})", ids):
            if r["item_id"] in meta:
                d = json.loads(r["data"])
                meta[r["item_id"]].update(fm=d.get("usual"), fm_reports=d.get("reports") or 0, fm_at=r["fetched_at"])
        return meta

    def _sightings(self, conn, since=0, item_id=None, shop=None):
        # `sold` and `map_name` arrived in later MaplePrices; a database it hasn't upgraded yet has neither
        sold = "s.sold" if any(r["name"] == "sold" for r in conn.execute("PRAGMA table_info(sightings)")) else "0"
        map_name = "c.map_name" if any(r["name"] == "map_name" for r in conn.execute("PRAGMA table_info(scans)")) else "NULL"
        sql = f"""SELECT s.id, s.at, s.item_id, s.ocr_name, s.qty, s.price, s.unit_price, s.confirmed, {sold} AS sold,
                        c.shop_title, c.seller, COALESCE(c.seller, c.shop_title) AS shop,
                        c.channel, c.fm_room, {map_name} AS map_name
                 FROM sightings s LEFT JOIN scans c ON c.id = s.scan_id
                 WHERE s.at >= ? AND s.item_id IS NOT NULL"""
        args = [since]
        if item_id is not None:
            sql += " AND s.item_id = ?"
            args.append(item_id)
        if shop is not None:
            sql += " AND COALESCE(c.seller, c.shop_title) = ?"
            args.append(shop)
        return [dict(r) for r in conn.execute(sql + " ORDER BY s.at DESC", args)]

    @staticmethod
    def _priced(rows):
        return [r for r in rows if r["confirmed"] and r["unit_price"] is not None]

    @staticmethod
    def _split(rows):
        """(listings still for sale, sales)."""
        return [r for r in rows if not r["sold"]], [r for r in rows if r["sold"]]

    # ---- tabs -------------------------------------------------------------

    def overview(self):
        now = self.clock()
        with self._conn() as conn:
            everything = self._sightings(conn)
            meta = self._meta(conn, [r["item_id"] for r in everything])
            scans = conn.execute("SELECT COUNT(*) FROM scans WHERE at >= ?", (now - DAY,)).fetchone()[0]
        rows, sales = self._split(everything)
        priced = self._priced(rows)

        day = [r for r in rows if r["at"] >= now - DAY]
        hours = {}
        for r in everything:
            if r["at"] >= now - 2 * DAY:
                h = int(r["at"] // 3600 * 3600)
                hours[h] = hours.get(h, 0) + 1
        start = int((now - 2 * DAY) // 3600 * 3600)
        activity = [{"t": h, "n": hours.get(h, 0)} for h in range(start, int(now) + 1, 3600)]

        week = [r for r in priced if r["at"] >= now - 7 * DAY]
        by_item = {}
        for r in week:
            by_item.setdefault(r["item_id"], []).append(r["unit_price"])
        most = sorted(by_item.items(), key=lambda kv: -len(kv[1]))[:12]
        most_listed = [dict(meta[i], count=len(v), median=median(v), low=min(v)) for i, v in most if i in meta]

        movers = []
        for item_id in {r["item_id"] for r in priced}:
            recent = [r["unit_price"] for r in priced if r["item_id"] == item_id and r["at"] >= now - DAY]
            before = [r["unit_price"] for r in priced
                      if r["item_id"] == item_id and now - 7 * DAY <= r["at"] < now - DAY]
            if len(recent) >= 2 and len(before) >= 2 and item_id in meta:
                was, is_ = median(before), median(recent)
                if was:
                    movers.append(dict(meta[item_id], was=was, now=is_, change=is_ / was - 1))
        movers.sort(key=lambda m: m["change"])

        return {
            "kpis": {
                "sightings_24h": len(day),
                "sightings_total": len(rows),
                "sales_24h": sum(r["at"] >= now - DAY for r in sales),
                "items_tracked": len({r["item_id"] for r in everything}),
                "shops": len({r["shop"] for r in everything if r["shop"]}),
                "scans_24h": scans,
                "deals_now": len(self._under_fm(day, meta)),
            },
            "activity": activity,
            "most_listed": most_listed,
            "fallers": movers[:8],
            "risers": movers[::-1][:8],
            "recent_sales": [dict(meta[r["item_id"]], at=r["at"], qty=r["qty"], price=r["price"],
                                  unit_price=r["unit_price"], shop=r["shop"], shop_title=r["shop_title"],
                                  channel=r["channel"], fm_room=r["fm_room"], map_name=r["map_name"])
                             for r in sales if r["at"] >= now - 7 * DAY and r["item_id"] in meta][:50],
        }

    def _under_fm(self, rows, meta):
        out, seen = [], set()
        for r in self._priced(rows):
            m = meta.get(r["item_id"])
            if not m or not m["fm"] or m["fm_reports"] < DEAL_MIN_REPORTS or r["qty"] != 1:  # FM of stacks is ambiguous
                continue
            ratio = r["unit_price"] / m["fm"]
            key = (r["item_id"], r["shop"], r["price"])
            if ratio <= DEAL and key not in seen:
                seen.add(key)
                out.append(dict(m, at=r["at"], price=r["price"], qty=r["qty"], shop=r["shop"],
                                shop_title=r["shop_title"], channel=r["channel"], fm_room=r["fm_room"], map_name=r["map_name"],
                                under=1 - ratio))
        return sorted(out, key=lambda d: -d["under"])

    def deals(self):
        now = self.clock()
        with self._conn() as conn:
            day = self._split(self._sightings(conn, since=now - DAY))[0]
            everything = self._priced(self._split(self._sightings(conn))[0])
            meta = self._meta(conn, [r["item_id"] for r in everything] + [r["item_id"] for r in day])

        npc, seen = [], set()
        for r in self._priced(day):
            m = meta.get(r["item_id"])
            key = (r["item_id"], r["shop"], r["price"], r["qty"])
            if m and m["npc"] and r["qty"] >= 1 and r["unit_price"] < m["npc"] and key not in seen:
                seen.add(key)
                npc.append(dict(m, at=r["at"], price=r["price"], qty=r["qty"], shop=r["shop"],
                                shop_title=r["shop_title"], channel=r["channel"], fm_room=r["fm_room"], map_name=r["map_name"],
                                profit=(m["npc"] - r["unit_price"]) * r["qty"]))
        npc.sort(key=lambda d: -d["profit"])

        flips = []
        by_item = {}
        for r in everything:
            if r["qty"] == 1:  # compared with FM prices, which are per single item
                by_item.setdefault(r["item_id"], []).append(r["unit_price"])
        for item_id, units in by_item.items():
            m = meta.get(item_id)
            if not m or len(units) < FLIP_MIN_SIGHTINGS:
                continue
            buy = percentile(units, 0.25)
            trusted_fm = m["fm"] if m["fm"] and m["fm_reports"] >= 2 else None
            sell = trusted_fm or median(units)
            if buy and sell and sell / buy - 1 >= FLIP_MIN_SPREAD:
                flips.append(dict(m, buy=buy, sell=sell, sell_basis="FM" if trusted_fm else "your median",
                                  spread=sell / buy - 1, sightings=len(units)))
        flips.sort(key=lambda d: -d["spread"])

        return {"under_fm": self._under_fm(day, meta)[:50], "npc": npc[:50], "flips": flips[:50]}

    def items(self, q=""):
        with self._conn() as conn:
            rows = self._sightings(conn)
            meta = self._meta(conn, [r["item_id"] for r in rows])
        by_item = {}
        for r in rows:
            by_item.setdefault(r["item_id"], []).append(r)
        q = q.strip().lower()
        out = []
        for item_id, rs in by_item.items():
            m = meta.get(item_id)
            if not m or (q and q not in m["name"].lower()):
                continue
            listed, sales = self._split(rs)
            units = [r["unit_price"] for r in self._priced(listed)]
            out.append(dict(m, count=len(listed), sold=len(sales), last_at=rs[0]["at"], median=median(units),
                            low=min(units) if units else None))
        return sorted(out, key=lambda d: -d["last_at"])[:300]

    def item(self, item_id):
        with self._conn() as conn:
            rows = self._sightings(conn, item_id=item_id)
            meta = self._meta(conn, [item_id]).get(item_id)
            info = conn.execute("SELECT data FROM item_info WHERE item_id=?", (item_id,)).fetchone()
        if not meta:
            return None
        listed, sold = self._split(rows)
        units = [r["unit_price"] for r in self._priced(listed)]
        sales = [r["unit_price"] for r in self._priced(sold)]
        stats = {"count": len(units), "low": min(units) if units else None, "p25": percentile(units, 0.25),
                 "median": median(units), "high": max(units) if units else None,
                 "last": units[0] if units else None,
                 "sold": len(sold), "last_sold": sales[0] if sales else None,
                 "sold_median": median(sales)}
        return dict(meta, info=json.loads(info["data"]) if info else None, stats=stats,
                    sightings=[{k: r[k] for k in ("at", "qty", "price", "unit_price", "confirmed", "sold", "shop",
                                                  "shop_title", "channel", "fm_room", "map_name")}
                               for r in rows])

    def shops(self):
        with self._conn() as conn:
            rows = self._sightings(conn)
            meta = self._meta(conn, [r["item_id"] for r in rows])
            scans = {r["shop"]: r["n"] for r in conn.execute(
                "SELECT COALESCE(seller, shop_title) AS shop, COUNT(*) AS n FROM scans GROUP BY shop")}
        by_shop = {}
        for r in rows:
            if r["shop"]:
                by_shop.setdefault(r["shop"], []).append(r)
        out = []
        for shop, every in by_shop.items():
            rs, sales = self._split(every)
            vs_fm = [r["unit_price"] / meta[r["item_id"]]["fm"] for r in self._priced(rs)
                     if r["qty"] == 1 and meta.get(r["item_id"], {}).get("fm")]
            out.append({
                "shop": shop, "seller": every[0]["seller"], "title": every[0]["shop_title"],
                "channel": every[0]["channel"], "fm_room": every[0]["fm_room"],  # where it was last seen
                "map_name": every[0]["map_name"],
                "scans": scans.get(shop, 0), "listings": len(rs), "sold": len(sales),
                "items": len({r["item_id"] for r in every}),
                "first_at": every[-1]["at"], "last_at": every[0]["at"],
                "cheap_share": sum(v <= CHEAP for v in vs_fm) / len(vs_fm) if vs_fm else None,
                "vs_fm": median(vs_fm) - 1 if vs_fm else None, "compared": len(vs_fm),
            })
        return sorted(out, key=lambda d: -d["last_at"])

    def shop(self, shop):
        with self._conn() as conn:
            rows = self._sightings(conn, shop=shop)
            meta = self._meta(conn, [r["item_id"] for r in rows])
        listings = []
        for r in rows:
            m = meta.get(r["item_id"], {})
            fm = m.get("fm")
            listings.append(dict(id=r["item_id"], name=m.get("name", r["ocr_name"]), at=r["at"], qty=r["qty"],
                                 channel=r["channel"], fm_room=r["fm_room"], map_name=r["map_name"],
                                 price=r["price"], unit_price=r["unit_price"], confirmed=r["confirmed"],
                                 sold=r["sold"], fm=fm,
                                 vs_fm=(r["unit_price"] / fm - 1) if fm and r["unit_price"] and r["qty"] == 1 else None))
        return {"shop": shop, "title": rows[0]["shop_title"] if rows else None,
                "seller": rows[0]["seller"] if rows else None,
                "channel": rows[0]["channel"] if rows else None, "fm_room": rows[0]["fm_room"] if rows else None,
                "map_name": rows[0]["map_name"] if rows else None,
                "listings": listings}

    # ---- settings (the only writes; MaplePrice may be writing too) ----------

    def _rw(self):
        return sqlite3.connect(f"file:{self.uri}?mode=rw", uri=True, timeout=10)

    def settings(self):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='ttl'").fetchone()
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("scans", "sightings", "items", "market_v2")}
        ttl = float(row[0]) if row else DEFAULT_TTL
        return {"ttl_hours": ttl / 3600, "ttl_range": TTL_RANGE, "counts": counts}

    def set_ttl(self, hours):
        hours = float(hours)
        if not TTL_RANGE[0] <= hours <= TTL_RANGE[1]:
            raise ValueError(f"TTL must be {TTL_RANGE[0]}-{TTL_RANGE[1]} hours")
        conn = self._rw()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('ttl', ?)", (str(hours * 3600),))
        finally:
            conn.close()
        return self.settings()

    def clear(self):
        """Delete every scan, sighting and cached MeowDB lookup. Keeps the TTL
        setting. MaplePrice can stay running: it refetches whatever it needs."""
        conn = self._rw()
        try:
            with conn:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'meta'")]
                for t in tables:
                    conn.execute(f'DELETE FROM "{t}"')
                conn.execute("DELETE FROM sqlite_sequence")  # scan ids restart at 1
                conn.execute("DELETE FROM meta WHERE key NOT IN ('ttl', 'imported_seen')")
            try:
                conn.execute("VACUUM")  # give the disk space back; skipped if MaplePrice is mid-write
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
        return self.settings()

    def icon(self, item_id):
        """The clean icon (downloaded, or bundled with MaplePrice) before the shop screenshot's crop,
        which has the stack count drawn over it and is sometimes blank."""
        with self._conn() as conn:
            row = conn.execute("SELECT png FROM icons WHERE item_id=?", (item_id,)).fetchone()
            if row:
                return row[0]
            row = conn.execute("SELECT data FROM item_info WHERE item_id=?", (item_id,)).fetchone()
            info = json.loads(row[0]) if row else None
            png = self.icons.png(info.get("iconKey")) if isinstance(info, dict) else None
            if png:
                return png
            row = conn.execute("SELECT png FROM shop_icons WHERE item_id=?", (item_id,)).fetchone()
        return row[0] if row else None
