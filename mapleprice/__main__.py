"""MapleStory Classic shop price overlay, with the MapleDash market dashboard
served from the same process (open it from the F10 window).

    python -m mapleprice                 # shops scan as they open (or F8), F9 = clear, F10 = options,
                                         # F7 = price item tooltips as you hover (inventory, NPC shops)
    python -m mapleprice --image shot.png --out annotated.png   # offline test
"""
import argparse
import ctypes
import ctypes.wintypes
import logging
import logging.handlers
import os
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psutil

from . import ocr
from .channel import read_channel
from .annotate import Panel, Version, make_card, make_label, make_price_card, tier
from .render import PilPainter, draw_badge, draw_labels, draw_panel
from .data import DEFAULT_DB, ItemDB
from .icons import pick
from .ocr import read_lines
from .settings import HOTKEYS, Settings, hotkey_name
from .shop import merge_attempts, parse_listings
from .version import VERSION

log = logging.getLogger("mapleprice")


def rss():
    """This process's memory in MB, for the log."""
    return psutil.Process().memory_info().rss // 2**20

def vk_code(key):
    key = key.upper()
    if key.startswith("F") and key[1:].isdigit():
        return 0x6F + int(key[1:])
    if len(key) == 1:
        return ord(key)
    raise SystemExit(f"Unsupported hotkey {key!r}; use F1-F24 or a single letter/digit")


STOP_FILE = DEFAULT_DB.parent / "stop"  # touch to quit (an admin instance can't be killed otherwise)
DASHBOARD_PORT = 8765


def start_dashboard(port=DASHBOARD_PORT):
    """Serve MapleDash on a background thread. Returns its URL, or None if it
    couldn't start (the overlay works without it)."""
    try:
        from mapledash.queries import Market
        from mapledash.server import serve
        server = serve(Market(DEFAULT_DB), port)
    except Exception:
        log.exception("couldn't start the dashboard")
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def open_in_browser(url):
    """Via Explorer, so the browser doesn't inherit our admin rights."""
    try:
        subprocess.Popen(["explorer.exe", url])
    except Exception:
        log.exception("couldn't open %s", url)


_instance_mutex = None


def already_running():
    """One overlay at a time: a second copy would draw over the first and fight for hotkeys."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\MaplePrice-overlay")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def optional(lookup, item_id):
    """Extras for the detail panel (info, icon) never fail a scan."""
    try:
        return lookup(item_id)
    except Exception:
        log.exception("%s failed for item %s", lookup.__name__, item_id)
        return None


def info_or_none(db, item_id):
    return optional(db.info, item_id)


def market_or_none(db, item_id):
    try:
        return db.market(item_id)
    except Exception:  # FM data is a nice-to-have; never fail the scan over it
        log.exception("FM lookup failed for item %s", item_id)
        return None


MAX_ATTEMPTS = 2
DEBUG_IMAGE = DEFAULT_DB.parent / "last_scan.png"


def save_debug_image(img):
    try:
        img.save(DEBUG_IMAGE)
    except Exception:  # only a debugging aid
        log.exception("couldn't save %s", DEBUG_IMAGE)


def read_shop(grab):
    """OCR the shop, and once more (fresh screenshot, upscaled) if any row's
    price wasn't read confidently. A shaky single read can be wrong (e.g.
    "1,499" -> "11,499"), so those only count once a second read agrees.
    None when there's no shop window on screen."""
    attempts = []
    for attempt in range(MAX_ATTEMPTS):
        img = grab()
        if attempt == 0:
            save_debug_image(img)
        lines = read_lines(img, attempt)
        if lines is None:
            log.info("no shop window found")
            return None
        attempts.append(parse_listings(lines))
        rows = merge_attempts(attempts, img)
        confirmed = sum(r.confirmed for r in rows)
        log.info("OCR attempt %d (memory %d MB): %d listings, %d rows so far (%d confirmed, %d sold): %s",
                 attempt + 1, rss(), len(attempts[-1]), len(rows), confirmed, sum(r.sold for r in rows),
                 [(l.name, l.qty, l.price) for l in attempts[-1]])
        # A cut-off price ("me...") won't read any better a second time.
        if rows and all(r.confirmed or r.uncertain for r in rows):
            break
    return rows


# Rows are looked up in parallel (ROWS), and each row fans its own requests out
# (FETCH). Separate pools, so a row waiting on its fetches can't starve them.
ROWS = ThreadPoolExecutor(max_workers=6, thread_name_prefix="row")
FETCH = ThreadPoolExecutor(max_workers=12, thread_name_prefix="fetch")


def narrow_by_icon(db, row, match):
    """Cut-off names (scroll tiers etc.) match several items; pick the one whose
    icon colour matches the icon in the shop row, if it's a clear winner."""
    try:
        colors = list(FETCH.map(db.icon_color, [i.id for i in match.items]))
    except Exception:
        log.exception("icon lookup failed")
        return
    idx = pick(row.icon_color, colors)
    log.info("icon match for %s: %s", row.names[0],
             match.items[idx].name if idx is not None else "ambiguous")
    if idx is not None:
        match.items = [match.items[idx]]


def versions_of(db, items):
    markets = FETCH.map(lambda i: market_or_none(db, i.id), items)
    return [Version(i, m) for i, m in zip(items, markets)]


def look_up(db, row):
    """Everything about one shop row: (listing, match, versions, seen, siblings, info)."""
    # Try each spelling the OCR produced until one matches an item.
    name = row.names[0]
    match = db.lookup(name)
    for alt in row.names[1:]:
        if match.found:
            break
        name, match = alt, db.lookup(alt)
    if not match.found:
        log.info("no item matched any reading of row: %s", row.names)
    if len(match.items) > 1:
        narrow_by_icon(db, row, match)
    listing = row.listing(name)
    seen, siblings, info = None, [], None
    if len(match.items) == 1:  # only track prices when we know exactly which item it is
        item = match.items[0]
        info_job = FETCH.submit(info_or_none, db, item.id)
        if row.confirmed:  # never let a one-off misread into the 24h history
            db.record_seen(item.id, listing.price / listing.qty)
            seen = db.seen(item.id)
        if tier(item.name):
            others = db.lookup(item.name.rsplit(": ", 1)[0] + ": ...").items
            siblings = versions_of(db, [i for i in others if i.id != item.id])
        info = info_job.result()
    return listing, match, versions_of(db, match.items), seen, siblings, info


def panel_box(rows):
    """Where the detail panel goes: right of the shop window, from its top edge
    (sliding up if it needs room) down to its bottom edge."""
    box = ocr.last_list_box
    if box:  # shop window edges relative to its item list
        return dict(left=box[0] + 529, top=box[1] - 136, right=10_000, bottom=box[3] + 64)
    return dict(left=rows[0].x + 485, top=rows[0].y - 145, right=10_000, bottom=rows[-1].bottom + 70)


last_map = None  # map name from the last minimap that showed one: only for the F10 hint
last_channel = None  # channel from the last time the game was connected to one


def read_location(grab):
    """(shop title, seller, your character's name) for the current screen. One job,
    so the OCR engine is never used from two threads at once."""
    try:
        img = grab()
        title, seller = ocr.read_header(img)
    except Exception:
        log.exception("couldn't read the shop's title/seller")
        return None, None, None
    try:
        player = ocr.read_player(img)
    except Exception:
        log.exception("couldn't read your character's name")
        player = None
    return title, seller, player


def read_map(grab):
    try:
        map_name = ocr.read_map(grab())
        log.info("minimap: %r (memory %d MB)", map_name, rss())
        return map_name
    except Exception:
        log.exception("couldn't read the minimap")
        return None


def where(settings, detected_map, detected_channel=None):
    """(channel, FM room, map name): a manual setting wins, then what was detected.
    The channel falls back to the last one seen (the connection only drops while
    changing channel), but the map never does: with the minimap hidden you may
    have moved, so it's unknown. FM room is None outside the Free Market."""
    global last_map, last_channel
    if detected_map is not None:
        last_map = detected_map
    if detected_channel is not None:
        last_channel = detected_channel
    channel = settings.channel if settings.channel is not None else last_channel
    if settings.fm_room is not None:
        return channel, settings.fm_room, f"Free Market <{settings.fm_room}>"
    return channel, ocr.fm_room_of(detected_map), detected_map


def where_text(channel, room, map_name=None):
    place = f"FM {room}" if room else map_name or "Map ?"
    return f"Ch {channel or '?'} \u00b7 {place}"


def blocked_reason(channel, room, map_name):
    """Why we won't scan here, or None. A shop logged without its channel and
    map can't be found again, and a guessed one would be false data."""
    if map_name is None:
        return "Please expand the minimap"
    if channel is None:
        return "Channel unknown: set it in F10"
    return None


def log_scan(db, title, seller, rows, results, channel=None, room=None, map_name=None, player=None):
    """Add this scan to the permanent market log (what MapleDash shows)."""
    entries = []
    for row, (listing, match, *_rest) in zip(rows, results):
        item_id = match.items[0].id if len(match.items) == 1 else None
        entries.append((item_id, listing.name, listing.qty, listing.price, row.confirmed, row.icon, row.sold))
    try:
        db.record_scan(title, seller, entries, channel, room, map_name, scanned_by=player)
        log.info("logged scan of %r by %r at %s, scanned by %r: %d listings", title, seller,
                 where_text(channel, room, map_name), player, len(entries))
    except Exception:
        log.exception("couldn't log the scan")


def scan(grab, db, settings=None):
    """OCR the shop, then look everything up.
    Returns (row labels, detail panel, "Ch 5 · FM 2" or "Ch 5 · Henesys Market",
    why it didn't scan or None). Nothing is read or logged until we know where we are."""
    settings = settings or Settings()
    detected_map = read_map(grab) if settings.fm_room is None else None  # a manual room needs no minimap
    place = where(settings, detected_map, read_channel())
    blocked = blocked_reason(*place)
    if blocked:
        log.info("not scanning at %s: %s", where_text(*place), blocked)
        return [], None, where_text(*place), blocked
    rows = read_shop(grab)  # all OCR finishes before any MeowDB call
    if rows is None:
        return [], None, where_text(*place), "No shop window found"
    t = time.perf_counter()
    location = FETCH.submit(read_location, grab) if rows else None  # runs while lookups wait on the network
    results = list(ROWS.map(lambda r: look_up(db, r), rows))
    log.info("lookups took %.2fs (memory %d MB)", time.perf_counter() - t, rss())
    if rows:
        title, seller, player = location.result()
        log_scan(db, title, seller, rows, results, *place, player=player)

    labels = []
    panel = Panel(**panel_box(rows)) if rows else None
    for row, (listing, match, versions, seen, siblings, info) in zip(rows, results):
        labels.append(make_label(listing, match, versions, seen, siblings))
        if row.sold:  # can't buy it: no card, the label says SOLD
            continue
        card = make_card(listing, match, versions, seen, info, siblings)
        same = next((c for c in panel.cards if c.key() == card.key()), None)
        if same:  # e.g. two identical stacks of arrows: one card, "x2"
            same.count += 1
            continue
        # The clean icon (bundled with MaplePrice) once we know the item; else the shop's own,
        # cropped from the screenshot. Neither is a download.
        known = match.items[0].id if len(match.items) == 1 else None
        card.icon = (db.stored_icon(known) if known else None) or row.icon
        panel.cards.append(card)
    return labels, panel, where_text(*place), None


def price_card(db, names):
    """The hover card for an item tooltip's name (OCR readings, best first)."""
    name, match = names[0], db.lookup(names[0])
    for alt in names[1:]:
        if match.found:
            break
        name, match = alt, db.lookup(alt)
    log.info("hovered %s -> %s", names, [i.name for i in match.items])
    info, siblings = None, []
    if match.found:
        item = match.items[0]
        info = optional(db.info, item.id)
        if tier(item.name):
            others = db.lookup(item.name.rsplit(": ", 1)[0] + ": ...").items
            siblings = versions_of(db, [i for i in others if i.id != item.id])
    card = make_price_card(name, match, versions_of(db, match.items), info, siblings)
    if match.found:
        card.icon = optional(db.icon_png, match.items[0].id)
    return card


HOVER_POLL = 0.08    # s between cursor checks
HOVER_SETTLE = 0.15  # the cursor rests this long before we look for a tooltip
HOVER_RETRY = 1.0    # keep looking this long after it stops: the tooltip can lag
HOVER_JITTER = 6     # px the cursor can drift before the card is hidden


def cursor_pos():
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def watch_hover(current_rect, db, events, enabled):
    """While `enabled`, price the item tooltip under the resting cursor:
    ("hover", (card, dot, rect)) when one shows up, ("hover", None) once the
    cursor moves off it. Cards are kept, so going back to an item is instant."""
    from . import capture
    cards = {}
    pos, since, done, shown_at = None, 0.0, False, None
    while True:
        time.sleep(HOVER_POLL)
        if not enabled.is_set():
            pos, shown_at = None, None
            continue
        now, here = time.monotonic(), cursor_pos()
        if here != pos:
            pos, since, done = here, now, False
            if shown_at and max(abs(here[0] - shown_at[0]), abs(here[1] - shown_at[1])) > HOVER_JITTER:
                events.put(("hover", None))
                shown_at = None
            continue
        if done or now - since < HOVER_SETTLE:
            continue
        try:
            rect = current_rect()
            found = ocr.read_tooltip(capture.grab(rect), (pos[0] - rect[0], pos[1] - rect[1]))
            if not found:
                done = now - since > HOVER_RETRY
                continue
            done = True
            names, dot = found
            if names[0] not in cards:
                cards[names[0]] = price_card(db, names)
            events.put(("hover", (cards[names[0]], dot, rect)))
            shown_at = pos
        except Exception:
            log.exception("hover price check failed")
            done = True


class ShopWatch:
    """When to scan a shop by itself (debounced). A shop is scanned once its
    list has looked the same for STABLE seconds, so the opening animation or a
    scroll settles first, and again only when the list changes. It counts as
    closed once it's been gone for GONE seconds, so one bad look doesn't clear it."""
    STABLE = 0.5
    GONE = 0.8

    def __init__(self):
        self.anchor = None         # where the shop's buttons are
        self.prev = None           # list fingerprint at the last look
        self.since = 0.0           # when the list last changed
        self.scanned = None        # list fingerprint at the last scan
        self.missing_since = None  # when the shop was last seen gone

    def update(self, now, anchor, fingerprint):
        """One look at the screen (anchor None: no shop). "scan", "closed" or None."""
        if anchor is None:
            if self.anchor is None:
                return None
            self.missing_since = self.missing_since or now
            if now - self.missing_since < self.GONE:
                return None
            self.__init__()
            return "closed"
        self.missing_since = None
        if anchor != self.anchor or self.prev is None or ocr.list_changed(self.prev, fingerprint):
            self.since = now
        self.anchor, self.prev = anchor, fingerprint
        if now - self.since >= self.STABLE and (self.scanned is None or ocr.list_changed(self.scanned, fingerprint)):
            self.scanned = fingerprint
            return "scan"
        return None


SHOP_POLL = 0.3  # s between looks for a shop window


def watch_shop(current_rect, events, enabled, busy):
    """While `enabled()`, scan shops as they open or change: ("auto_scan", None),
    and ("shop_closed", None) once the shop's gone."""
    import numpy as np
    from . import capture
    watch = ShopWatch()
    while True:
        time.sleep(SHOP_POLL)
        if not enabled():
            watch = ShopWatch()
            continue
        if busy.is_set():  # a scan is reading the screen: don't judge it half-drawn
            continue
        try:
            rect = current_rect()
            rgb = np.array(capture.grab(rect))
            x, y = cursor_pos()
            cursor = x - rect[0], y - rect[1]
            anchor = ocr.find_shop(rgb, watch.anchor)
            if anchor and ocr.find_tooltip_dot(rgb, cursor, anywhere=False):
                continue  # a tooltip is over the list: wait until it's gone
            fingerprint = ocr.list_fingerprint(rgb, anchor, cursor) if anchor else None
            what = watch.update(time.monotonic(), anchor, fingerprint)
            if what == "scan":
                log.info("shop list settled at %s: auto scan", anchor)
                events.put(("auto_scan", None))
            elif what == "closed":
                events.put(("shop_closed", None))
        except Exception:
            log.exception("shop watch failed")
            time.sleep(2)


def foreground_title():
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
    return buf.value


def listen_hotkeys(bindings, events, paused=lambda key: False):
    """Poll key state instead of RegisterHotKey: games often suppress normal
    hotkeys (raw input with RIDEV_NOHOTKEYS) while they have focus.
    `bindings()` is read on every poll, so a rebound key works at once; a key
    doesn't fire while `paused(key)` (the options window is waiting for a new key)."""
    user32 = ctypes.windll.user32
    was_down = {}
    while True:
        for key, name in bindings():
            vk = vk_code(key)
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not was_down.get(vk, down) and not paused(key):  # first look at a key: just note it
                log.info("key %s pressed (foreground: %r)", name, foreground_title())
                events.put((name, None))
            was_down[vk] = down
        time.sleep(0.03)


def run_overlay(args, db):
    import tkinter as tk
    from . import capture
    from .overlay import OptionsWindow, Overlay

    root = tk.Tk()
    # Tk prints a drawing error to the console and carries on (an empty panel): log it too
    root.report_callback_exception = lambda kind, err, tb: log.error("overlay error", exc_info=(kind, err, tb))
    ov = Overlay(root)
    events = queue.Queue()
    busy = threading.Event()
    settings = Settings.load()
    for field, _ in HOTKEYS:  # --scan-key etc. override the saved keys
        if getattr(args, field):
            key = hotkey_name(getattr(args, field))
            if not key:
                raise SystemExit(f"Unsupported hotkey {getattr(args, field)!r}; use F1-F24 or a single letter/digit")
            setattr(settings, field, key)
    shown = {}  # last scan result, so toggling an option redraws it
    keys = lambda: f"{settings.clear_key} clear \u00b7 {settings.options_key} options"
    hovering = threading.Event()  # hover price checks are on

    def show_result():
        if "labels" not in shown:
            return
        ov.clear()
        if settings.show_labels:
            ov.draw(shown["labels"])
        if settings.show_panel and shown["panel"]:
            ov.draw_panel(shown["panel"])
        n = len(shown["labels"])
        if n:
            ov.status(f"{shown['where']}  \u00b7  {n} items  |  {keys()}")
        else:
            ov.status(f"{shown['where']}  \u00b7  no shop listings found  |  {keys()}", "warn")
        log.info("drawn (memory %d MB)", rss())

    def on_settings_change():
        settings.save()
        log.info("settings changed: %s", settings)
        show_result()

    dashboard = start_dashboard()
    log.info("dashboard at %s", dashboard)
    options = OptionsWindow(root, settings, on_settings_change, lambda: last_map, read_channel,
                            open_dashboard=(lambda: open_in_browser(dashboard)) if dashboard else None)

    def current_rect():
        hwnd = capture.find_window(args.title)
        return capture.client_rect(hwnd) if hwnd else capture.primary_monitor_rect()

    def worker(rect):
        try:
            labels, panel, place, blocked = scan(lambda: capture.grab(rect), db, settings)
            if blocked:
                events.put(("blocked", blocked))
                return
            log.info("scan found %d listings at %s (memory %d MB): %s", len(labels), place,
                     rss(), [l.text for l in labels])
            events.put(("result", (labels, panel, place)))
        except Exception as e:  # surface network/OCR failures on screen
            log.exception("scan failed")
            events.put(("error", f"Scan failed: {e}"))
        finally:
            busy.clear()

    def start_scan():
        if busy.is_set():
            return
        busy.set()
        ov.clear()  # don't OCR our own labels
        rect = current_rect()
        log.info("scanning window rect %s (memory %d MB)", rect, rss())
        ov.place(rect)
        ov.status("Scanning...")  # top-centre badge: clear of the minimap and the shop
        threading.Thread(target=worker, args=(rect,), daemon=True).start()

    def poll():
        if STOP_FILE.exists():
            STOP_FILE.unlink()
            log.info("stop file found, quitting")
            root.destroy()
            return
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "scan":
                    start_scan()
                elif kind == "auto_scan":
                    if settings.auto_scan:  # might have been switched off since
                        start_scan()
                elif kind == "shop_closed":
                    if shown:  # its prices would be left floating over the game
                        shown.clear()
                        ov.clear()
                elif kind == "clear":
                    shown.clear()
                    ov.clear()
                elif kind == "options":
                    options.toggle()
                elif kind == "hover_toggle":
                    if hovering.is_set():
                        hovering.clear()
                        ov.hover_card()
                        ov.flash("Hover price check off", "info", 1200)
                    else:
                        hovering.set()
                        ov.place(current_rect())
                        ov.flash(f"Hover price check on: rest the cursor on an item  \u00b7  {settings.hover_key} off",
                                 "info", 2500)
                elif kind == "hover":
                    if payload and hovering.is_set():
                        card, dot, rect = payload
                        if rect != ov.rect:  # the game window moved
                            ov.place(rect)
                        ov.hover_card(card, dot)
                    else:
                        ov.hover_card()
                elif kind == "result":
                    shown.update(zip(("labels", "panel", "where"), payload))
                    show_result()
                elif kind == "blocked":
                    shown.clear()
                    ov.clear()
                    ov.flash(payload, "warn")
                elif kind == "error":
                    ov.status(payload, "error")
        except queue.Empty:
            pass
        except Exception:  # e.g. a panel that failed to draw: log it, but keep scanning (and seeing the stop file)
            log.exception("overlay event failed")
        root.after(50, poll)

    bindings = lambda: [(settings.scan_key, "scan"), (settings.clear_key, "clear"), (settings.options_key, "options"),
                        (settings.hover_key, "hover_toggle")]
    paused = lambda key: options.capturing
    threading.Thread(target=listen_hotkeys, args=(bindings, events, paused), daemon=True).start()
    threading.Thread(target=watch_hover, args=(current_rect, db, events, hovering), daemon=True, name="hover").start()
    threading.Thread(target=watch_shop, args=(current_rect, events, lambda: settings.auto_scan, busy),
                     daemon=True, name="shop").start()
    threading.Thread(target=ocr.warm_up, daemon=True, name="ocr-warm-up").start()  # first scan as quick as the rest
    STOP_FILE.unlink(missing_ok=True)
    ov.place(current_rect())
    print(f"MaplePrice {VERSION} running. {settings.scan_key} = scan shop, {settings.hover_key} = hover price check on/off, "
          f"{settings.clear_key} = clear, {settings.options_key} = options "
          f"(channel, FM room, dashboard). Close this window to quit.")
    if dashboard:
        print(f"Market dashboard: {dashboard}")
        if settings.open_dashboard:
            open_in_browser(dashboard)
    if settings.show_options:
        options.toggle()
    poll()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


def run_image(args, db):
    from PIL import Image

    img = Image.open(args.image).convert("RGB")
    labels, panel, place, blocked = scan(lambda: img, db)
    print("location:", place)
    painter = PilPainter(img)  # draws exactly what the live overlay would
    if blocked:
        print("not scanned:", blocked)
        draw_badge(painter, img.width // 2, img.height // 2, blocked, "warn", font="big", center=True)
    else:
        draw_badge(painter, img.width // 2, 6, f"{place}  ·  {len(labels)} items")
    for lb in labels:
        print(f"{lb.text}  @ ({lb.right}, {lb.center_y})")
    draw_labels(painter, labels)
    if panel:
        draw_panel(painter, panel)
        for card in panel.cards:
            pills = "  ".join(f"[{p.text}]" for p in card.pills)
            print(f"  card: {card.title} | {card.subtitle} | {''.join(t for t, _ in card.price)} {pills}")
    if args.out:
        img.save(args.out)
        print(f"Saved {args.out}")


def main():
    p = argparse.ArgumentParser(prog="mapleprice", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--title", default="MapleStory", help="game window title (exact match)")
    p.add_argument("--scan-key", help="override the saved hotkey (default F8; rebind it in the options window)")
    p.add_argument("--clear-key", help="override the saved hotkey (default F9)")
    p.add_argument("--options-key", help="override the saved hotkey (default F10)")
    p.add_argument("--hover-key", help="override the saved hotkey (default F7)")
    p.add_argument("--image", help="annotate a saved screenshot instead of running the overlay")
    p.add_argument("--out", help="with --image: where to save the annotated copy")
    args = p.parse_args()

    log_path = DEFAULT_DB.parent / "log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        # log.txt, then log.txt.1 to .3 as it fills: never more than ~20 MB in all
        handlers=[logging.handlers.RotatingFileHandler(log_path, maxBytes=5 * 2**20, backupCount=3, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    log.info("start pid=%s admin=%s version=%s", os.getpid(), is_admin, VERSION)
    if not args.image and already_running():
        log.info("already running, exiting")
        ctypes.windll.user32.MessageBoxW(None, "MaplePrice is already running.\n\n"
                                         "Press F10 in game for options and the dashboard.", "MaplePrice", 0x40)
        return

    from .capture import make_dpi_aware
    make_dpi_aware()
    db = ItemDB(mapper=FETCH.map)
    if args.image:
        run_image(args, db)
    else:
        run_overlay(args, db)


if __name__ == "__main__":
    main()
