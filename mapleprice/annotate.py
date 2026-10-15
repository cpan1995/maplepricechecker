"""What to draw next to each shop listing (shared by the live overlay and --image mode).

A Label is a block of lines; each line is a list of (text, color) segments so
flags like "LOWEST" can stand out from the prices around them. For tiered items
(scrolls), the other tiers' prices go on the item's card in the detail panel.
"""
from dataclasses import dataclass, field

from . import theme

NEUTRAL = theme.INK
DIM = theme.MUTED
FM = theme.GOLD_INK      # FM price, so it stands apart from the NPC price
PROFIT = theme.UP        # listing costs less than the NPC pays: buy it and vend it
LOWEST = theme.BLUE      # cheapest we've seen this item listed in the last 24h
HIGHEST = theme.DOWN     # priciest we've seen this item listed in the last 24h
UNKNOWN = theme.WARN
DEAL = theme.PLUM        # listed well under the usual FM price
SOLD = theme.FAINT       # greyed-out row: someone already bought it

LABEL_GAP = 46       # px left of the item name: clears the item icon and the shop border
TIER_ORDER = ["Lesser", "Intermediate", "Greater", "Chaos"]
DEAL_THRESHOLD = 0.20  # flag listings at least this far below the FM price


@dataclass
class Label:
    """What's left of a shop row: only what's worth acting on there (an NPC
    profit, today's price range, SOLD). Prices live on the cards; a label with
    no lines isn't drawn."""
    right: int       # right edge x
    center_y: int
    lines: list = field(default_factory=list)  # [[(text, color), ...], ...]

    @property
    def text(self):
        return " / ".join("".join(t for t, _ in line) for line in self.lines)


@dataclass
class Version:
    item: object     # data.Item
    market: object   # data.Market or None if the FM lookup failed


def fmt(n):
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}b".replace(".0b", "b")
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m".replace(".0m", "m")
    if n >= 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    if n != int(n):
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    return f"{int(n):,}"


def fmt_short(n):
    """Tighter than fmt for the other-tiers line: 4,999 -> 5k, 5,500 -> 5.5k."""
    if n < 1000:
        return f"{int(n)}"
    return fmt(n) if n >= 1_000_000 else f"{n / 1000:.1f}k".replace(".0k", "k")


def _common_prefix_len(names):
    first, last = min(names), max(names)
    i = 0
    while i < len(first) and first[i] == last[i]:
        i += 1
    # back up to a word boundary so "Intermediate" isn't shown as "ntermediate"
    while i > 0 and first[i - 1] not in " :":
        i -= 1
    return i


def _npc_profit(listing, item):
    """'NPC 7 ea = 35 (+15)' in green when the NPC pays more than the asking
    price (buy it and vend it), else nothing. A guessed or unreadable price never counts."""
    npc_total = item.price * listing.qty
    if listing.uncertain or listing.price is None or listing.price >= npc_total:
        return []
    npc = f"NPC {fmt(item.price)}" + (f" ea = {fmt(npc_total)}" if listing.qty > 1 else "")
    return [(npc, PROFIT), (f" (+{fmt(npc_total - listing.price)})", PROFIT)]


def tier(name):
    """'Crossbow Attack Scroll: Lesser' -> 'Lesser' (None for untiered items)."""
    return name.rsplit(": ", 1)[1] if ": " in name else None


def other_tiers(siblings):
    """The matched item's other tiers, in tier order, as one card line:
    'Other tiers  Lesser -   Inter. ~25k'."""
    order = lambda v: (TIER_ORDER.index(tier(v.item.name)) if tier(v.item.name) in TIER_ORDER
                       else len(TIER_ORDER), v.item.name)
    line = [("Other tiers  ", LABEL)]
    for i, v in enumerate(sorted(siblings, key=order)):
        m = v.market
        line += [(("   " if i else "") + f"{_short(tier(v.item.name) or v.item.name)} ", DIM),
                 (f"~{fmt_short(m.usual)}", FM) if m and m.usual is not None else ("-", DIM)]
    return line


def make_label(listing, match, versions, seen=None, siblings=()):
    """`seen` is the data.Seen range for this item, only when it matched exactly one item.
    `siblings` are the matched item's other tiers (Versions); their prices go on the card."""
    label = Label(listing.x - LABEL_GAP, (listing.y + listing.bottom) // 2)
    line = _label_line(listing, match, versions, seen, siblings)
    if listing.sold:  # its range still shows what it went for, but nothing to act on
        line = [seg for seg in line if seg[1] not in (LOWEST, HIGHEST, PROFIT)]
        line = [("SOLD", SOLD)] + ([("  ", SOLD)] + line if line else [])
    if line:
        label.lines.append(line)
    return label


def _label_line(listing, match, versions, seen, siblings):
    if not match.found:
        return [("not found", UNKNOWN)]
    if len(versions) != 1:  # name cut off, item undecided: the card lists what it could be
        return []
    v = versions[0]
    line = _npc_profit(listing, v.item)
    if seen and seen.count >= 2:
        unit = listing.price / listing.qty
        line.append(((" " * 2 if line else "") + f"24h {fmt(seen.low)}-{fmt(seen.high)}", DIM))
        if unit <= seen.low:
            line.append(("  LOWEST", LOWEST))
        elif unit >= seen.high:
            line.append(("  HIGHEST", HIGHEST))
    if line and siblings:  # say which tier we decided on; the card lists the others
        line.insert(0, (f"{_short(tier(v.item.name))}: ", DIM))
    return line


def _short(name):
    return name if len(name) <= 8 else name[:5] + "."


# ---- Detail panel (right of the shop window) ---------------------------------
# One card per listing: icon, name, what the item does, then a plain-English
# verdict on the asking price. Rendering lives in render.py.

LABEL = theme.MUTED     # "Asking", "FM", "NPC" captions
FAIR = theme.CARD_LINE  # accent for a fair price / no opinion
STATS = [  # (key in MeowDB stats, label)
    ("watk", "W.ATK"), ("matk", "M.ATK"), ("str", "STR"), ("dex", "DEX"), ("int", "INT"),
    ("luk", "LUK"), ("acc", "ACC"), ("avoid", "AVOID"), ("speed", "SPEED"), ("jump", "JUMP"),
    ("hp", "HP"), ("mp", "MP"), ("wdef", "W.DEF"), ("mdef", "M.DEF"),
]
OVERPRICED = 1.2      # asking >= 120% of the FM price
MIN_SANE_LISTING = 0.3  # ignore MeowDB listings under 30% of the FM price (per-piece or junk)


@dataclass
class Pill:
    """A chip: `color` for its text and border, on a wash of the same colour."""
    text: str
    color: str


@dataclass
class Card:
    title: str
    subtitle: str                                 # what the item is / does
    price: list                                   # [(text, color), ...]
    pills: list = field(default_factory=list)     # [Pill, ...] verdicts, most important first
    extra: list = field(default_factory=list)     # more [(text, color), ...] lines (cut-off names)
    accent: str = FAIR                            # colour of the card's edge: the verdict at a glance
    icon: bytes | None = None                     # PNG from MeowDB
    count: int = 1                                # identical listings merged into this card

    def key(self):
        return self.title, "".join(t for t, _ in self.price)


@dataclass
class Panel:
    """Cards go in the box (left, top)-(right, bottom): right of the shop window.
    The panel starts level with the shop's top and may slide up to `min_top`."""
    left: int
    top: int
    right: int
    bottom: int
    min_top: int = 8
    cards: list = field(default_factory=list)


def describe_stats(info):
    """'Lv 15 Bowman · W.DEF +12  HP +5 · 5 slots' / '60% success · W.ATK +2' / ''."""
    if not info:
        return ""
    stats = info.get("stats") or {}
    parts = []
    req = " ".join(p for p in (f"Lv {info['levelReq']}" if info.get("levelReq") else "",
                               info.get("reqJob") or "") if p)
    if req:
        parts.append(req)
    if stats.get("successRate"):
        parts.append(f"{stats['successRate']}% success")
    bonuses = "  ".join(f"{label} +{stats[k]}" for k, label in STATS if stats.get(k))
    if bonuses:
        parts.append(bonuses)
    if stats.get("slots"):
        parts.append(f"{stats['slots']} slots")
    return "  \u00b7  ".join(parts)


def _asking(listing):
    if listing.price is None:
        return [("Asking ", LABEL), ("?", UNKNOWN)]
    segs = [("Asking ", LABEL), (fmt(listing.price), NEUTRAL)]
    if listing.qty > 1:
        segs.append((f" for {fmt(listing.qty)} ({fmt(listing.price / listing.qty)} ea)", DIM))
    return segs


def _fm(m):
    """'FM ~21.5k (3)', or nothing when there's no FM price to show."""
    if m is None or m.usual is None:
        return []
    return [("    FM ", LABEL), (f"~{fmt(m.usual)}", FM), (f" ({m.reports})", DIM)]


def _npc_price(item, qty):
    if not item.price:
        return []
    return [("    NPC ", LABEL), (fmt(item.price) + (" ea" if qty > 1 else ""), NEUTRAL)]


def verdict(listing, market):
    """(Pill or None, accent colour) comparing the asking price with the FM price."""
    if listing.price is None or listing.uncertain:
        return Pill("PRICE UNCLEAR", UNKNOWN), UNKNOWN
    if not market or not market.usual or listing.qty != 1:
        return None, FAIR
    ratio = listing.price / market.usual
    if ratio <= 1 - DEAL_THRESHOLD:
        return Pill(f"DEAL  {1 - ratio:.0%} UNDER FM", DEAL), DEAL
    if ratio >= OVERPRICED:
        over = f"{ratio:.1f}x FM PRICE" if ratio >= 2 else f"{ratio - 1:.0%} OVER FM"
        return Pill(over, HIGHEST), HIGHEST
    return Pill("FAIR PRICE", theme.MUTED), FAIR


def make_card(listing, match, versions, seen=None, info=None, siblings=()):
    """The detail-panel card for one shop listing. `siblings`: the item's other
    tiers (scrolls), listed with their FM prices."""
    if not match.found:
        return Card(listing.name, "Not found on MeowDB", _asking(listing), accent=UNKNOWN)

    names = {v.item.name for v in versions}
    if len(names) == 1:  # different items sharing one name (e.g. two "Beret"s): show it once
        best = max(versions, key=lambda v: v.market.reports if v.market else -1)
        versions = [best]
    if len(versions) > 1:
        cut = _common_prefix_len(sorted(names))
        card = Card(listing.name, "Name is cut off. Could be:", _asking(listing), accent=UNKNOWN)
        seen_names = set()
        for v in versions:
            short = v.item.name[cut:].strip() or v.item.name
            if short not in seen_names:
                seen_names.add(short)
                card.extra.append([(short, NEUTRAL)] + (_fm(v.market) or [("    no FM data", DIM)]))
        return card

    v = versions[0]
    item, m = v.item, v.market
    price = _asking(listing) + _fm(m) + _npc_price(item, listing.qty)
    pill, accent = verdict(listing, m)
    card = Card(item.name, describe_stats(info), price, accent=accent)
    if siblings:
        card.extra.append(other_tiers(siblings))
    if pill:
        card.pills.append(pill)
    certain = listing.price is not None and not listing.uncertain
    npc_total = item.price * listing.qty
    if certain and listing.price < npc_total:
        card.pills.append(Pill(f"NPC PAYS {fmt(npc_total - listing.price)} MORE", PROFIT))
    if certain and seen and seen.count >= 2:
        unit = listing.price / listing.qty
        if unit <= seen.low:
            card.pills.append(Pill("CHEAPEST YOU'VE SEEN TODAY", LOWEST))
        elif unit >= seen.high:
            card.pills.append(Pill("PRICIEST YOU'VE SEEN TODAY", HIGHEST))
    if (certain and m and m.cheapest_sell and m.cheapest_sell < listing.price / listing.qty
            and (not m.usual or m.cheapest_sell >= MIN_SANE_LISTING * m.usual)):
        card.pills.append(Pill(f"MEOWDB HAS ONE FOR {fmt(m.cheapest_sell)}", theme.PURPLE))
    return card


def _meowdb_line(m):
    """'MeowDB  selling from 12k   buying at 9k', or None without live listings."""
    line = []
    if m and m.cheapest_sell:
        line += [("MeowDB  ", LABEL), ("selling from ", DIM), (fmt(m.cheapest_sell), NEUTRAL)]
    if m and m.best_buy:
        line += [("   " if line else "MeowDB  ", LABEL), ("buying at ", DIM), (fmt(m.best_buy), NEUTRAL)]
    return line or None


def make_price_card(name, match, versions, info=None, siblings=()):
    """The card for an item you're hovering over (inventory, NPC shop): what
    it's worth, with no asking price to judge."""
    if not match.found:
        return Card(name, "Not found on MeowDB", [("Can't price this one", DIM)], accent=UNKNOWN)
    best = max(versions, key=lambda v: v.market.reports if v.market else -1)  # same-name items: show one
    item, m = best.item, best.market
    fm = _fm(m)
    price = ([("FM ", LABEL)] + fm[1:] if fm else [("No FM price yet", DIM)]) + _npc_price(item, 1)
    card = Card(item.name, describe_stats(info), price, accent=FM if fm else FAIR)
    meowdb = _meowdb_line(m)
    if meowdb:
        card.extra.append(meowdb)
    if siblings:
        card.extra.append(other_tiers(siblings))
    return card


HOVER_WIDTH = 320          # the hover price card
HOVER_TOOLTIP_WIDTH = 300  # room left for the game's tooltip when the card goes right of it


def hover_panel(card, dot):
    """Where the hover card goes: left of the game's tooltip (whose dot is at
    `dot`), level with its top, or right of it when there's no room."""
    left = dot[0] - 14 - HOVER_WIDTH
    if left < 4:
        left = dot[0] + HOVER_TOOLTIP_WIDTH
    top = dot[1] - 14
    return Panel(left, top, left + HOVER_WIDTH, top + 400, cards=[card])
