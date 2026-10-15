"""MaplePrice's look: cream panels with wood-brown borders and hard drop shadows,
like MapleStory's own windows. Fredoka for titles, Nunito for everything else
(both SIL Open Font License, bundled in assets/fonts with their licences).

Colours are picked to read on cream, not on the game behind it."""
import sys
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

# ---- palette -----------------------------------------------------------------
CREAM = "#fff8e7"      # panels
CREAM_2 = "#fdecc8"    # panel bottom / fields
CARD = "#fffaf0"       # cards inside a panel
CARD_LINE = "#e2c592"  # card borders
WOOD = "#7a3b0c"       # window borders
WOOD_DARK = "#4a2306"  # hard drop shadows
INK = "#3b1d06"        # main text
MUTED = "#74553a"      # captions ("Asking", "FM", "NPC")
FAINT = "#a8906e"      # greyed out
ORANGE = "#f58a1f"
GOLD_INK = "#b9560a"   # FM prices: gold reads badly on cream, burnt orange doesn't
UP = "#23803a"         # good for you (profit)
DOWN = "#c2302a"       # bad for you (overpriced)
BLUE = "#2f5f9e"
PLUM = "#a3249a"       # deals
WARN = "#8a5a00"
PURPLE = "#6b4fa0"

SHADOW = 3             # px the hard shadow drops below a window
RADIUS = 10            # window corners
CARD_RADIUS = 7


def tint(color, amount=0.86, base=CARD):
    """`color` washed out towards `base`: the fill behind a chip's coloured text."""
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(c, b))


# ---- fonts -------------------------------------------------------------------
# name -> (Tk font, PIL file, PIL pixel size). Tk sizes are points; PIL's are the
# same size in pixels at 96 DPI (pt * 4/3), so --image mode matches the overlay.
FONTS = {
    "title": (("Fredoka SemiBold", 10), "Fredoka-SemiBold.ttf", 13),
    "body": (("Nunito ExtraBold", 9), "Nunito-ExtraBold.ttf", 12),
    "small": (("Nunito", 9, "bold"), "Nunito-Bold.ttf", 12),
    "pill": (("Nunito ExtraBold", 7), "Nunito-ExtraBold.ttf", 10),
    "label": (("Nunito ExtraBold", 11), "Nunito-ExtraBold.ttf", 15),
    "label_small": (("Nunito ExtraBold", 9), "Nunito-ExtraBold.ttf", 12),
    "big": (("Fredoka SemiBold", 18), "Fredoka-SemiBold.ttf", 24),
}

_loaded = False


def load_fonts():
    """Make the bundled fonts usable by Tk, for this process only (nothing is
    installed). Windows only; elsewhere Tk falls back to a default font.
    Windows won't load a font from a network path (e.g. running from \\\\wsl.localhost),
    so they're copied to a local temp folder first."""
    global _loaded
    if _loaded or sys.platform != "win32":
        return
    import ctypes
    import logging
    import shutil
    import tempfile
    FR_PRIVATE = 0x10
    local = Path(tempfile.gettempdir()) / "mapleprice-fonts"
    for f in sorted(FONT_DIR.glob("*.ttf")):
        try:
            local.mkdir(exist_ok=True)
            copy = local / f.name
            if not copy.exists() or copy.stat().st_size != f.stat().st_size:
                shutil.copyfile(f, copy)
            if not ctypes.windll.gdi32.AddFontResourceExW(str(copy), FR_PRIVATE, 0):
                logging.getLogger("mapleprice").warning("couldn't load font %s", copy)
        except OSError:
            logging.getLogger("mapleprice").exception("couldn't load font %s", f)
    _loaded = True


def pil_font(name):
    from PIL import ImageFont
    _, file, size = FONTS[name]
    return ImageFont.truetype(str(FONT_DIR / file), size)
