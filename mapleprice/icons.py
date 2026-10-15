"""Tell item variants apart by icon colour.

Scroll tiers share a name prefix the shop cuts off ("Earring STR Scroll: ...")
but not a colour: Lesser is blue, Intermediate red, Greater yellow, Chaos grey.
We compare the average colour of the icon in the shop row against each
candidate's real icon from MeowDB.
"""
import colorsys
import io

MAX_DISTANCE = 35   # best candidate must be at least this close...
MIN_MARGIN = 10     # ...and this much closer than the runner-up


def icon_box(listing):
    """Where the item icon sits in a shop row: just left of the item name."""
    return (listing.x - 40, listing.y - 4, listing.x - 4, listing.bottom + 4)


def feature(img):
    """Mean RGB of the icon's own pixels, skipping transparency, black outlines,
    quantity digits and the light grey shop background."""
    data = img.convert("RGBA").tobytes()
    total, n = [0, 0, 0], 0
    for i in range(0, len(data), 4):
        r, g, b, a = data[i:i + 4]
        if a < 200:
            continue
        _, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v < 0.2 or (s < 0.15 and v > 0.7):
            continue
        total[0] += r
        total[1] += g
        total[2] += b
        n += 1
    return tuple(c / n for c in total) if n else None


def distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def pick(shop_color, candidate_colors):
    """Index of the candidate whose icon clearly matches, else None."""
    if shop_color is None:
        return None
    scored = sorted(
        (distance(shop_color, c), i) for i, c in enumerate(candidate_colors) if c is not None
    )
    if not scored or scored[0][0] > MAX_DISTANCE:
        return None
    if len(scored) > 1 and scored[1][0] - scored[0][0] < MIN_MARGIN:
        return None
    return scored[0][1]


def _is_tile(rgba):
    """The light grey of the shop's icon tile and row background."""
    r, g, b, a = rgba
    _, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return a and sat < 0.2 and val > 0.6


def crop_icon(img, listing):
    """The item icon exactly as the shop shows it (stack count included), as PNG
    bytes with the shop's grey tile made transparent. Free and instant: no
    download needed."""
    x0, y0, x1, y1 = icon_box(listing)
    icon = img.crop((x0 - 4, y0, x1, y1 - 2)).convert("RGBA")  # wide enough for stack counts
    w, h = icon.size
    px = icon.load()
    # Clear tile-grey pixels connected to the outer band, where the tile and
    # the row background are; the item art in the middle is left alone.
    band = 5
    todo = [(x, y) for x in range(w) for y in range(h)
            if (x < band or y < band or x >= w - band or y >= h - band) and _is_tile(px[x, y])]
    clear = set(todo)
    while todo:
        x, y = todo.pop()
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= n[0] < w and 0 <= n[1] < h and n not in clear and _is_tile(px[n]):
                clear.add(n)
                todo.append(n)
    for x, y in clear:
        px[x, y] = (0, 0, 0, 0)
    # Divider lines of the shop UI: an edge row/column that's solid across the icon.
    for y in [*range(4), *range(h - 3, h)]:
        if sum(px[x, y][3] > 0 for x in range(w)) > 0.7 * w:
            for x in range(w):
                px[x, y] = (0, 0, 0, 0)
    for x in [*range(6), *range(w - 3, w)]:
        if sum(px[x, y][3] > 0 for y in range(h)) > 0.7 * h:
            for y in range(h):
                px[x, y] = (0, 0, 0, 0)
    buf = io.BytesIO()
    icon.save(buf, "PNG")
    return buf.getvalue()
