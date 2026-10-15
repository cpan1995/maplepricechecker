"""Turn OCR lines from a player shop window into (item, qty, price) listings.

Each shop row renders as two lines:
    Pig's Head
    1 for 500 mesos
"""
import re
from collections import Counter
from dataclasses import dataclass

from .icons import crop_icon, feature, icon_box

# "1 for 500 mesos", also when the shop cuts it off ("1 for 499,999,999,999 me...")
# and when OCR mistakes digits for look-alike letters ("1 for gg,ggg me...").
CONFUSABLE = str.maketrans("OoDIlgqSB", "000119958")
NUM = r"([\dOoDIlgqSB][\dOoDIlgqSB,.]*)"
# The price itself can be unreadable ("1 for me..."): it's then None.
PRICE_RE = re.compile(rf"^\s*{NUM}\s*for\s*(?:{NUM})?\s*me", re.IGNORECASE)


@dataclass
class Line:
    text: str
    x: int
    y: int
    w: int
    h: int
    words: list = None  # [(text, x), ...] when the OCR provides word positions
    score: float = 1.0  # OCR confidence


@dataclass
class Listing:
    name: str
    qty: int
    price: int | None  # price for the whole bundle of `qty`; None if unreadable
    x: int          # left edge of the item name text
    y: int          # top of the name line
    bottom: int     # bottom of the price line
    uncertain: bool = False  # price had to be guessed (look-alike letters or cut off)
    confident: bool = False  # the OCR was sure of the price line
    sold: bool = False       # greyed out: already bought, can't be bought any more


def _int(s):
    return int(re.sub(r"[,.]", "", s).translate(CONFUSABLE))


def _is_price_line(text):
    m = PRICE_RE.match(text)
    return m if m and re.search(r"\d", m.group(1) + (m.group(2) or "")) else None


ICON_WIDTH = 45
CONFIDENT = 0.9  # OCR score above which one reading of a price is trusted on its own


def _strip_icon_junk(name_line, column_x):
    """Drop words the OCR read off the item icon, i.e. left of the text column."""
    if name_line.x >= column_x - 6:
        return name_line.text.strip()
    if name_line.words:
        return " ".join(t for t, x in name_line.words if x >= column_x - 6).strip()
    # No word positions: icon junk is a short leading token like "O" or "D".
    parts = name_line.text.split()
    while parts and (len(parts[0]) <= 2 or not parts[0][0].isalnum()):
        parts.pop(0)
    return " ".join(parts)


def parse_listings(lines):
    listings = []
    for price_line in lines:
        m = _is_price_line(price_line.text)
        if not m:
            continue
        # The name is the closest line directly above, left-aligned with the
        # price. OCR sometimes glues part of the item icon onto the front of the
        # name ("O Slime Bubble"), which starts the line up to an icon's width early.
        above = [
            l for l in lines
            if l is not price_line
            and -ICON_WIDTH <= l.x - price_line.x <= 12
            and 0 < price_line.y - l.y <= price_line.h * 2.5
            and not _is_price_line(l.text)
            and re.search(r"[A-Za-z]{2}", l.text)  # not the stack count drawn on the icon
        ]
        if not above:
            continue
        name_line = max(above, key=lambda l: l.y)
        try:
            qty = _int(m.group(1))
            price = _int(m.group(2)) if m.group(2) else None
        except ValueError:
            continue
        name = _strip_icon_junk(name_line, price_line.x)
        if not name:
            continue
        guessed = price is None or not re.fullmatch(r"[\d,.]+", m.group(1) + m.group(2))
        cut_off = not re.search(r"mes[o0]s", price_line.text, re.IGNORECASE)
        listings.append(Listing(
            name=name, qty=qty, price=price,
            x=price_line.x, y=name_line.y, bottom=price_line.y + price_line.h,
            uncertain=guessed or cut_off,
            confident=price_line.score >= CONFIDENT,
        ))
    return sorted(listings, key=lambda l: l.y)


TEXT_WIDTH = 150   # px of the name/price text column we look at
SOLD_DARKEST = 100  # in-stock text is black (0); a sold row's greyed text is >= ~160


def is_sold(image, listing):
    """Sold rows stay in the shop but greyed out: pale grey text on a near-white
    row instead of black text. So: no dark pixels in the text = sold."""
    gray = image.convert("L").crop((listing.x, listing.y, listing.x + TEXT_WIDTH, listing.bottom))
    return gray.getextrema()[0] >= SOLD_DARKEST


SHOP_ROWS = 6       # the shop window shows this many listings at once
ROW_TOLERANCE = 10  # px: readings this close vertically are the same row


@dataclass
class Row:
    """One shop row merged across several OCR attempts."""
    names: list     # distinct name readings, most frequent first
    icon_color: tuple | None  # average colour of the row's item icon (icons.feature)
    icon: bytes | None        # the row's icon cut from the screenshot (PNG)
    confirmed: bool  # read cleanly, and either confidently or the same way twice
    uncertain: bool  # the agreed price was guessed from look-alike letters or cut off
    qty: int
    price: int
    x: int
    y: int
    bottom: int
    sold: bool = False

    def listing(self, name):
        return Listing(name, self.qty, self.price, self.x, self.y, self.bottom, self.uncertain, sold=self.sold)


def merge_attempts(attempts, image=None):
    """Combine the listings from each OCR attempt into one Row per shop row.
    `image` (any of the screenshots) is used to sample each row's icon colour."""
    groups = []  # [[Listing, ...], ...]
    for listings in attempts:
        for l in listings:
            group = next((g for g in groups if abs(g[0].y - l.y) <= ROW_TOLERANCE), None)
            if group is None:
                groups.append([l])
            else:
                group.append(l)

    rows = []
    for g in groups:
        name_counts = Counter(l.name for l in g)
        (qty, price), votes = Counter((l.qty, l.price) for l in g).most_common(1)[0]
        winners = [l for l in g if (l.qty, l.price) == (qty, price)]
        uncertain = all(l.uncertain for l in winners)
        confident = any(l.confident for l in winners)
        first = g[0]
        rows.append(Row(
            names=[n for n, _ in name_counts.most_common()],
            icon_color=feature(image.crop(icon_box(first))) if image is not None else None,
            icon=crop_icon(image, first) if image is not None else None,
            qty=qty, price=price, confirmed=(votes >= 2 or confident) and not uncertain, uncertain=uncertain,
            x=first.x, y=first.y, bottom=max(l.bottom for l in g),
            sold=is_sold(image, first) if image is not None else False,
        ))
    return sorted(rows, key=lambda r: r.y)
