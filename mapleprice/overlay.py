"""Transparent, click-through, always-on-top window drawn over the game."""
import base64
import ctypes
import tkinter as tk
import tkinter.font as tkfont

from . import theme
from .annotate import hover_panel
from .render import draw_badge, draw_labels, draw_panel
from .settings import HOTKEYS, hotkey_name
from .version import VERSION

KEY = "#ff00fe"  # color-keyed to fully transparent

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x8000000
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x10
SWP_SHOWWINDOW = 0x40


class Overlay:
    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.configure(bg=KEY)
        self.canvas = tk.Canvas(root, bg=KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        theme.load_fonts()
        self.fonts = {name: tkfont.Font(root=root, font=tk_font) for name, (tk_font, *_) in theme.FONTS.items()}
        self.images = []  # PhotoImages on the canvas (icons)
        self._flash_job = None
        self.rect = None  # where place() last put us
        root.update_idletasks()
        self._click_through()

    def _click_through(self):
        user32 = ctypes.windll.user32
        self.hwnd = hwnd = user32.GetParent(self.root.winfo_id())
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )

    def place(self, rect):
        self.rect = rect
        left, top, width, height = rect
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        # Tk's geometry() alone doesn't move an overrideredirect window here; do it natively.
        ctypes.windll.user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, left, top, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.root.update()

    def clear(self):
        self.canvas.delete("all")
        self.images = []
        self.root.update()

    def status(self, text, tone="info"):
        """Badge at the top centre: clear of the minimap (top-left) and the shop.
        `tone` is "info", "warn" or "error" (render.TONES)."""
        self.canvas.delete("status")
        draw_badge(TkPainter(self, tags=("status",)), self.canvas.winfo_width() // 2, 6, text, tone)
        self.root.update()

    def clear_status(self):
        self.canvas.delete("status")
        self.root.update()

    def flash(self, text, tone="warn", ms=2000):
        """Message in the middle of the game for `ms`, then gone."""
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self.canvas.delete("flash")
        draw_badge(TkPainter(self, tags=("flash",)), self.canvas.winfo_width() // 2,
                   self.canvas.winfo_height() // 2, text, tone, font="big", center=True)
        self.root.update()
        self._flash_job = self.root.after(ms, lambda: self.canvas.delete("flash"))

    def draw(self, labels):
        draw_labels(TkPainter(self), labels)

    def draw_panel(self, panel):
        draw_panel(TkPainter(self), panel)

    def hover_card(self, card=None, dot=None):
        """The price card for the hovered item, beside its tooltip (whose dot is
        at `dot`): left of it, or right if there's no room. None hides it."""
        self.canvas.delete("hover")
        if card:
            draw_panel(TkPainter(self, tags=("hover",)), hover_panel(card, dot))
        self.root.update()


class TkPainter:
    """render.py painter for the overlay canvas. Everything it draws gets `tags`,
    so a badge can be replaced without clearing the rest."""

    def __init__(self, overlay, tags=()):
        self.ov = overlay
        self.canvas = overlay.canvas
        self.fonts = overlay.fonts
        self.tags = tags

    def size(self):
        return self.canvas.winfo_width(), self.canvas.winfo_height()

    def text_width(self, text, font):
        return self.fonts[font].measure(text)

    def line_height(self, font):
        return self.fonts[font].metrics("linespace")

    def text(self, x, y, text, color, font):
        self.canvas.create_text(x, y, text=text, fill=color, font=self.fonts[font], anchor="nw", tags=self.tags)

    def round_rect(self, x0, y0, x1, y1, r, fill, outline=None, width=1):
        """A smoothed polygon: Tk has no rounded rectangle. Corner points are
        doubled so the curve hugs the corners instead of rounding the whole side."""
        r = max(1, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
        pts = [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r,
               x1, y1 - r, x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]
        self.canvas.create_polygon(pts, smooth=True, fill=fill, outline=outline or "",
                                   width=width if outline else 0, tags=self.tags)

    def image(self, x, y, png):
        photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
        self.ov.images.append(photo)  # Tk drops images that Python stops referencing
        self.canvas.create_image(x, y, image=photo, anchor="nw", tags=self.tags)


class OptionsWindow:
    """Small normal window (F10) for things the overlay can't see by itself."""

    BG, FG, MUTED, FIELD = theme.CREAM, theme.INK, theme.MUTED, theme.CREAM_2
    TITLE_FONT, FONT, BOLD = ("Fredoka SemiBold", 14), ("Nunito", 10, "bold"), ("Nunito ExtraBold", 10)

    def __init__(self, root, settings, on_change, detected_map, detected_channel, open_dashboard=None):
        self.root, self.settings, self.on_change, self.detected_map = root, settings, on_change, detected_map
        self.detected_channel = detected_channel
        self.open_dashboard = open_dashboard
        self.win = None
        self.capturing = False  # waiting for a new hotkey: the listener holds off
        self.key_buttons = {}

    def toggle(self):
        if self.win and self.win.winfo_exists():
            self.win.destroy()
            self.win = None
            self.capturing = False  # closed mid-rebind
        else:
            self._open()

    def _label(self, parent, text, muted=False, **grid):
        tk.Label(parent, text=text, bg=self.BG, fg=self.MUTED if muted else self.FG,
                 font=("Nunito", 9, "bold") if muted else self.FONT, anchor="w", justify="left").grid(sticky="w", **grid)

    def _open(self):
        s = self.settings
        win = self.win = tk.Toplevel(self.root)
        win.title(f"MaplePrice {VERSION} options")
        win.configure(bg=self.BG, padx=18, pady=14)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.toggle)

        tk.Label(win, text="MaplePrice", bg=self.BG, fg=theme.WOOD, font=self.TITLE_FONT).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self._label(win, "Where you are, saved with every scan so MapleDash\ncan tell you where to find a shop again.",
                    muted=True, row=1, column=0, columnspan=2, pady=(0, 10))

        spin = dict(bg=self.FIELD, fg=self.FG, buttonbackground=self.FIELD, insertbackground=self.FG,
                    relief="flat", width=7, font=self.BOLD, state="readonly", readonlybackground=self.FIELD)

        self._label(win, "Channel", row=2, column=0, pady=4)
        channel = tk.StringVar()
        tk.Spinbox(win, values=("Auto",) + tuple(str(i) for i in range(1, 41)), textvariable=channel,
                   command=lambda: self._set("channel", channel.get()), **spin).grid(row=2, column=1, sticky="w")
        channel.set(str(s.channel) if s.channel else "Auto")  # after: a new Spinbox resets to its first value
        channel_now = tk.Label(win, bg=self.BG, fg=self.MUTED, font=("Nunito", 9, "bold"), anchor="w",
                               justify="left")
        channel_now.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 6))

        def show_channel():  # live while open: you can change channel with the window up
            if self.win is win and win.winfo_exists():
                on = self.detected_channel()
                channel_now.configure(text=f"Auto asks the game's connection{f' (now: Ch {on})' if on else ''}.")
                win.after(2000, show_channel)
        show_channel()

        self._label(win, "FM room", row=4, column=0, pady=4)
        room = tk.StringVar()
        tk.Spinbox(win, values=("Auto",) + tuple(str(i) for i in range(1, 31)), textvariable=room,
                   command=lambda: self._set("fm_room", room.get()), **spin).grid(row=4, column=1, sticky="w")
        room.set(str(s.fm_room) if s.fm_room else "Auto")
        detected = self.detected_map()
        self._label(win, f"Auto reads the map name off the minimap{f' (last: {detected})' if detected else ''}.\n"
                         "Expand the minimap if it's collapsed.", muted=True, row=5, column=0, columnspan=2, pady=(0, 10))

        check = dict(bg=self.BG, fg=self.FG, selectcolor=self.FIELD, activebackground=self.BG,
                     activeforeground=self.FG, font=self.FONT, anchor="w")
        auto = tk.BooleanVar(value=s.auto_scan)
        tk.Checkbutton(win, text="Scan shops by themselves as they open", variable=auto,
                       command=lambda: self._set("auto_scan", auto.get()), **check).grid(
            row=6, column=0, columnspan=2, sticky="w")
        labels = tk.BooleanVar(value=s.show_labels)
        tk.Checkbutton(win, text="Prices beside each shop row", variable=labels,
                       command=lambda: self._set("show_labels", labels.get()), **check).grid(
            row=7, column=0, columnspan=2, sticky="w")
        panel = tk.BooleanVar(value=s.show_panel)
        tk.Checkbutton(win, text="Detail cards right of the shop", variable=panel,
                       command=lambda: self._set("show_panel", panel.get()), **check).grid(
            row=8, column=0, columnspan=2, sticky="w")

        startup = tk.BooleanVar(value=s.open_dashboard)
        tk.Checkbutton(win, text="Open the dashboard when MaplePrice starts", variable=startup,
                       command=lambda: self._set("open_dashboard", startup.get()), **check).grid(
            row=9, column=0, columnspan=2, sticky="w")
        options = tk.BooleanVar(value=s.show_options)
        tk.Checkbutton(win, text="Open this window when MaplePrice starts", variable=options,
                       command=lambda: self._set("show_options", options.get()), **check).grid(
            row=10, column=0, columnspan=2, sticky="w")

        tk.Label(win, text="Hotkeys", bg=self.BG, fg=theme.WOOD, font=("Fredoka SemiBold", 11)).grid(
            row=11, column=0, sticky="w", pady=(12, 0))
        self._label(win, "Click one, then press the new key (F1-F24, A-Z, 0-9).\nEsc cancels.",
                    muted=True, row=12, column=0, columnspan=2, pady=(0, 4))
        self.key_buttons = {}
        for i, (field, name) in enumerate(HOTKEYS):
            self._label(win, name, row=13 + i, column=0, pady=2)
            b = tk.Button(win, text=getattr(s, field), width=12, bg=self.FIELD, fg=self.FG, activebackground=self.FIELD,
                          activeforeground=self.FG, relief="solid", bd=1, highlightbackground=theme.WOOD,
                          font=self.BOLD, cursor="hand2")
            b.configure(command=lambda f=field, b=b: self._capture(f, b))
            b.grid(row=13 + i, column=1, sticky="w", pady=2)
            self.key_buttons[field] = b

        if self.open_dashboard:
            tk.Button(win, text="Open market dashboard", command=self.open_dashboard, bg=theme.ORANGE, fg="#ffffff",
                      activebackground="#ffb44d", activeforeground="#ffffff", relief="solid", bd=2,
                      font=("Fredoka SemiBold", 11), padx=10, pady=3,
                      cursor="hand2").grid(row=13 + len(HOTKEYS), column=0, columnspan=2, sticky="we", pady=(12, 0))
        win.focus_force()

    def _capture(self, field, button):
        """The next key pressed in this window becomes `field`'s hotkey; Esc cancels."""
        self.capturing = True
        self._show_keys()
        button.configure(text="Press a key...", fg=theme.GOLD_INK)

        def pressed(event):
            key = hotkey_name(event.keysym)
            if key is None and event.keysym != "Escape":
                return "break"  # Shift, arrows...: can't be a hotkey, keep waiting
            self.win.unbind("<KeyPress>")
            if key:
                self.set_key(field, key)
            self._show_keys()
            # The hotkey listener sees this press too: stay paused until it has
            # seen the key held, or it would fire as soon as we unpause.
            self.root.after(400, lambda: setattr(self, "capturing", False))
            return "break"

        self.win.bind("<KeyPress>", pressed)
        self.win.focus_force()

    def set_key(self, field, key):
        """Bind `key` to `field`; an action that already had it gets `field`'s old key."""
        s = self.settings
        for other, _ in HOTKEYS:
            if other != field and getattr(s, other) == key:
                setattr(s, other, getattr(s, field))
        setattr(s, field, key)
        self.on_change()

    def _show_keys(self):
        for field, b in self.key_buttons.items():
            b.configure(text=getattr(self.settings, field), fg=self.FG)

    def _set(self, key, value):
        if key in ("channel", "fm_room"):
            value = int(value) if str(value).isdigit() else None
        setattr(self.settings, key, value)
        self.on_change()
