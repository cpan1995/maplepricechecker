"""Local OCR with RapidOCR (PaddleOCR models on onnxruntime, CPU only).

Benchmarked on saved shop screenshots against Windows' built-in OCR: it read
30/32 prices correctly vs 24/32, and in ~0.2s per scan vs ~1.5s, because we
only OCR the shop's item list: the shop window is found by template-matching
its BUY ITEM / LEAVE STORE buttons, and the list sits at a fixed offset.
"""
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
import cv2

from .shop import Line

log = logging.getLogger("mapleprice")

ASSETS = Path(__file__).resolve().parent / "assets"
# (image, its offset from the BUY ITEM button's top-left, match threshold), tried in order.
# Your own shop shows OPEN STORE / CLOSE STORE instead; only CLOSE STORE is used,
# since OPEN STORE greys out once the store is open. It half-matches a visitor's
# LEAVE STORE (~0.85), hence the stricter threshold and going second.
TEMPLATES = [
    (ASSETS / "shop_buttons.png", (0, 0), 0.8),        # someone else's shop: BUY ITEM / LEAVE STORE
    (ASSETS / "shop_owner_buttons.png", (0, 18), 0.95),  # your own: CLOSE STORE
]
# Regions relative to the top-left of the buttons template.
LIST_BOX = (-140, 56, 70, 304)     # item list: icons through scrollbar, 6 rows
TITLE_BOX = (-140, -96, 360, -64)  # the shop's name, in the box at the top
SELLER_BOX = (-140, 32, -36, 60)   # seller's name, under their character

_engine = None
_engine_lock = threading.Lock()  # the hover watcher and a shop scan can OCR at the same time
_templates = None
_half_templates = None
_templates_lock = threading.Lock()
last_list_box = None  # where read_lines last found the shop's item list


def _ocr():
    global _engine
    if _engine is None:
        # 4 threads is the sweet spot (all 20 cores was 2.5x slower); the
        # rotation classifier is pointless for horizontal UI text.
        _engine = RapidOCR(det_limit_side_len=960, det_limit_type="max",
                           intra_op_num_threads=4, inter_op_num_threads=1)
    return _engine


def warm_up():
    """Load the OCR models and run them once, on a made-up line of text. The first run takes
    ~1s longer than the rest; called in the background at startup, so the first shop scan
    doesn't pay for it (one that starts meanwhile waits on the lock, as it would anyway)."""
    strip = np.full((40, 260, 3), 255, np.uint8)
    cv2.putText(strip, "Warm up 1,234", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    t = time.perf_counter()
    try:
        _read(strip)
    except Exception:
        log.exception("OCR warm-up failed (the first scan just loads it instead)")
        return
    log.info("OCR ready in %.2fs", time.perf_counter() - t)


def _read(rgb):
    """OCR results [(quad, text, score), ...] for an RGB array, or None."""
    with _engine_lock:
        result, _ = _ocr()(rgb, use_cls=False)
    return result


def _half(rgb):
    return cv2.resize(rgb, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)


def _load_templates():
    global _templates, _half_templates
    if _templates is None:
        _templates = [(np.array(Image.open(path).convert("RGB")), offset, threshold)
                      for path, offset, threshold in TEMPLATES]
        _half_templates = [_half(t) for t, _, _ in _templates]


def locate_buttons(rgb):
    """Top-left of the shop's BUY ITEM / LEAVE STORE buttons (or where they'd
    be, in your own shop), or None if no shop is open."""
    _load_templates()
    with _templates_lock:  # the shop watcher and a scan both reorder the list
        for i, (template, (dx, dy), threshold) in enumerate(_templates):
            if rgb.shape[0] < template.shape[0] or rgb.shape[1] < template.shape[1]:
                continue
            _, score, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(rgb, template, cv2.TM_CCOEFF_NORMED))
            if score >= threshold:
                if i:  # e.g. your own shop: check its buttons first next time
                    _templates.insert(0, _templates.pop(i))
                return bx - dx, by - dy
    return None


SHOP_HINT = 0.62  # a half-size match this good is worth a full-size look (shops: >= 0.68, else ~0.53)
NEAR = 24         # px the shop may have moved since the last look


def find_shop(rgb, near=None):
    """locate_buttons, cheap enough to poll a few times a second: first right
    where the shop was (`near`, ~1ms), then a half-size pass over the screen
    (~20ms, vs 40-80ms full size) that only pays for a full search when
    something there looks like the buttons."""
    _load_templates()
    if near:
        x, y = near
        x0, y0 = max(0, x - NEAR), max(0, y - NEAR)
        w = max(dx + t.shape[1] for t, (dx, _), _ in _templates) + NEAR
        h = max(dy + t.shape[0] for t, (_, dy), _ in _templates) + NEAR
        found = locate_buttons(rgb[y0:y + h, x0:x + w])
        if found:
            return found[0] + x0, found[1] + y0
    small = _half(rgb)
    for template in _half_templates:
        if small.shape[0] >= template.shape[0] and small.shape[1] >= template.shape[1]:
            if cv2.minMaxLoc(cv2.matchTemplate(small, template, cv2.TM_CCOEFF_NORMED))[1] >= SHOP_HINT:
                return locate_buttons(rgb)
    return None


def _region(rgb, anchor, box):
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    return max(0, anchor[0] + x0), max(0, anchor[1] + y0), min(w, anchor[0] + x1), min(h, anchor[1] + y1)


LIST_ICONS = 45      # px of icons at the list's left: our labels can overlap them
CURSOR_BOX = (-12, -12, 36, 40)  # the game's cursor sprite around its tip
CHANGED_PX = 40      # grey levels: a pixel that moved more than this changed
CHANGED_SHARE = 0.005  # share of changed pixels that makes it a different list


def list_fingerprint(rgb, anchor, cursor=None):
    """A greyscale copy of the shop's item names and prices, to tell when the
    list changes (opened, scrolled, something sold). The icons are left out, and
    so is the game's cursor (NaN): neither changes what's for sale."""
    x0, y0, x1, y1 = _region(rgb, anchor, LIST_BOX)
    x0 += LIST_ICONS
    grey = cv2.cvtColor(np.ascontiguousarray(rgb[y0:y1, x0:x1]), cv2.COLOR_RGB2GRAY).astype(np.float32)
    if cursor:
        cx, cy = cursor[0] - x0, cursor[1] - y0
        bx0, by0, bx1, by1 = CURSOR_BOX
        grey[max(0, cy + by0):max(0, cy + by1), max(0, cx + bx0):max(0, cx + bx1)] = np.nan
    return grey


def list_changed(a, b):
    if a.shape != b.shape:  # the shop window moved off the screen's edge
        return True
    diff = np.abs(a - b)
    seen = ~np.isnan(diff)
    return not seen.any() or (diff[seen] > CHANGED_PX).mean() > CHANGED_SHARE


def locate_list(rgb):
    """(x0, y0, x1, y1) of the shop's item list in `rgb`, or None if no shop is open."""
    anchor = locate_buttons(rgb)
    return _region(rgb, anchor, LIST_BOX) if anchor else None


MINIMAP_BOX = (0, 0, 300, 120)  # the minimap's default spot, top-left of the game
MINIMAP_PANEL = (153, 187, 204)  # the light blue behind the minimap's street and map name
ROOM_RE = re.compile(r"Free\s*Market\s*<?\s*(\d{1,2})", re.IGNORECASE)


def _on_panel(rgb, box):
    """Is this OCR'd text sitting on the minimap's title panel? Shop signs,
    the window title and our own overlay are all on other colors."""
    xs, ys = [int(p[0]) for p in box], [int(p[1]) for p in box]
    px = rgb[max(0, min(ys)):max(ys), max(0, min(xs)):max(xs)].reshape(-1, 3).astype(int)
    return len(px) > 0 and (np.abs(px - MINIMAP_PANEL).max(axis=1) <= 6).mean() >= 0.35


def clean_map_name(name):
    """OCR spacing varies ("Free Market<8>", "FreeMarket <2>"); one spelling per
    map keeps them comparable."""
    room = ROOM_RE.search(name)
    if room:
        return f"Free Market <{int(room.group(1))}>"
    return re.sub(r"\s*<\s*", " <", " ".join(name.split()))


def read_map(img):
    """Map name from the minimap title, e.g. "Free Market <2>" or "Henesys Market"
    (the line under the street name). None when the minimap is collapsed or hidden."""
    rgb = np.array(img.convert("RGB"))
    x0, y0, x1, y1 = MINIMAP_BOX
    crop = rgb[y0:min(y1, rgb.shape[0]), x0:min(x1, rgb.shape[1])]
    # Only OCR the title panel's rows: the map picture below is busy (Kerning
    # City's took 0.8s to "read"), and no panel at all means it's collapsed.
    panel_rows = np.flatnonzero((np.abs(crop.astype(int) - MINIMAP_PANEL).max(axis=2) <= 6).mean(axis=1) >= 0.15)
    if not len(panel_rows):
        return None
    crop = crop[max(0, panel_rows[0] - 8):panel_rows[-1] + 9]  # a margin: tight crops drop spaces
    result = _read(crop)
    lines = sorted((min(p[1] for p in box), text) for box, text, _ in (result or []) if _on_panel(crop, box))
    return clean_map_name(lines[1][1]) if len(lines) >= 2 else None  # [0] is the street


def fm_room_of(map_name):
    """Free Market room number of a map name ("Free Market <2>" -> 2), else None."""
    m = ROOM_RE.search(map_name or "")
    return int(m.group(1)) if m else None


def read_header(img):
    """(shop title, seller name) of the open shop; either may be None."""
    rgb = np.array(img.convert("RGB"))
    anchor = locate_buttons(rgb)
    if not anchor:
        return None, None
    out = []
    for box in (TITLE_BOX, SELLER_BOX):
        x0, y0, x1, y1 = _region(rgb, anchor, box)
        if x1 - x0 < 8 or y1 - y0 < 8:
            out.append(None)
            continue
        result = _read(rgb[y0:y1, x0:x1])
        text = " ".join(t for _, t, score in (result or []) if score >= 0.6).strip()
        out.append(text or None)
    return tuple(out)


# Your character's name is on the status bar at the bottom, under your job, right of the
# "LV." label. The bar moves with the window size, so the label is found by template.
LEVEL_LABEL = ASSETS / "level_label.png"
PLAYER_BOX = (69, -4, 205, 28)  # job and name, from the label's top-left
STATUS_BAR = 120                # px at the bottom of the screen to look for the label in
NAME_RE = re.compile(r"[A-Za-z0-9]{2,13}")
_level_label = None


def read_player(img):
    """Your character's name off the status bar, or None if it isn't on screen or reads oddly."""
    global _level_label
    if _level_label is None:
        _level_label = np.array(Image.open(LEVEL_LABEL).convert("RGB"))
    rgb = np.array(img.convert("RGB"))
    top = max(0, rgb.shape[0] - STATUS_BAR)
    bar = rgb[top:]
    if bar.shape[0] < _level_label.shape[0] or bar.shape[1] < _level_label.shape[1]:
        return None
    _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(bar, _level_label, cv2.TM_CCOEFF_NORMED))
    if score < 0.8:
        return None
    x0, y0, x1, y1 = _region(rgb, (x, y + top), PLAYER_BOX)
    lines = sorted((min(p[1] for p in box), text.strip()) for box, text, score in (_read(rgb[y0:y1, x0:x1]) or [])
                   if score >= 0.8)
    if len(lines) < 2:  # job, then name: with only one line we can't tell which it is
        return None
    name = lines[-1][1]
    return name if NAME_RE.fullmatch(name) else None


def read_lines(img, attempt=0):
    """OCR text lines (image coordinates) from the shop list, or None when no
    shop window is found. (Never the whole screen: that took 5-15s and, upscaled
    on a retry, left the OCR engine holding ~800 MB.) Retries (attempt >= 1)
    upscale 2x to get a second, independent reading."""
    global last_list_box
    rgb = np.array(img.convert("RGB"))
    box = last_list_box = locate_list(rgb)
    if not box:
        return None
    ox, oy = box[0], box[1]
    region = rgb[box[1]:box[3], box[0]:box[2]]
    scale = 2 if attempt else 1
    if scale != 1:
        region = cv2.resize(region, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    result = _read(region)
    lines = []
    for quad, text, score in result or []:
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        lines.append(Line(
            text, round(min(xs) / scale) + ox, round(min(ys) / scale) + oy,
            round((max(xs) - min(xs)) / scale), round((max(ys) - min(ys)) / scale),
            score=float(score),
        ))
    return lines


# Item tooltips (inventory, NPC shops) start with a small cyan dot left of the
# item's name. It's opaque, unlike the see-through box around it, so it's the
# same colour on every map: an exact anchor for the name line.
TOOLTIP_DOT = (85, 204, 255)
TOOLTIP_DOT_DY = 35            # the dot sits this far below the cursor's tip
NAME_STRIP = (6, -9, 400, 15)  # the name line, relative to the dot's top-left
NAME_WHITE = 200               # the name is white; what shows through the box isn't
NAME_GAP = 16                  # empty columns that end the name


def find_tooltip_dot(rgb, cursor=None, anywhere=True):
    """Top-left (x, y) of an item tooltip's dot, or None. With several on
    screen, the one where the cursor's tooltip would be. Looks in the band
    where it normally is first: the whole screen takes ~20x longer (skipped
    unless `anywhere`)."""
    if cursor is not None:
        top = max(0, cursor[1] + TOOLTIP_DOT_DY - 12)
        dot = _find_dot(rgb[top:cursor[1] + TOOLTIP_DOT_DY + 20], (cursor[0], cursor[1] - top))
        if dot:
            return dot[0], dot[1] + top
    return _find_dot(rgb, cursor) if anywhere else None


def _find_dot(rgb, cursor):
    if not rgb.size:
        return None
    near = (np.abs(rgb.astype(np.int16) - TOOLTIP_DOT).max(axis=2) <= 10).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(near)
    dots = [(int(x), int(y)) for x, y, w, h, _ in stats[1:] if 3 <= w <= 8 and 3 <= h <= 8]
    if not dots:
        return None
    if cursor is None:
        return dots[0]
    cx, cy = cursor
    return min(dots, key=lambda d: abs(d[1] - cy - TOOLTIP_DOT_DY) * 4 + abs(d[0] - cx))


def _name_only(strip):
    """The name line as white text on black, cut at the first wide gap so
    nothing right of the tooltip comes along."""
    white = strip.min(axis=2) >= NAME_WHITE
    cols = np.flatnonzero(white.any(axis=0))
    if not len(cols):
        return None
    gaps = np.flatnonzero(np.diff(cols) > NAME_GAP)
    end = cols[gaps[0]] + 1 if len(gaps) else cols[-1] + 1
    out = np.where(white[:, :end, None], 255, 0).astype(np.uint8).repeat(3, axis=2)
    return cv2.copyMakeBorder(out, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=0)


def read_tooltip(img, cursor=None):
    """(readings of the item's name, best first; the dot's (x, y)) for the item
    tooltip on screen, or None when there isn't one. Two readings: the name
    cleaned up to white-on-black, then as is (OCR slips differ between them)."""
    rgb = np.array(img.convert("RGB"))
    dot = find_tooltip_dot(rgb, cursor)
    if not dot:
        return None
    x0, y0, x1, y1 = NAME_STRIP
    strip = rgb[max(0, dot[1] + y0):dot[1] + y1, dot[0] + x0:dot[0] + x1]
    names = []
    clean = _name_only(strip)
    if clean is not None:
        names.append(" ".join(t for _, t, _ in sorted(_read(clean) or [], key=lambda r: r[0][0][0])))
    raw = sorted(_read(np.ascontiguousarray(strip)) or [], key=lambda r: r[0][0][0])
    if raw:
        names.append(raw[0][1])  # the leftmost: anything further right shows through the box
    names = list(dict.fromkeys(n.strip() for n in names if n.strip()))
    return (names, dot) if names else None
