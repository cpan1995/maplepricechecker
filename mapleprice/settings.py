"""User options, saved next to the database so they survive restarts."""
import json
import logging
import re
from dataclasses import asdict, dataclass

from .data import DEFAULT_DB

PATH = DEFAULT_DB.parent / "settings.json"
log = logging.getLogger("mapleprice")
HOTKEYS = (("scan_key", "Scan shop"), ("clear_key", "Clear"), ("options_key", "This window"),
           ("hover_key", "Hover price check"))


def hotkey_name(key):
    """"F8", "A", "7" for a key we can poll (F1-F24, a letter or digit), else None."""
    key = str(key).upper()
    return key if re.fullmatch(r"F([1-9]|1\d|2[0-4])|[A-Z0-9]", key) else None


@dataclass
class Settings:
    channel: int | None = None     # manual channel; None = ask the game's connection
    fm_room: int | None = None     # manual FM room; None = read it from the minimap
    show_labels: bool = True       # prices beside each shop row
    show_panel: bool = True        # detail cards right of the shop
    open_dashboard: bool = True    # open MapleDash in the browser when MaplePrice starts
    show_options: bool = True      # open the options window when MaplePrice starts
    auto_scan: bool = True         # scan a shop by itself once it opens or its list changes
    scan_key: str = "F8"
    clear_key: str = "F9"
    options_key: str = "F10"
    hover_key: str = "F7"         # toggles pricing whatever item tooltip is under the cursor

    @classmethod
    def load(cls, path=PATH):
        try:
            data = json.loads(path.read_text())
            s = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            for field, _ in HOTKEYS:  # a hand-edited key we can't poll: back to the default
                setattr(s, field, hotkey_name(getattr(s, field)) or getattr(cls, field))
            return s
        except FileNotFoundError:
            return cls()
        except Exception:
            log.exception("bad settings file %s, using defaults", path)
            return cls()

    def save(self, path=PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
