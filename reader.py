#!/usr/bin/env python3
"""
Webtoon Reader - vertical, continuous-scroll image viewer (tkinter/ttk + Pillow).

Requires: Pillow   (pip install pillow)
Optional: gallery-dl (pip install -U gallery-dl) for Discover & Download
Usage:    python webtoon_reader.py [folder_or_image]

Built to use the machine properly (tuned for Windows, works elsewhere):
  * Per-page decoding + Lanczos resampling runs on a thread pool, so scrolling never waits
  * Tall pages are cut into tiles, so even 30,000 px strips work
  * A big read-ahead window keeps the next screens decoded before you get there
  * Directory scanning (Library) and cover previews run off the UI thread
  * DPI-aware: crisp on 125% / 150% / 4K displays

Keys
  Up/Down, J/K      scroll          Space / Shift+Space   page down / up
  PgUp/PgDn         page            Home / End            top / bottom
  + / -             image width     Ctrl+Wheel            image width
  T                 next theme      A                     toggle auto-scroll
  [ / ]             slower / faster auto-scroll
  N / P             next / previous chapter (sibling folder)
  O                 open folder     F  open files
  L                 Library + History panel
  D                 Discover & Download window (covers, queue, saved searches)
  C                 continue reading (last opened book)
  Esc / Backspace   back to the Home screen (History / Library / Explore)
  Z                 zen mode        F11  fullscreen       H / double-click  toolbar
  Esc               leave fullscreen / zen          Q     quit
"""
import colorsys
import glob
import hashlib
import io
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote_plus

# Sharp (not bitmap-stretched) rendering on Windows high-DPI screens. Must run before Tk starts.
if sys.platform == "win32":
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

try:
    from PIL import Image, ImageTk, ImageDraw, ImageOps
except ImportError:
    sys.exit("Pillow is required:  pip install pillow")

_R = getattr(Image, "Resampling", Image)
SMOOTH = _R.LANCZOS                 # best-quality resampling, used for pages and the HUD
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
CFG_PATH = os.path.join(os.path.expanduser("~"), ".webtoon_reader.json")
TILE = 2048                          # max height of one canvas image tile
WORKERS = max(4, min(12, os.cpu_count() or 4))


# ------------------------------------------------------------------ colours
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    ra, rb = hex_rgb(a), hex_rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


THEMES = {
    "Midnight":  dict(bg="#0f0f14", panel="#1a1b26", fg="#c0caf5", accent="#7aa2f7", accent2="#bb9af7", border="#2a2e45", canvas="#08080b"),
    "AMOLED":    dict(bg="#000000", panel="#0a0a0a", fg="#e8e8e8", accent="#00d1b2", accent2="#7c4dff", border="#222222", canvas="#000000"),
    "Cyberpunk": dict(bg="#0a0a14", panel="#12122a", fg="#e8f1ff", accent="#ff2a6d", accent2="#05d9e8", border="#2a2a52", canvas="#05050d"),
    "Ocean":     dict(bg="#0b1a24", panel="#10283a", fg="#d6ecf5", accent="#2ec4b6", accent2="#4cc9f0", border="#1d4057", canvas="#071219"),
    "Dracula":   dict(bg="#21222c", panel="#282a36", fg="#f8f8f2", accent="#bd93f9", accent2="#ff79c6", border="#44475a", canvas="#191a21"),
    "Nord":      dict(bg="#2e3440", panel="#3b4252", fg="#eceff4", accent="#88c0d0", accent2="#b48ead", border="#4c566a", canvas="#242933"),
    "Gruvbox":   dict(bg="#1d2021", panel="#282828", fg="#ebdbb2", accent="#fabd2f", accent2="#fe8019", border="#504945", canvas="#141617"),
    "Sepia":     dict(bg="#e9dcc3", panel="#d9c9a8", fg="#3b2f1e", accent="#a0522d", accent2="#d98c3f", border="#bfa985", canvas="#f4ead5"),
    "Light":     dict(bg="#f2f2f2", panel="#e4e4e8", fg="#202124", accent="#3367d6", accent2="#9c27b0", border="#c8c8d0", canvas="#ffffff"),
}
for _t in THEMES.values():
    _t["col"] = mix(_t["canvas"], _t["fg"], 0.05)       # reading column backdrop
    _t["ph"] = mix(_t["col"], _t["accent"], 0.10)       # page placeholder while decoding
    _t["dim"] = mix(_t["fg"], _t["canvas"], 0.50)       # muted text on the canvas
    _t["dim_p"] = mix(_t["fg"], _t["panel"], 0.40)      # muted text on panels
    _t["track"] = mix(_t["canvas"], _t["fg"], 0.16)     # progress line track


def pill_image(w, h, fill, outline, ss=4):
    """Anti-aliased translucent rounded pill (RGBA)."""
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    try:
        d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=h * ss // 2,
                            fill=fill, outline=outline, width=ss)
    except AttributeError:  # very old Pillow
        d.ellipse((0, 0, h * ss, h * ss), fill=fill)
        d.ellipse((w * ss - h * ss, 0, w * ss, h * ss), fill=fill)
        d.rectangle((h * ss // 2, 0, w * ss - h * ss // 2, h * ss), fill=fill)
    return im.resize((w, h), SMOOTH)


def gradient_image(w, h, c1, c2):
    a, b = hex_rgb(c1), hex_rgb(c2)
    row = [tuple(round(a[k] + (b[k] - a[k]) * x / max(1, w - 1)) for k in range(3)) for x in range(w)]
    im = Image.new("RGB", (w, h))
    im.putdata(row * h)
    return im


# ------------------------------------------------------------------ helpers
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_images(folder):
    try:
        names = [n for n in os.listdir(folder) if os.path.splitext(n)[1].lower() in EXTS]
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names, key=natural_key)]


def image_size(path):
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ------------------------------------------------------- downloader backend (gallery-dl)
# gallery-dl jobs run on worker threads and talk to the UI only through the results queue,
# exactly like the page decoders do.  Options are set once per job and never cleared, so a
# search running in one thread can't wipe the settings of a download running in another.
try:
    from gallery_dl import config as gdl_config
    from gallery_dl import exception as gdl_exc
    from gallery_dl import extractor as gdl_extractor
    from gallery_dl import job as gdl_job
    HAVE_GDL = True
except Exception:
    HAVE_GDL = False

DL_MODES = ("Search", "Codes", "Tags", "URLs")
ALL_SITES = "(all listed)"
SORTS = ("popular", "popular-week", "popular-today", "recent")
DEFAULT_UA = "MyBatchDownloader/1.0"
COVER_DIR = os.path.join(os.path.expanduser("~"), ".webtoon_reader_covers")
HINTS = {
    "Search": "Keywords or tags, e.g.  artist:name language:english pages:>20",
    "Codes": "One or more numeric codes, e.g.  123456, 654321",
    "Tags": "Booru tags separated by spaces (blank = latest), e.g.  cat_ears blue_eyes",
    "URLs": "Gallery / user / post URLs, or the path to a .txt file of URLs",
}

# Rating filter tags for tag search. Sites not listed get NO filter (they are flagged unfiltered).
RATING_TAG = {
    "safebooru": "",
    "danbooru": "rating:general",
    "konachan": "rating:safe",
    "yandere": "rating:safe",
    "gelbooru": "rating:general",
}
# Folder layout for book / URL downloads: <root>/<site>/<title> [<id>]/001.jpg
LAYOUTS = {"nhentai": ["{category}", "{title[:80]} [{gallery_id}]"]}
METADATA_SITES = {"nhentai"}          # these also get an info.json next to the images
# URL templates per site. Add a site by adding an entry ("search" and/or "code").
BOOK_SITES = {
    "nhentai": {
        "search": "https://nhentai.net/search/?q={query}&sort={sort}&page={page}",
        "code": "https://nhentai.net/g/{code}/",
    },
    "hitomi": {"code": "https://hitomi.la/galleries/{code}.html"},
    "hentainexus": {"code": "https://hentainexus.com/view/{code}"},
}


def dl_post(ready, *msg):
    ready.put(("dl",) + msg)


def dl_guard(fn, ready, *args):
    try:
        fn(ready, *args)
    except Exception as e:
        dl_post(ready, "log", f"Error: {e}")
        dl_post(ready, "idle")


def dl_code(url):
    nums = re.findall(r"\d+", url)
    return nums[-1] if nums else ""


def dl_apply(opts):
    """Set gallery-dl's global options for a job. Idempotent and never calls config.clear()."""
    X = ("extractor",)
    out = os.path.abspath(opts["out"])
    os.makedirs(out, exist_ok=True)
    gdl_config.set(X, "base-directory", out)
    gdl_config.set(X, "archive", os.path.join(out, "archive.sqlite3") if opts.get("archive", True) else None)
    gdl_config.set(X, "sleep", [opts.get("sleep_min", 1.0), opts.get("sleep_max", 2.5)])
    gdl_config.set(X, "sleep-request", [0.4, 1.0])
    gdl_config.set(X, "retries", 3)
    gdl_config.set(X, "user-agent", opts.get("ua") or DEFAULT_UA)
    gdl_config.set(X, "cookies", opts.get("cookies") or None)
    for site, layout in LAYOUTS.items():
        gdl_config.set(("extractor", site), "directory", layout)
        gdl_config.set(("extractor", site), "filename", "{num:>03}.{extension}")
    for site in METADATA_SITES:
        gdl_config.set(("extractor", site), "postprocessors", [{
            "name": "metadata", "mode": "json",
            "event": "init",            # use "file" if no info.json appears on your version
            "filename": "info.json", "indent": 2, "ensure-ascii": False,
        }])


def dl_tag_sites():
    """{category: example URL containing TAG} for every tag-search extractor."""
    found = {}
    for cls in gdl_extractor.extractors():
        example = getattr(cls, "example", "") or ""
        if cls.subcategory == "tag" and "TAG" in example:
            found.setdefault(cls.category, example)
    return found


def dl_worker_sites(ready):
    dl_post(ready, "sites", dl_tag_sites())


def dl_build_tag_url(site, tags, tag_sites):
    rating = RATING_TAG.get(site, "")
    parts = ([rating] if rating else []) + list(tags)
    url = tag_sites[site].replace("TAG", quote_plus(" ".join(parts)))
    if gdl_extractor.find(url) is None:
        raise ValueError(f"no gallery-dl extractor matches {url}")
    return url


def dl_owned_codes(out):
    codes = set()
    if not out:
        return codes
    for p in glob.glob(os.path.join(out, "*", "*", "info.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            v = d.get("gallery_id") or d.get("id")
            if v:
                codes.add(str(v))
        except Exception:
            pass
    return codes


if HAVE_GDL:
    class Collector(gdl_job.Job):
        """Collects gallery URLs from a search page without downloading anything."""

        def __init__(self, url, limit, cancel):
            super().__init__(url)
            self.limit, self.cancel, self.urls = limit, cancel, []

        def handle_url(self, url, kwdict):
            pass

        def handle_directory(self, kwdict):
            pass

        def handle_queue(self, url, kwdict):
            if self.cancel.is_set():
                raise gdl_exc.StopExtraction()
            self.urls.append(url)
            if len(self.urls) >= self.limit:
                raise gdl_exc.StopExtraction()

    class InfoJob(gdl_job.Job):
        """Reads one gallery's metadata and the URL of its first image, then stops."""
        info = first = None

        def handle_directory(self, kwdict):
            if self.info is None:
                self.info = dict(kwdict)

        def handle_url(self, url, kwdict):
            self.first = url
            raise gdl_exc.StopExtraction()

        def handle_queue(self, url, kwdict):
            pass

    class CancelDownload(gdl_job.DownloadJob):
        """DownloadJob that can be stopped between files, honours a file limit, reports progress."""
        ready = cancel = iid = None
        limit = 0
        count = 0

        def handle_url(self, url, kwdict):
            if self.cancel.is_set() or (self.limit and self.count >= self.limit):
                raise gdl_exc.StopExtraction()
            super().handle_url(url, kwdict)
            self.count += 1
            pf = getattr(self, "pathfmt", None)
            path = getattr(pf, "realpath", None) or getattr(pf, "path", None)
            if path:
                self.dirs.add(os.path.dirname(path))
            dl_post(self.ready, "prog", self.iid, self.count, str(kwdict.get("filename", "")))


def dl_fetch_info(url):
    j = InfoJob(url)
    try:
        j.run()
    except Exception:
        pass
    d = j.info or {}
    tags = d.get("tags") or []
    info = {
        "code": str(d.get("gallery_id") or d.get("id") or dl_code(url)),
        "title": d.get("title") or d.get("title_en") or d.get("title_pretty") or "(unknown title)",
        "pages": d.get("count") or d.get("pages") or "?",
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [str(tags)],
    }
    return info, j.first


def dl_cover(img_url, referer, opts):
    """Download (or load from the on-disk cache) a thumbnail and centre it on a fixed-size tile."""
    tw, th = opts["thumb"]
    cache = os.path.join(COVER_DIR, hashlib.md5(img_url.encode("utf-8")).hexdigest() + ".jpg")
    im = None
    if os.path.isfile(cache):
        try:
            im = Image.open(cache)
            im.load()
        except Exception:
            im = None
    if im is None:
        import http.cookiejar
        import requests                     # installed together with gallery-dl
        sess = requests.Session()
        sess.headers.update({"User-Agent": opts.get("ua") or DEFAULT_UA, "Referer": referer})
        if opts.get("cookies"):
            try:
                jar = http.cookiejar.MozillaCookieJar()
                jar.load(opts["cookies"], ignore_discard=True, ignore_expires=True)
                sess.cookies.update(jar)
            except Exception:
                pass
        r = sess.get(img_url, timeout=20)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        im.thumbnail((tw, th), SMOOTH)
        os.makedirs(COVER_DIR, exist_ok=True)
        im.save(cache, "JPEG", quality=85)
    im = im.convert("RGB")
    if im.width > tw or im.height > th:
        im.thumbnail((tw, th), SMOOTH)
    tile = Image.new("RGB", (tw, th), opts.get("ph", "#222222"))
    tile.paste(im, ((tw - im.width) // 2, (th - im.height) // 2))
    return tile


def dl_worker_search(ready, token, cancel, opts, site, query, sort, page):
    dl_apply(opts)
    url = BOOK_SITES[site]["search"].format(query=quote_plus(query), sort=sort, page=page)
    if gdl_extractor.find(url) is None:
        dl_post(ready, "log", "gallery-dl can't handle " + url)
        dl_post(ready, "cards", token, page, [], set())
        dl_post(ready, "searchdone", token)
        return
    c = Collector(url, 200, cancel)
    try:
        c.run()
    except Exception as e:
        dl_post(ready, "log", f"Search error: {e}")
    cards = [(u, dl_code(u)) for u in c.urls]
    dl_post(ready, "cards", token, page, cards, dl_owned_codes(opts["out"]))
    for u, _code in cards:                       # details + covers stream in afterwards
        if cancel.is_set():
            return
        info, first = dl_fetch_info(u)
        dl_post(ready, "info", token, u, info)
        if opts.get("covers") and first and not cancel.is_set():
            try:
                dl_post(ready, "cover", token, u, dl_cover(first, u, opts))
            except Exception:
                pass
    dl_post(ready, "searchdone", token)


def dl_worker_item(ready, iid, cancel, opts, url, limit):
    """One queue entry = one gallery-dl job. Never raises."""
    if cancel.is_set():
        dl_post(ready, "item", iid, "stopped", 0, [])
        return
    dl_post(ready, "item", iid, "downloading", 0, [])
    try:
        dl_apply(opts)
        j = CancelDownload(url)
    except Exception as e:
        dl_post(ready, "item", iid, "failed", 0, f"unsupported or unreadable URL ({e})")
        return
    j.ready, j.cancel, j.iid, j.limit, j.dirs = ready, cancel, iid, limit, set()
    status = 0
    try:
        status = j.run()
    except Exception as e:
        dl_post(ready, "item", iid, "failed", j.count, str(e))
        return
    dirs = sorted(j.dirs, key=natural_key)
    if cancel.is_set():
        dl_post(ready, "item", iid, "stopped", j.count, dirs)
    elif status and not j.count:
        dl_post(ready, "item", iid, "failed", 0,
                f"gallery-dl status {status} (login needed, cookies missing, or blocked?)")
    else:
        dl_post(ready, "item", iid, "done", j.count, dirs)


# ------------------------------------------------------------ Discover & Download window
class Discover(tk.Frame):
    """Browse-and-download window: search bar, cover grid, download queue."""

    def __init__(self, app, parent):
        super().__init__(parent, bd=0, highlightthickness=0)
        self.app, self.S = app, app.S
        S = self.S
        cfg = app.cfg
        mode = cfg.get("dl_mode", "Search")
        self.mode = tk.StringVar(value=mode if mode in DL_MODES else "Search")
        self.site = tk.StringVar()
        self.query = tk.StringVar()
        self.sort = tk.StringVar(value=cfg.get("dl_sort", SORTS[0]))
        self.sfw = tk.BooleanVar(value=cfg.get("dl_sfw", True))
        self.hide_owned = tk.BooleanVar(value=cfg.get("dl_hide_owned", False))
        self.n_tag = tk.IntVar(value=15)
        self.n_url = tk.IntVar(value=0)
        self.tw, self.th = S(170), S(240)           # cover tile size
        self.blank = None
        self.cards, self.card_by_url = [], {}
        self.owned = set()
        self.items, self.next_id = {}, 1
        self.token = 0
        self.page = 1
        self.search_cancel = threading.Event()
        self._reflow_job = None
        self._build()
        self.retheme()
        self._mode_changed()
        self._update_dir_label()
        self._show_empty(HINTS[self.mode.get()])

    # ------------------------------------------------------------------ build
    def _build(self):
        S = self.S
        top = ttk.Frame(self, padding=(S(12), S(9)))
        top.pack(side="top", fill="x")
        ttk.Button(top, text="\u2699", style="Tool.TButton", width=3, command=self.open_settings).pack(side="right")
        ttk.Button(top, text="\u2605", style="Tool.TButton", width=3, command=self._saved_menu).pack(side="right", padx=(0, 4))
        self.go_btn = ttk.Button(top, text="Search", style="Accent.TButton", command=self._go)
        self.go_btn.pack(side="right", padx=S(8))
        ttk.Button(top, text="\u2190", style="Tool.TButton", width=3, command=self.close).pack(
            side="left", padx=(0, S(8)))
        seg = ttk.Frame(top)
        seg.pack(side="left")
        for m in DL_MODES:
            ttk.Radiobutton(seg, text=m, value=m, variable=self.mode, style="Toolbutton",
                            takefocus=False, command=self._mode_changed).pack(side="left")
        self.site_cb = ttk.Combobox(top, textvariable=self.site, state="readonly", width=14)
        self.site_cb.pack(side="left", padx=(S(10), 0))
        self.q_ent = ttk.Entry(top, textvariable=self.query)
        self.q_ent.pack(side="left", fill="x", expand=True, padx=(S(10), 0))
        self.q_ent.bind("<Return>", lambda e: self._go())

        fb = ttk.Frame(self, padding=(S(12), 0, S(12), S(8)))
        fb.pack(side="top", fill="x")
        rt = ttk.Frame(fb)
        rt.pack(side="right")
        self.dsel_btn = ttk.Button(rt, text="Download selected (0)", style="Accent.TButton",
                                   command=self._download_selected, state="disabled")
        self.dsel_btn.pack(side="right")
        ttk.Button(rt, text="Clear", style="Tool.TButton", command=self._select_none).pack(side="right", padx=(0, 6))
        ttk.Button(rt, text="Select all", style="Tool.TButton", command=self._select_all).pack(side="right", padx=(0, 6))
        lt = ttk.Frame(fb)
        lt.pack(side="left")
        self.sort_lbl = ttk.Label(lt, text="Sort", style="Dim.TLabel")
        self.sort_cb = ttk.Combobox(lt, textvariable=self.sort, state="readonly", width=13, values=SORTS)
        self.sfw_cb = ttk.Checkbutton(lt, text="SFW-filtered sites only", variable=self.sfw,
                                      command=self._mode_changed)
        self.lim_lbl = ttk.Label(lt, text="Max imgs", style="Dim.TLabel")
        self.lim_sp = ttk.Spinbox(lt, from_=0, to=500, width=5, textvariable=self.n_tag)
        self.hide_cb = ttk.Checkbutton(lt, text="Hide owned", variable=self.hide_owned, command=self._reflow)
        pager = ttk.Frame(lt)
        self.prev_btn = ttk.Button(pager, text="\u2039", width=3, style="Tool.TButton",
                                   command=lambda: self._turn(-1), state="disabled")
        self.prev_btn.pack(side="left")
        self.page_lbl = ttk.Label(pager, text="Page 1", style="Dim.TLabel")
        self.page_lbl.pack(side="left", padx=6)
        self.next_btn = ttk.Button(pager, text="\u203a", width=3, style="Tool.TButton",
                                   command=lambda: self._turn(1), state="disabled")
        self.next_btn.pack(side="left")
        self._ctl = [self.sort_lbl, self.sort_cb, self.sfw_cb, self.lim_lbl, self.lim_sp, self.hide_cb, pager]
        for i, w in enumerate(self._ctl):
            w.grid(row=0, column=i, padx=(0, S(10)))

        bot = ttk.Frame(self, padding=(S(12), S(4)))
        bot.pack(side="bottom", fill="x")
        ttk.Button(bot, text="Change\u2026", style="Tool.TButton", command=self.open_settings).pack(side="right")
        self.dir_lbl = ttk.Label(bot, style="Status.TLabel", anchor="w")
        self.dir_lbl.pack(side="right", padx=8)
        self.status_lbl = ttk.Label(bot, style="Status.TLabel", anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(side="top", fill="both", expand=True)
        left = ttk.Frame(pw)
        pw.add(left, weight=4)
        right = ttk.Frame(pw, padding=(S(8), 0, S(8), S(6)))
        pw.add(right, weight=1)

        self.vbar = ttk.Scrollbar(left, orient="vertical")
        self.vbar.pack(side="right", fill="y")
        self.cv = tk.Canvas(left, highlightthickness=0, bd=0, yscrollincrement=S(30),
                            yscrollcommand=self.vbar.set)
        self.vbar.configure(command=self.cv.yview)
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bd=0, highlightthickness=0)
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.cv.bind("<Configure>", lambda e: self._sched_reflow())
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.empty = tk.Label(left, text="", justify="center", font=self.app.f_sub, wraplength=S(460))

        ttk.Label(right, text="Download queue", style="Brand.TLabel").pack(side="top", anchor="w", pady=(S(2), S(4)))
        qb = ttk.Frame(right)
        qb.pack(side="top", fill="x", pady=(0, S(4)))
        for text, cmd in (("Stop", self._stop_sel), ("Stop all", self._stop_all),
                          ("Retry", self._retry), ("Clear done", self._clear_done), ("Open", self._open_sel)):
            ttk.Button(qb, text=text, style="Tool.TButton", command=cmd).pack(side="left", padx=(0, 3))
        self.log_txt = tk.Text(right, height=8, wrap="word", state="disabled", relief="flat", bd=0,
                               highlightthickness=0, font=self.app.f_small)
        self.log_txt.pack(side="bottom", fill="x", pady=(S(6), 0))
        wrap = ttk.Frame(right)
        wrap.pack(side="top", fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("state", "prog"), selectmode="extended")
        self.tree.heading("#0", text="Book", anchor="w")
        self.tree.heading("state", text="State")
        self.tree.heading("prog", text="Progress")
        self.tree.column("#0", width=S(150), stretch=True)
        self.tree.column("state", width=S(78), anchor="w", stretch=False)
        self.tree.column("prog", width=S(110), anchor="w", stretch=False)
        tsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._open_sel())

    def retheme(self):
        t = self.app.t
        self.configure(bg=t["bg"])
        self.cv.configure(bg=t["bg"])
        self.inner.configure(bg=t["bg"])
        self.empty.configure(bg=t["bg"], fg=t["dim"])
        self.log_txt.configure(bg=t["bg"], fg=t["fg"], insertbackground=t["fg"],
                               selectbackground=t["accent"], selectforeground=t["bg"])
        for tag, col in (("done", t["accent"]), ("downloading", t["accent2"]), ("failed", "#e06c75"),
                         ("stopped", t["dim"]), ("queued", t["fg"])):
            self.tree.tag_configure(tag, foreground=col)
        self.blank = ImageTk.PhotoImage(Image.new("RGB", (self.tw, self.th), t["ph"]))
        for c in self.cards:
            if not c["cover"]:
                c["img"].configure(image=self.blank)

    # ------------------------------------------------------------ small helpers
    def log(self, msg):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", msg + "\n")
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 300:
            self.log_txt.delete("1.0", f"{lines - 300}.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def set_status(self, text):
        self.status_lbl.configure(text=text)

    def _update_dir_label(self):
        d = self.app.cfg.get("dl_dir")
        self.dir_lbl.configure(text="Saving to: " + ((d if len(d) < 48 else "\u2026" + d[-47:]) if d else
                                                    "(you'll be asked on the first download)"))

    def _show_empty(self, text):
        self.empty.configure(text=text)
        self.empty.place(in_=self.cv, relx=0.5, rely=0.4, anchor="center")
        self.empty.lift()

    def _hide_empty(self):
        self.empty.place_forget()

    @staticmethod
    def _int(var, default):
        try:
            return max(0, int(var.get()))
        except (tk.TclError, ValueError):
            return default

    def _opts(self):
        c = self.app.cfg
        lo = float(c.get("dl_sleep_min", 1.0))
        hi = max(lo, float(c.get("dl_sleep_max", 2.5)))
        return dict(out=c.get("dl_dir") or "", cookies=(c.get("dl_cookies") or "").strip(),
                    ua=(c.get("dl_ua") or "").strip() or DEFAULT_UA, archive=c.get("dl_archive", True),
                    sleep_min=lo, sleep_max=hi, covers=c.get("dl_covers", True),
                    thumb=(self.tw, self.th), ph=self.app.t["ph"], tag_sites=dict(self.app.dl_tag_sites))

    # ------------------------------------------------------------------- modes
    def _mode_changed(self):
        m = self.mode.get()
        if m == "Search":
            sites = [x for x, v in BOOK_SITES.items() if "search" in v]
        elif m == "Codes":
            sites = [x for x, v in BOOK_SITES.items() if "code" in v]
        elif m == "Tags":
            pool = sorted(x for x in self.app.dl_tag_sites if not self.sfw.get() or x in RATING_TAG)
            sites = ([ALL_SITES] + pool) if pool else []
        else:
            sites = []
        self.site_cb.configure(values=sites, state="readonly" if sites else "disabled")
        if self.site.get() not in sites:
            self.site.set(sites[0] if sites else "")
        show = {
            self.sort_lbl: m == "Search", self.sort_cb: m == "Search",
            self.hide_cb: m == "Search", self._ctl[6]: m == "Search",
            self.sfw_cb: m == "Tags",
            self.lim_lbl: m in ("Tags", "URLs"), self.lim_sp: m in ("Tags", "URLs"),
        }
        for w, on in show.items():
            w.grid() if on else w.grid_remove()
        self.lim_lbl.configure(text="Max imgs" if m == "Tags" else "Max files")
        self.lim_sp.configure(textvariable=self.n_tag if m == "Tags" else self.n_url)
        self.go_btn.configure(text="Search" if m == "Search" else "Download")
        if not self.cards:
            self._show_empty(HINTS[m])

    def _go(self):
        m, q, site = self.mode.get(), self.query.get().strip(), self.site.get()
        if m == "Search":
            if not q:
                self.log("Type something to search for.")
                return
            if site and self.app.ensure_dl_dir():
                self._remember(m, site, q)
                self._search(1)
            return
        if not self.app.ensure_dl_dir():
            return
        if m == "Codes":
            codes = list(dict.fromkeys(re.findall(r"\d+", q)))
            if not codes or not site:
                self.log("No numeric codes found.")
                return
            for c in codes:
                self.enqueue(f"{site} #{c}", BOOK_SITES[site]["code"].format(code=c), code=c)
            self._remember(m, site, q)
        elif m == "Tags":
            if site == ALL_SITES:
                sites = [x for x in sorted(self.app.dl_tag_sites) if not self.sfw.get() or x in RATING_TAG]
            else:
                sites = [site] if site else []
            if not sites:
                self.log("No tag-search sites loaded yet.")
                return
            if any(x not in RATING_TAG for x in sites) and not messagebox.askyesno(
                    "Unfiltered sites", "Some selected sites have no rating filter and can return "
                                        "adult content. Continue?", parent=self):
                return
            tags = q.split()
            for x in sites:
                try:
                    url = dl_build_tag_url(x, tags, self.app.dl_tag_sites)
                except Exception as e:
                    self.log(f"{x}: {e}")
                    continue
                self.enqueue(f"{x}: {' '.join(tags) or 'latest'}", url, limit=self._int(self.n_tag, 15))
            self._remember(m, site, q)
        else:
            tokens = []
            for t in q.split():
                if os.path.isfile(t):
                    try:
                        with open(t, "r", encoding="utf-8") as f:
                            tokens += f.read().split()
                    except OSError:
                        self.log(f"Can't read {t}")
                else:
                    tokens.append(t)
            for u in dict.fromkeys(tokens):
                self.enqueue(u if len(u) < 60 else u[:57] + "\u2026", u, limit=self._int(self.n_url, 0))
            if not tokens:
                self.log("Paste at least one URL.")
            else:
                self._remember(m, site, q)

    # ------------------------------------------------------------------ search
    def _search(self, page):
        site, q, sort = self.site.get(), self.query.get().strip(), self.sort.get()
        self.search_cancel.set()
        self.search_cancel = cancel = threading.Event()
        self.token += 1
        self.page = page
        self._clear_cards()
        self._show_empty("Searching\u2026")
        self.set_status(f"Searching {site}\u2026")
        self.go_btn.configure(state="disabled")
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.app.info_pool.submit(dl_guard, dl_worker_search, self.app.ready, self.token, cancel,
                                  self._opts(), site, q, sort, page)

    def _turn(self, d):
        if self.page + d >= 1 and self.query.get().strip():
            self._search(self.page + d)

    # -------------------------------------------------------------- cards grid
    def _clear_cards(self):
        for c in self.cards:
            c["frame"].destroy()
        self.cards, self.card_by_url = [], {}
        self._update_sel()

    def _make_card(self, url, code, owned):
        S = self.S
        fr = ttk.Frame(self.inner, style="Card.TFrame", padding=S(6))
        img = ttk.Label(fr, image=self.blank, text="\u2026", compound="center", style="CardDim.TLabel")
        img.pack()
        title = ttk.Label(fr, text=("\u2713 " if owned else "") + f"#{code}", style="CardTitle.TLabel",
                          wraplength=self.tw, justify="left", anchor="w")
        title.pack(fill="x", pady=(S(5), 0))
        meta = ttk.Label(fr, text="loading\u2026", style="CardDim.TLabel", anchor="w")
        meta.pack(fill="x")
        card = dict(url=url, code=code, frame=fr, img=img, title=title, meta=meta, sel=False,
                    owned=owned, info=None, photo=None, cover=False)
        for w in (fr, img, title, meta):
            w.bind("<Button-1>", lambda e, c=card: self._toggle(c))
            w.bind("<Double-Button-1>", lambda e, c=card: self._download_cards([c]))
            w.bind("<Button-3>", lambda e, c=card: self._ctx(e, c))
            w.bind("<Button-2>", lambda e, c=card: self._ctx(e, c))
        self.cards.append(card)
        self.card_by_url[url] = card

    def _style_card(self, c):
        sel = "Sel" if c["sel"] else ""
        c["frame"].configure(style=f"Card{sel}.TFrame")
        c["img"].configure(style=f"CardDim{sel}.TLabel")
        c["title"].configure(style=f"CardTitle{sel}.TLabel")
        c["meta"].configure(style=f"CardDim{sel}.TLabel")

    def _toggle(self, c):
        c["sel"] = not c["sel"]
        self._style_card(c)
        self._update_sel()

    def _select_all(self):
        for c in self.cards:
            c["sel"] = True
            self._style_card(c)
        self._update_sel()

    def _select_none(self):
        for c in self.cards:
            c["sel"] = False
            self._style_card(c)
        self._update_sel()

    def _update_sel(self):
        n = sum(1 for c in self.cards if c["sel"])
        self.dsel_btn.configure(text=f"Download selected ({n})", state="normal" if n else "disabled")

    def _sched_reflow(self):
        if self._reflow_job:
            self.after_cancel(self._reflow_job)
        self._reflow_job = self.after(80, self._reflow)

    def _reflow(self):
        self._reflow_job = None
        cw = max(1, self.cv.winfo_width())
        cell = self.tw + self.S(12) + self.S(12)             # card padding + gaps
        cols = max(1, cw // cell)
        vis = [c for c in self.cards if not (self.hide_owned.get() and c["owned"])]
        for c in self.cards:
            c["frame"].grid_forget()
        for i, c in enumerate(vis):
            c["frame"].grid(row=i // cols, column=i % cols, padx=self.S(6), pady=self.S(6), sticky="n")
        self.cv.itemconfigure(self.win, width=cw)
        self.cv.configure(scrollregion=self.cv.bbox("all"))

    def _refresh_card(self, c):
        info = c["info"]
        mark = "\u2713 " if c["owned"] else ""
        title = info["title"] if info else f"#{c['code']}"
        if len(title) > 90:
            title = title[:87] + "\u2026"
        c["title"].configure(text=mark + title)
        if info:
            c["meta"].configure(text=f"#{info['code']}  \u00b7  {info['pages']} pg")

    def _ctx(self, e, c):
        m = self.app.themed_menu(self)
        m.add_command(label="Download", command=lambda: self._download_cards([c]))
        m.add_command(label="Select / deselect", command=lambda: self._toggle(c))
        m.add_separator()
        m.add_command(label="Open page in browser", command=lambda: webbrowser.open(c["url"]))
        m.add_command(label="Copy code", command=lambda: self._copy(c["code"]))
        m.add_command(label="Copy URL", command=lambda: self._copy(c["url"]))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    # ------------------------------------------------------------------- queue
    def _download_selected(self):
        self._download_cards([c for c in self.cards if c["sel"]])

    def _download_cards(self, cards):
        if not cards or not self.app.ensure_dl_dir():
            return
        for c in cards:
            info = c["info"]
            pages = info["pages"] if info else None
            self.enqueue(info["title"] if info else f"#{c['code']}", c["url"],
                         total=pages if isinstance(pages, int) else None, code=c["code"])

    def enqueue(self, title, url, total=None, limit=0, code=""):
        if not self.app.ensure_dl_dir():
            return
        iid = str(self.next_id)
        self.next_id += 1
        item = dict(id=iid, title=title, url=url, state="queued", done=0, total=total, limit=limit,
                    dirs=[], cancel=None, code=code)
        self.items[iid] = item
        self.tree.insert("", "end", iid=iid, text=title, values=("queued", ""), tags=("queued",))
        self._submit(item)
        self._queue_status()

    def _submit(self, item):
        item["cancel"] = threading.Event()
        item["state"], item["done"] = "queued", 0
        self.app.dl_pool.submit(dl_worker_item, self.app.ready, item["id"], item["cancel"],
                                self._opts(), item["url"], item["limit"])

    def _prog_text(self, it):
        if it["state"] == "queued":
            return ""
        if it["total"]:
            k = max(0, min(10, round(10 * it["done"] / it["total"])))
            return "\u2588" * k + "\u2591" * (10 - k) + f" {it['done']}/{it['total']}"
        return f"{it['done']} files" if it["done"] else ""

    def _set_item(self, iid, state, done):
        it = self.items.get(iid)
        if not it or not self.tree.exists(iid):
            return None
        it["state"], it["done"] = state, done
        self.tree.item(iid, values=(state, self._prog_text(it)), tags=(state,))
        return it

    def _queue_status(self):
        n_run = sum(1 for i in self.items.values() if i["state"] == "downloading")
        n_wait = sum(1 for i in self.items.values() if i["state"] == "queued")
        self.set_status(f"{n_run} downloading, {n_wait} queued" if (n_run or n_wait) else "Idle")

    def _sel_items(self):
        return [self.items[i] for i in self.tree.selection() if i in self.items]

    def _stop_sel(self):
        for it in self._sel_items():
            if it["cancel"] and it["state"] in ("queued", "downloading"):
                it["cancel"].set()

    def _stop_all(self):
        for it in self.items.values():
            if it["cancel"] and it["state"] in ("queued", "downloading"):
                it["cancel"].set()

    def _retry(self):
        sel = self._sel_items() or [i for i in self.items.values() if i["state"] in ("failed", "stopped")]
        for it in sel:
            if it["state"] in ("failed", "stopped"):
                self._set_item(it["id"], "queued", 0)
                self._submit(it)
        self._queue_status()

    def _clear_done(self):
        for iid, it in list(self.items.items()):
            if it["state"] in ("done", "failed", "stopped"):
                if self.tree.exists(iid):
                    self.tree.delete(iid)
                del self.items[iid]

    def _open_sel(self):
        for it in self._sel_items():
            dirs = [d for d in it["dirs"] if os.path.isdir(d)]
            if dirs:
                self.app.open_path(dirs[0])
                self.app.lift()
                return
        self.log("Select a finished download to open it.")

    # ------------------------------------------------------- saved / recent
    @staticmethod
    def _lab(s):
        return f"{s['mode']}: {s['query'] or '(latest)'}  [{s['site'] or '-'}]"

    def _current(self):
        return dict(mode=self.mode.get(), site=self.site.get(), query=self.query.get().strip(),
                    sort=self.sort.get())

    def _remember(self, mode, site, q):
        rec = [r for r in self.app.cfg.setdefault("dl_recent", [])
               if not (r["mode"] == mode and r["query"] == q and r["site"] == site)]
        rec.insert(0, dict(mode=mode, site=site, query=q, sort=self.sort.get()))
        self.app.cfg["dl_recent"] = rec[:15]

    def _apply(self, s):
        self.mode.set(s["mode"])
        self._mode_changed()
        if s["site"] in self.site_cb.cget("values"):
            self.site.set(s["site"])
        self.sort.set(s.get("sort", SORTS[0]))
        self.query.set(s["query"])
        self._go()

    def _saved_menu(self):
        cfg = self.app.cfg
        saved, recent = cfg.setdefault("dl_saved", []), cfg.setdefault("dl_recent", [])
        m = self.app.themed_menu(self)
        m.add_command(label="\u2605 Save this search", command=self._save_current)
        if saved:
            m.add_separator()
            for sv in saved:
                m.add_command(label=self._lab(sv), command=lambda sv=sv: self._apply(sv))
            rm = self.app.themed_menu(m)
            for sv in saved:
                rm.add_command(label=self._lab(sv), command=lambda sv=sv: self._unsave(sv))
            m.add_cascade(label="Remove saved", menu=rm)
        if recent:
            m.add_separator()
            for r in recent[:10]:
                m.add_command(label="\u21ba " + self._lab(r), command=lambda r=r: self._apply(r))
            m.add_command(label="Clear recent", command=lambda: cfg.__setitem__("dl_recent", []))
        try:
            m.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            m.grab_release()

    def _save_current(self):
        cur = self._current()
        saved = self.app.cfg.setdefault("dl_saved", [])
        if cur["query"] or cur["mode"] == "Tags":
            if cur not in saved:
                saved.append(cur)
                self.log("Saved: " + self._lab(cur))
        else:
            self.log("Type a query before saving it.")

    def _unsave(self, sv):
        saved = self.app.cfg.setdefault("dl_saved", [])
        if sv in saved:
            saved.remove(sv)

    # --------------------------------------------------------------- settings
    def open_settings(self):
        app, S = self.app, self.S
        c = app.cfg
        d = tk.Toplevel(self)
        d.title("Download settings")
        d.transient(self.winfo_toplevel())
        d.configure(bg=app.t["bg"])
        f = ttk.Frame(d, padding=S(14))
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        v_dir = tk.StringVar(value=c.get("dl_dir", ""))
        v_ck = tk.StringVar(value=c.get("dl_cookies", ""))
        v_ua = tk.StringVar(value=c.get("dl_ua", DEFAULT_UA))
        v_lo = tk.DoubleVar(value=c.get("dl_sleep_min", 1.0))
        v_hi = tk.DoubleVar(value=c.get("dl_sleep_max", 2.5))
        v_arch = tk.BooleanVar(value=c.get("dl_archive", True))
        v_cov = tk.BooleanVar(value=c.get("dl_covers", True))
        v_open = tk.BooleanVar(value=c.get("dl_autoopen", False))

        def row(r, text, widget_factory, browse=None):
            ttk.Label(f, text=text).grid(row=r, column=0, sticky="w", pady=4, padx=(0, S(10)))
            w = widget_factory()
            w.grid(row=r, column=1, sticky="ew", pady=4)
            if browse:
                ttk.Button(f, text="\u2026", width=3, command=browse).grid(row=r, column=2, padx=(S(4), 0))

        def browse_dir():
            p = filedialog.askdirectory(parent=d, initialdir=v_dir.get() or app.lib_root)
            if p:
                v_dir.set(p)

        def browse_ck():
            p = filedialog.askopenfilename(parent=d, title="cookies.txt exported from your browser",
                                           filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
            if p:
                v_ck.set(p)

        row(0, "Save folder", lambda: ttk.Entry(f, textvariable=v_dir, width=46), browse_dir)
        row(1, "Cookies file", lambda: ttk.Entry(f, textvariable=v_ck), browse_ck)
        row(2, "User-agent", lambda: ttk.Entry(f, textvariable=v_ua))
        sp = ttk.Frame(f)
        ttk.Spinbox(sp, from_=0, to=30, increment=0.5, width=5, textvariable=v_lo).pack(side="left")
        ttk.Label(sp, text="  to  ").pack(side="left")
        ttk.Spinbox(sp, from_=0, to=30, increment=0.5, width=5, textvariable=v_hi).pack(side="left")
        ttk.Label(sp, text="  seconds between files", style="Dim.TLabel").pack(side="left")
        row(3, "Delay", lambda: sp)
        ttk.Checkbutton(f, text="Skip files already downloaded (uses archive.sqlite3 in the save folder)",
                        variable=v_arch).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Fetch cover thumbnails for search results",
                        variable=v_cov).grid(row=5, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(f, text="Open a finished book in the reader if nothing is open",
                        variable=v_open).grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Label(f, style="Dim.TLabel", wraplength=S(480), justify="left",
                  text="Sites behind Cloudflare need cookies exported from your browser, and the "
                       "user-agent must be the same string your browser uses.").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))

        def num(var, default):
            try:
                return max(0.0, float(var.get()))
            except (tk.TclError, ValueError):
                return default

        def save():
            old = c.get("dl_dir")
            c["dl_dir"] = v_dir.get().strip()
            c["dl_cookies"] = v_ck.get().strip()
            c["dl_ua"] = v_ua.get().strip() or DEFAULT_UA
            c["dl_sleep_min"] = num(v_lo, 1.0)
            c["dl_sleep_max"] = max(c["dl_sleep_min"], num(v_hi, 2.5))
            c["dl_archive"], c["dl_covers"], c["dl_autoopen"] = v_arch.get(), v_cov.get(), v_open.get()
            if c["dl_dir"] and c["dl_dir"] != old:
                app.set_library_root(c["dl_dir"])
            self._update_dir_label()
            d.destroy()

        bb = ttk.Frame(f)
        bb.grid(row=8, column=0, columnspan=3, sticky="e", pady=(S(12), 0))
        ttk.Button(bb, text="Cancel", command=d.destroy).pack(side="right")
        ttk.Button(bb, text="Save", style="Accent.TButton", command=save).pack(side="right", padx=6)
        d.grab_set()

    # ------------------------------------------- messages from worker threads
    def on_msg(self, kind, *a):
        fn = getattr(self, "_on_" + kind, None)
        if fn:
            fn(*a)

    def _on_log(self, msg):
        self.log(msg)

    def _on_sites(self, sites):
        self._mode_changed()

    def _on_idle(self):
        self.go_btn.configure(state="normal")

    def _on_cards(self, token, page, cards, owned):
        if token != self.token:
            return
        self.owned = owned
        self.go_btn.configure(state="normal")
        for url, code in cards:
            self._make_card(url, code, code in owned)
        self.page_lbl.configure(text=f"Page {page}")
        self.prev_btn.configure(state="normal" if page > 1 else "disabled")
        self.next_btn.configure(state="normal" if cards else "disabled")
        if cards:
            self._hide_empty()
            self._reflow()
            self.set_status(f"{len(cards)} results \u2013 loading details\u2026")
        else:
            self._show_empty("No results, or the site blocked the request.\n"
                             "Try cookies + a matching user-agent in \u2699 settings.")
            self.set_status("No results")

    def _on_info(self, token, url, info):
        c = self.card_by_url.get(url)
        if token != self.token or not c:
            return
        c["info"] = info
        if info["code"] in self.owned:
            c["owned"] = True
        self._refresh_card(c)

    def _on_cover(self, token, url, im):
        c = self.card_by_url.get(url)
        if token != self.token or not c:
            return
        c["photo"] = ImageTk.PhotoImage(im)
        c["cover"] = True
        c["img"].configure(image=c["photo"], text="")

    def _on_searchdone(self, token):
        if token == self.token:
            self.set_status(f"{len(self.cards)} results  \u00b7  \u2713 = already in your library")
            self._reflow()

    def _on_prog(self, iid, count, name):
        self._set_item(iid, "downloading", count)
        self.set_status(f"Downloading\u2026 {name}")

    def _on_item(self, iid, state, done, extra):
        it = self._set_item(iid, state, done)
        if not it:
            return
        if state in ("done", "stopped"):
            it["dirs"] = list(extra or [])
        if state == "done":
            self.log(f"Done: {it['title']} ({done} files)" if done else
                     f"Nothing new: {it['title']} (already downloaded?)")
            card = self.card_by_url.get(it["url"])
            if card:
                card["owned"] = True
                self._refresh_card(card)
            self.app.populate_library()
            self.app.home_rescan()
            if self.app.cfg.get("dl_autoopen") and not self.app.paths and it["dirs"]:
                self.app.open_path(it["dirs"][0])
        elif state == "failed":
            self.log(f"Failed: {it['title']}: {extra}")
        elif state == "stopped":
            self.log(f"Stopped: {it['title']}")
        self._queue_status()

    # ------------------------------------------------------------ wheel & close
    def wheel(self, w, dy):
        step = int(dy * 3) or (1 if dy > 0 else -1)
        p = w
        while p is not None:
            if p in (self.tree, self.log_txt) or isinstance(p, tk.Listbox):
                p.yview_scroll(step, "units")
                return
            if p is self.cv or p is self.inner:
                self.cv.yview_scroll(step, "units")
                return
            p = getattr(p, "master", None)

    def save_state(self):
        self.app.cfg.update(dl_mode=self.mode.get(), dl_sort=self.sort.get(),
                            dl_sfw=self.sfw.get(), dl_hide_owned=self.hide_owned.get())

    def shutdown(self):
        self.search_cancel.set()
        for it in self.items.values():
            if it["cancel"]:
                it["cancel"].set()

    def close(self):
        self.app.home.back()     # state and running downloads stay alive; D / Explore brings it back


# --------------------------------------------------------------------- app
# ===================================================================== Home (Kotatsu-style)
def rrect(c, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle on a canvas (smoothed polygon)."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def draw_icon(c, name, cx, cy, s, col, tags=(), fill=False):
    """Tiny vector icons drawn with canvas primitives (no font or image dependency)."""
    w = max(1, round(s / 11))
    ln = dict(fill=col, width=w, capstyle="round", joinstyle="round", tags=tags)
    ol = dict(fill=col if fill else "", outline=col, width=w, tags=tags)
    if name == "search":
        r = s * 0.30
        c.create_oval(cx - r - s * 0.08, cy - r - s * 0.08, cx + r - s * 0.08, cy + r - s * 0.08, **ol)
        c.create_line(cx + s * 0.14, cy + s * 0.14, cx + s * 0.40, cy + s * 0.40, **ln)
    elif name == "history":
        r = s * 0.42
        c.create_arc(cx - r, cy - r, cx + r, cy + r, start=70, extent=290, style="arc", outline=col,
                     width=w, tags=tags)
        c.create_line(cx, cy - s * 0.2, cx, cy, cx + s * 0.16, cy + s * 0.1, **ln)
        c.create_line(cx + r * 0.34, cy - r * 0.94, cx + r * 0.34, cy - r * 0.5, cx + r * 0.78, cy - r * 0.72, **ln)
    elif name == "sd":
        pts = [-.30, -.44, .10, -.44, .30, -.24, .30, .44, -.30, .44]
        c.create_polygon([(cx + v * s) if i % 2 == 0 else (cy + v * s) for i, v in enumerate(pts)],
                         joinstyle="round", **ol)
        for dx in (-.14, 0.0, .14):
            c.create_line(cx + dx * s, cy - .34 * s, cx + dx * s, cy - .14 * s, **ln)
    elif name == "compass":
        r = s * 0.44
        c.create_oval(cx - r, cy - r, cx + r, cy + r, **dict(ol, fill=""))
        a, d = (cx + s * .24, cy - s * .24), (cx - s * .24, cy + s * .24)
        l, rr = (cx - s * .08, cy - s * .08), (cx + s * .08, cy + s * .08)
        c.create_polygon([*a, *l, *d, *rr], joinstyle="round", **dict(ol, fill=""))
        c.create_polygon([*a, *l, *rr], fill=col, outline=col, tags=tags)
    elif name == "heart":
        pts = []
        for i in range(40):
            t = 2 * math.pi * i / 40
            x = 16 * math.sin(t) ** 3
            y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
            pts += [cx + x * s / 36, cy + y * s / 36 - s * 0.04]
        c.create_polygon(pts, smooth=True, **ol)
    elif name == "bookmark":
        c.create_polygon(cx - s * .26, cy - s * .42, cx + s * .26, cy - s * .42, cx + s * .26, cy + s * .42,
                         cx, cy + s * .2, cx - s * .26, cy + s * .42, joinstyle="round", **ol)
    elif name == "dice":
        r = s * 0.40
        rrect(c, cx - r, cy - r, cx + r, cy + r, s * 0.14, fill="", outline=col, width=w, tags=tags)
        for dx, dy in ((-.2, -.2), (.2, -.2), (0, 0), (-.2, .2), (.2, .2)):
            c.create_oval(cx + dx * s - w, cy + dy * s - w, cx + dx * s + w, cy + dy * s + w,
                          fill=col, outline=col, tags=tags)
    elif name == "download":
        c.create_line(cx, cy - s * .40, cx, cy + s * .18, **ln)
        c.create_line(cx - s * .2, cy, cx, cy + s * .22, cx + s * .2, cy, **ln)
        c.create_line(cx - s * .38, cy + s * .42, cx + s * .38, cy + s * .42, **ln)
    elif name == "dots":
        for dy in (-.3, 0, .3):
            c.create_oval(cx - w, cy + dy * s - w, cx + w, cy + dy * s + w, fill=col, outline=col, tags=tags)
    elif name == "check":
        c.create_line(cx - s * .3, cy, cx - s * .08, cy + s * .22, cx + s * .32, cy - s * .22, **ln)
    elif name == "back":
        c.create_line(cx + s * .2, cy - s * .3, cx - s * .2, cy, cx + s * .2, cy + s * .3, **ln)


def round_cover(im, r, bg):
    """Rounded corners, flattened onto the page colour (no alpha needed)."""
    w, h = im.size
    ss = 3
    m = Image.new("L", (w * ss, h * ss), 0)
    try:
        ImageDraw.Draw(m).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r * ss, fill=255)
    except AttributeError:
        ImageDraw.Draw(m).rectangle((0, 0, w * ss - 1, h * ss - 1), fill=255)
    m = m.resize((w, h), SMOOTH)
    out = Image.new("RGB", (w, h), bg)
    out.paste(im, (0, 0), m)
    return out


def darken_bottom(im):
    w, h = im.size
    try:
        mask = Image.linear_gradient("L").resize((w, h)).point(lambda v: int(max(0, v - 80) * 1.2))
        return Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), im, mask)
    except Exception:
        return im


def first_image(folder, depth=2):
    imgs = list_images(folder)
    if imgs:
        return imgs[0]
    if depth <= 0:
        return None
    try:
        subs = sorted((d for d in os.listdir(folder)
                       if not d.startswith(".") and os.path.isdir(os.path.join(folder, d))), key=natural_key)
    except OSError:
        return None
    for d in subs[:20]:
        p = first_image(os.path.join(folder, d), depth - 1)
        if p:
            return p
    return None


def wrap_lines(text, font, width, lines=2):
    """Word-wrap into at most `lines` lines, ellipsising what doesn't fit."""
    text = " ".join(str(text).split())
    out = []
    while text and len(out) < lines:
        if font.measure(text) <= width:
            out.append(text)
            text = ""
            break
        k = len(text)
        while k > 1 and font.measure(text[:k]) > width:
            k -= 1
        cut = text.rfind(" ", 0, k + 1)
        if cut > k * 0.4:
            k = cut
        out.append(text[:k].rstrip())
        text = text[k:].lstrip()
    if text and out:
        last = out[-1]
        while last and font.measure(last + "\u2026") > width:
            last = last[:-1]
        out[-1] = last.rstrip() + "\u2026"
    return out


def home_cover_job(ready, gid, key, folder, w, h, r, bg, banner=False):
    """Worker: cover thumbnail for a local book (first page, cropped to fill, cached on disk)."""
    try:
        path = first_image(folder)
        if not path:
            ready.put(("hcover", gid, key, None))
            return
        st = os.stat(path)
        tag = f"{path}|{st.st_mtime_ns}|{st.st_size}|{w}x{h}|{int(banner)}"
        cache = os.path.join(COVER_DIR, "loc_" + hashlib.md5(tag.encode("utf-8", "replace")).hexdigest() + ".jpg")
        im = None
        if os.path.isfile(cache):
            try:
                im = Image.open(cache)
                im.load()
            except Exception:
                im = None
        if im is None:
            with Image.open(path) as src:
                src.draft("RGB", (w * 2, h * 2))
                im = ImageOps.fit(src.convert("RGB"), (w, h), SMOOTH, centering=(0.5, 0.0))
            if banner:
                im = darken_bottom(im)
            try:
                os.makedirs(COVER_DIR, exist_ok=True)
                im.save(cache, "JPEG", quality=85)
            except OSError:
                pass
        ready.put(("hcover", gid, key, round_cover(im.convert("RGB"), r, bg)))
    except Exception:
        ready.put(("hcover", gid, key, None))


def home_scan(ready, token, root, max_depth=4, limit=6000):
    """Worker: every folder that directly contains images is a 'book'."""
    books, stack, seen = [], [(root, 0)], 0
    while stack and seen < limit:
        path, depth = stack.pop()
        seen += 1
        n, subs = 0, []
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            subs.append(e.path)
                        elif os.path.splitext(e.name)[1].lower() in EXTS:
                            n += 1
                    except OSError:
                        pass
        except OSError:
            continue
        if n and path != root:
            try:
                mt = os.stat(path).st_mtime
            except OSError:
                mt = 0
            par = os.path.dirname(path)
            books.append(dict(path=path, title=os.path.basename(path), n=n, mtime=mt,
                              parent=os.path.basename(par) if par != root else ""))
        if depth < max_depth:
            stack.extend((d, depth + 1) for d in subs)
    ready.put(("hlib", token, root, books))


def hist_ts(h):
    ts = h.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    now = datetime.now()
    for y in (now.year, now.year - 1):
        try:
            d = datetime.strptime(f"{y} {h.get('time', '')}", "%Y %b %d %H:%M")
            if d <= now:
                return d.timestamp()
        except ValueError:
            pass
    return None


def day_label(ts):
    if ts is None:
        return "Earlier"
    d, today = datetime.fromtimestamp(ts).date(), datetime.now().date()
    n = (today - d).days
    if n <= 0:
        return "Today"
    if n == 1:
        return "Yesterday"
    if n < 7:
        return f"{n} days ago"
    return d.strftime("%b %d") if d.year == today.year else d.strftime("%b %d, %Y")


def source_color(name):
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:4], 16) / 65535.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.62, 0.9)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


class Scroller(tk.Frame):
    """Vertically scrolling frame: canvas + inner frame + slim scrollbar."""

    def __init__(self, parent, app, on_scroll=None):
        super().__init__(parent, bd=0, highlightthickness=0)
        self.app, self.S, self.on_scroll, self.on_resize = app, app.S, on_scroll, None
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.vbar.pack(side="right", fill="y")
        self.cv = tk.Canvas(self, highlightthickness=0, bd=0, yscrollincrement=app.S(30),
                            yscrollcommand=self._yset)
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bd=0, highlightthickness=0)
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.cv.bind("<Configure>", self._cv_conf)
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))

    def _yview(self, *a):
        self.cv.yview(*a)

    def _yset(self, a, b):
        self.vbar.set(a, b)
        if self.on_scroll:
            self.on_scroll()

    def _cv_conf(self, e):
        self.cv.itemconfigure(self.win, width=e.width)
        if self.on_resize:
            self.on_resize(e.width)

    def wheel(self, dy):
        self.cv.yview_scroll(int(dy * 3) or (1 if dy > 0 else -1), "units")

    def to_top(self):
        self.cv.yview_moveto(0)

    def recolor(self, bg):
        self.configure(bg=bg)
        self.cv.configure(bg=bg)
        self.inner.configure(bg=bg)


class CoverGrid(Scroller):
    """Responsive grid of cover cards (+ optional section headers). Covers load lazily."""

    def __init__(self, parent, app, gid, on_open, on_menu):
        super().__init__(parent, app, on_scroll=self._sched_load)
        S = app.S
        self.gid, self.on_open, self.on_menu = gid, on_open, on_menu
        self.tw, self.th, self.radius, self.gap = S(156), S(222), S(12), S(14)
        self.ch = self.th + S(7) + 2 * S(17) + S(20)
        self.cards, self.layout, self.entries, self._empty = {}, [], [], None
        self.bg = app.t["bg"]
        self.blank = None
        self.pad = tk.Frame(self.inner, height=S(24), bd=0, highlightthickness=0)
        self.empty = tk.Frame(self, bd=0, highlightthickness=0)
        self.empty_lbl = tk.Label(self.empty, justify="center", font=app.f_sub, wraplength=S(420))
        self.empty_lbl.pack()
        self.empty_btn = ttk.Button(self.empty, text="")
        self.on_resize = lambda w: self._sched_reflow()
        self._reflow_job = self._load_job = None

    def retheme(self):
        t = self.app.t
        self.bg = t["bg"]
        self.recolor(self.bg)
        self.pad.configure(bg=self.bg)
        self.empty.configure(bg=self.bg)
        self.empty_lbl.configure(bg=self.bg, fg=t["dim"])
        self.blank = ImageTk.PhotoImage(round_cover(Image.new("RGB", (self.tw, self.th), t["ph"]),
                                                    self.radius, self.bg))
        if self._empty is not None:
            self.set_items(self.entries, *self._empty)

    def set_items(self, entries, empty_text="", action=None):
        S, t, app = self.S, self.app.t, self.app
        self.entries, self._empty = entries, (empty_text, action)
        for w in self.inner.winfo_children():
            if w is not self.pad:
                w.destroy()
        self.cards, self.layout = {}, []
        for e in entries:
            if "header" in e:
                lb = tk.Label(self.inner, text=e["header"], font=app.f_h, bg=self.bg, fg=t["fg"], anchor="w")
                self.layout.append(("h", lb))
            else:
                self.layout.append(("c", self._make_card(e)))
        if entries:
            self.empty.place_forget()
        else:
            self.empty_lbl.configure(text=empty_text)
            if action:
                self.empty_btn.configure(text=action[0], command=action[1])
                self.empty_btn.pack(pady=(S(10), 0))
            else:
                self.empty_btn.pack_forget()
            self.empty.place(relx=0.5, rely=0.4, anchor="center")
            self.empty.lift()
        self._reflow()
        self._sched_load()

    def _make_card(self, e):
        S, t, app = self.S, self.app.t, self.app
        c = tk.Canvas(self.inner, width=self.tw, height=self.ch, bg=self.bg, highlightthickness=0, bd=0,
                      cursor="hand2")
        img = c.create_image(0, 0, image=self.blank, anchor="nw")
        pct = e.get("pct")
        if pct:
            x1, x2, y = S(12), self.tw - S(12), self.th - S(10)
            c.create_rectangle(x1, y, x2, y + S(3), fill="#555555", outline="")
            c.create_rectangle(x1, y, x1 + (x2 - x1) * min(100, pct) / 100.0, y + S(3),
                               fill=t["accent"], outline="")
        if e.get("fav"):
            rrect(c, S(6), S(6), S(34), S(34), S(9), fill="#141418", outline="")
            draw_icon(c, "heart", S(20), S(20), S(16), "#ff5c7a", fill=True)
        if e.get("missing"):
            c.create_text(self.tw // 2, self.th // 2, text="missing", fill=t["dim"], font=app.f_sub)
        lines = wrap_lines(e["title"], app.f_card, self.tw - S(2), 2)
        c.create_text(S(1), self.th + S(7), text="\n".join(lines), anchor="nw", font=app.f_card, fill=t["fg"])
        if e.get("sub"):
            s1 = wrap_lines(e["sub"], app.f_small, self.tw, 1)
            c.create_text(S(1), self.th + S(7) + 2 * S(17), text=s1[0] if s1 else "", anchor="nw",
                          font=app.f_small, fill=t["dim"])
        key = e["key"]
        c.bind("<Button-1>", lambda ev, k=key: self.on_open(k))
        c.bind("<Button-3>", lambda ev, k=key: self.on_menu(ev, k))
        c.bind("<Button-2>", lambda ev, k=key: self.on_menu(ev, k))
        self.cards[key] = dict(cv=c, img=img, path=e.get("path"), req=False, photo=None)
        return c

    def _sched_reflow(self):
        if getattr(self, "_reflow_job", None):
            self.after_cancel(self._reflow_job)
        self._reflow_job = self.after(60, self._reflow)

    def _reflow(self):
        self._reflow_job = None
        S = self.S
        cw = max(1, self.cv.winfo_width())
        cell = self.tw + self.gap
        cols = max(1, (cw - self.gap) // cell)
        x0 = max(self.gap, (cw - (cols * cell - self.gap)) // 2)
        for w in self.inner.grid_slaves():
            w.grid_forget()
        row = col = 0
        for kind, w in self.layout:
            if kind == "h":
                if col:
                    row, col = row + 1, 0
                w.grid(row=row, column=0, columnspan=cols, sticky="w", padx=(x0, 0), pady=(S(14), S(8)))
                row += 1
            else:
                w.grid(row=row, column=col, sticky="n", padx=(x0 if col == 0 else self.gap, 0), pady=(0, S(8)))
                col += 1
                if col >= cols:
                    row, col = row + 1, 0
        if col:
            row += 1
        self.pad.grid(row=row, column=0, columnspan=cols, sticky="ew")
        self._sched_load()

    def _sched_load(self):
        if getattr(self, "_load_job", None):
            self.after_cancel(self._load_job)
        self._load_job = self.after(70, self._load_visible)

    def _load_visible(self):
        self._load_job = None
        if not self.winfo_ismapped():
            return
        top = self.cv.canvasy(0)
        view = self.cv.winfo_height()
        for key, c in self.cards.items():
            if c["req"] or not c["path"]:
                continue
            y = c["cv"].winfo_y()
            if y + self.ch >= top - view and y <= top + 2 * view:
                c["req"] = True
                self.app.io_pool.submit(home_cover_job, self.app.ready, self.gid, key, c["path"],
                                        self.tw, self.th, self.radius, self.bg)

    def on_cover(self, key, im):
        c = self.cards.get(key)
        if c is None or im is None:
            return
        c["photo"] = ImageTk.PhotoImage(im)
        c["cv"].itemconfigure(c["img"], image=c["photo"])


class SearchBar(tk.Canvas):
    """Rounded 'Search manga' pill with a placeholder, a search icon and a three-dot menu."""

    def __init__(self, parent, app, placeholder, on_enter=None, on_change=None, on_menu=None):
        super().__init__(parent, height=app.S(54), highlightthickness=0, bd=0)
        self.app, self.S = app, app.S
        self.placeholder, self.on_enter, self.on_change, self.on_menu = placeholder, on_enter, on_change, on_menu
        self._ph = True
        self.entry = tk.Entry(self, relief="flat", bd=0, highlightthickness=0, font=app.f_sub)
        self.win = self.create_window(0, 0, window=self.entry, anchor="w")
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.entry.bind("<Return>", lambda e: self.on_enter(self.text()) if self.on_enter else None)
        self.entry.bind("<KeyRelease>", self._typed)
        self.entry.bind("<Escape>", lambda e: (self.winfo_toplevel().focus_set(), "break")[1])
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)
        self._set_placeholder()

    def text(self):
        return "" if self._ph else self.entry.get().strip()

    def set_text(self, s):
        if s:
            self._ph = False
            self.entry.delete(0, "end")
            self.entry.insert(0, s)
        else:
            self._set_placeholder()
        self.redraw()

    def _set_placeholder(self):
        self._ph = True
        self.entry.delete(0, "end")
        self.entry.insert(0, self.placeholder)

    def _focus_in(self, e):
        if self._ph:
            self.entry.delete(0, "end")
            self._ph = False
            self.redraw()

    def _focus_out(self, e):
        if not self.entry.get().strip():
            self._set_placeholder()
            self.redraw()

    def _typed(self, e):
        if self.on_change and not self._ph:
            self.on_change(self.text())

    def _click(self, e):
        if self.on_menu and e.x > self.winfo_width() - self.S(56):
            self.on_menu(e)
        else:
            self.entry.focus_set()

    def redraw(self):
        t, S = self.app.t, self.S
        w, h = max(self.winfo_width(), S(200)), S(54)
        pill = mix(t["bg"], t["fg"], 0.12)
        self.configure(bg=t["bg"])
        self.delete("deco")
        rrect(self, 1, S(3), w - 1, h - S(3), (h - S(6)) // 2, fill=pill, outline="", tags="deco")
        draw_icon(self, "search", S(32), h // 2, S(22), t["dim"], tags="deco")
        draw_icon(self, "dots", w - S(30), h // 2, S(22), t["dim"], tags="deco", fill=True)
        self.coords(self.win, S(56), h // 2)
        self.itemconfigure(self.win, width=max(S(40), w - S(56) - S(60)))
        self.tag_lower("deco")
        self.entry.configure(bg=pill, fg=t["dim"] if self._ph else t["fg"], insertbackground=t["fg"],
                             selectbackground=t["accent"], selectforeground=t["bg"])


class ChipBar(tk.Canvas):
    """Single-choice rounded filter chips (the 'On device / Completed / favourites' row)."""

    def __init__(self, parent, app, chips, on_select):
        super().__init__(parent, height=app.S(46), highlightthickness=0, bd=0)
        self.app, self.S, self.chips, self.on_select = app, app.S, chips, on_select
        self.cur, self.hits = chips[0][0], []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)

    def set_current(self, key):
        self.cur = key
        self.redraw()

    def _click(self, e):
        for x1, x2, key in self.hits:
            if x1 <= e.x <= x2:
                self.cur = key
                self.redraw()
                self.on_select(key)
                return

    def redraw(self):
        t, S, app = self.app.t, self.S, self.app
        self.configure(bg=t["bg"])
        self.delete("all")
        self.hits = []
        x, y, h = S(14), S(4), S(38)
        for key, label, icon in self.chips:
            sel = key == self.cur
            tw = app.f_sub.measure(label)
            iw = S(24) if (sel or icon) else 0
            w = tw + iw + S(30)
            rrect(self, x, y, x + w, y + h, h // 2, fill=mix(t["bg"], t["fg"], 0.22) if sel else "",
                  outline=mix(t["bg"], t["fg"], 0.38), width=1)
            if sel:
                draw_icon(self, "check", x + S(24), y + h // 2, S(18), t["fg"])
            elif icon:
                draw_icon(self, icon, x + S(24), y + h // 2, S(18), t["fg"])
            self.create_text(x + S(15) + iw, y + h // 2, text=label, anchor="w", font=app.f_sub, fill=t["fg"])
            self.hits.append((x, x + w, key))
            x += w + S(10)


class NavPill(tk.Canvas):
    """The floating History / Library / Explore pill."""
    ITEMS = (("history", "history"), ("library", "sd"), ("explore", "compass"))

    def __init__(self, parent, app, on_select):
        S = app.S
        cell = S(66)
        super().__init__(parent, width=3 * cell + S(26), height=S(64), highlightthickness=0, bd=0)
        self.cell = cell
        self.app, self.S, self.on_select, self.cur = app, S, on_select, "library"
        self.bind("<Button-1>", self._click)

    def set_current(self, name):
        self.cur = name
        self.redraw()

    def _click(self, e):
        i = int((e.x - self.S(13)) // self.cell)
        if 0 <= i < 3:
            self.on_select(self.ITEMS[i][0])

    def redraw(self):
        t, S = self.app.t, self.S
        w, h = 3 * self.cell + S(26), S(64)
        self.configure(bg=t["bg"], width=w, height=h)
        self.delete("all")
        rrect(self, 1, 1, w - 1, h - 1, h // 2, fill=mix(t["bg"], t["fg"], 0.14),
              outline=mix(t["bg"], t["fg"], 0.2))
        for i, (name, icon) in enumerate(self.ITEMS):
            cx, cy = S(13) + self.cell * i + self.cell // 2, h // 2
            on = name == self.cur
            if on:
                r = S(26)
                self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=mix(t["bg"], t["fg"], 0.34), outline="")
            draw_icon(self, icon, cx, cy, S(28), t["fg"] if on else t["dim"])


class Tiles(tk.Canvas):
    """The 2x2 quick-action tiles (Local storage / Bookmarks / Random / Downloads)."""

    def __init__(self, parent, app, specs, on_click):
        super().__init__(parent, height=app.S(2 * 58 + 10), highlightthickness=0, bd=0)
        self.app, self.S, self.specs, self.on_click, self.hits = app, app.S, specs, on_click, []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)

    def _click(self, e):
        for x1, y1, x2, y2, key in self.hits:
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                self.on_click(key)
                return

    def redraw(self):
        t, S, app = self.app.t, self.S, self.app
        self.configure(bg=t["bg"])
        self.delete("all")
        self.hits = []
        w = max(self.winfo_width(), S(300))
        gap, th = S(10), S(56)
        tw = (w - gap) // 2
        for i, (key, label, icon) in enumerate(self.specs):
            r, c = divmod(i, 2)
            x1, y1 = c * (tw + gap), r * (th + gap)
            rrect(self, x1, y1, x1 + tw, y1 + th, S(18), fill=mix(t["bg"], t["fg"], 0.12), outline="")
            draw_icon(self, icon, x1 + S(32), y1 + th // 2, S(24), t["fg"])
            self.create_text(x1 + S(62), y1 + th // 2, text=label, anchor="w", font=app.f_sub, fill=t["fg"])
            self.hits.append((x1, y1, x1 + tw, y1 + th, key))


class Carousel(tk.Canvas):
    """'Suggestions': big rounded banners; click a neighbour (or a dot) to move along."""
    CW, CH = 520, 240

    def __init__(self, parent, app, on_open):
        super().__init__(parent, height=app.S(self.CH + 40), highlightthickness=0, bd=0)
        self.app, self.S, self.on_open = app, app.S, on_open
        self.items, self.idx, self.photos, self.req, self.hits = [], 0, {}, set(), []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._click)

    def set_items(self, items):
        same = [i["path"] for i in items] == [i["path"] for i in self.items]
        self.items = items
        self.idx = min(self.idx, max(0, len(items) - 1))
        if not same:
            self.photos, self.req = {}, set()
        self.redraw()

    def on_cover(self, key, im):
        if im is not None:
            self.photos[key] = ImageTk.PhotoImage(im)
            self.redraw()

    def _click(self, e):
        for x1, y1, x2, y2, kind, i in self.hits:
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                if kind == "dot" or i != self.idx:
                    self.idx = i
                    self.redraw()
                else:
                    self.on_open(self.items[i]["path"])
                return

    def redraw(self):
        t, S, app = self.app.t, self.S, self.app
        self.configure(bg=t["bg"])
        self.delete("all")
        self.hits = []
        w = self.winfo_width()
        cw, ch, y0 = S(self.CW), S(self.CH), S(6)
        if not self.items:
            self.create_text(max(w, S(300)) // 2, y0 + ch // 2, text="Nothing to suggest yet.\n"
                             "Pick your library folder and your unread books show up here.",
                             fill=t["dim"], font=app.f_sub, justify="center")
            return
        for i in range(max(0, self.idx - 1), min(len(self.items), self.idx + 3)):
            x1 = S(16) + (i - self.idx) * (cw + S(12))
            if x1 > w or x1 + cw < 0:
                continue
            it = self.items[i]
            ph = self.photos.get(it["path"])
            if ph is not None:
                self.create_image(x1, y0, image=ph, anchor="nw")
            else:
                rrect(self, x1, y0, x1 + cw, y0 + ch, S(24), fill=mix(t["bg"], t["accent"], 0.22), outline="")
                if it["path"] not in self.req:
                    self.req.add(it["path"])
                    app.io_pool.submit(home_cover_job, app.ready, "banner", it["path"], it["path"],
                                       cw, ch, S(24), t["bg"], True)
            tc = "#ffffff" if ph is not None else t["fg"]
            self.create_text(x1 + S(22), y0 + ch - S(52), text=wrap_lines(it["title"], app.f_h, cw - S(44), 1)[0],
                             anchor="w", font=app.f_h, fill=tc)
            self.create_text(x1 + S(22), y0 + ch - S(26), text=it["sub"], anchor="w", font=app.f_small,
                             fill="#d6d6d6" if ph is not None else t["dim"])
            self.hits.append((x1, y0, x1 + cw, y0 + ch, "card", i))
        n, dy = len(self.items), y0 + ch + S(20)
        x = (w - (n - 1) * S(16)) // 2
        for i in range(n):
            on = i == self.idx
            r = S(4) if on else S(3)
            self.create_oval(x - r, dy - r, x + r, dy + r, fill=t["fg"] if on else t["border"], outline="")
            self.hits.append((x - S(8), dy - S(8), x + S(8), dy + S(8), "dot", i))
            x += S(16)


class HistoryPage(tk.Frame):
    def __init__(self, parent, home):
        super().__init__(parent, bd=0, highlightthickness=0)
        self.home, self.app = home, home.app
        S = self.app.S
        self._job = None
        self.bar = SearchBar(self, self.app, "Search history", on_change=self._changed, on_menu=self._menu)
        self.bar.pack(fill="x", padx=S(14), pady=(S(14), S(6)))
        self.cg = CoverGrid(self, self.app, "history", self._open, self._ctx)
        self.cg.pack(fill="both", expand=True)

    def retheme(self):
        self.configure(bg=self.app.t["bg"])
        self.bar.redraw()
        self.cg.retheme()
        self.refresh()

    def _changed(self, text):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(200, self.refresh)

    def refresh(self):
        self._job = None
        cfg, q = self.app.cfg, self.bar.text().lower()
        favs, read = set(cfg.get("favs", [])), cfg["read"]
        entries, last = [], None
        for h in cfg["history"]:
            p = h.get("path", "")
            title = os.path.basename(p) or p
            par = os.path.basename(os.path.dirname(p))
            if q and q not in title.lower() and q not in par.lower():
                continue
            label = day_label(hist_ts(h))
            if label != last:
                entries.append({"header": label})
                last = label
            entries.append(dict(key=p, title=title, sub=par, path=p, pct=read.get(p), fav=p in favs,
                                missing=not os.path.isdir(p)))
        self.cg.set_items(entries, "Nothing here yet.\nBooks you read show up in this list." if not q
                          else "No history matches your search.")

    def _open(self, path):
        if os.path.isdir(path):
            self.app.open_path(path)
        else:
            self.app.status.config(text=f"Folder not found: {path}")

    def _ctx(self, e, path):
        app = self.app
        m = app.themed_menu(self)
        m.add_command(label="Open", command=lambda: self._open(path))
        m.add_command(label="Remove from favourites" if path in app.cfg.get("favs", []) else "Add to favourites",
                      command=lambda: self.home.toggle_fav(path))
        m.add_command(label="Show in file manager", command=lambda: app._reveal(path))
        m.add_separator()
        m.add_command(label="Remove from history", command=lambda: self._remove(path))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _remove(self, path):
        self.app.cfg["history"] = [h for h in self.app.cfg["history"] if h.get("path") != path]
        self.refresh()

    def _menu(self, e):
        m = self.app.themed_menu(self)
        m.add_command(label="Clear history", command=self._clear)
        m.add_command(label="Remove missing folders", command=self._prune)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _clear(self):
        if self.app.cfg["history"] and messagebox.askyesno("Clear history", "Clear all reading history?"):
            self.app.cfg["history"] = []
            self.refresh()

    def _prune(self):
        self.app.cfg["history"] = [h for h in self.app.cfg["history"] if os.path.isdir(h.get("path", ""))]
        self.refresh()

    def wheel(self, w, dy):
        self.cg.wheel(dy)


class LibraryPage(tk.Frame):
    CHIPS = (("all", "On device", None), ("progress", "In progress", None),
             ("done", "Completed", None), ("fav", "Favourites", "heart"))

    def __init__(self, parent, home):
        super().__init__(parent, bd=0, highlightthickness=0)
        self.home, self.app = home, home.app
        S = self.app.S
        self._job = None
        self.group_var = tk.BooleanVar(value=self.app.cfg.get("lib_group", False))
        self.bar = SearchBar(self, self.app, "Search library", on_change=self._changed, on_menu=self._menu)
        self.bar.pack(fill="x", padx=S(14), pady=(S(14), S(2)))
        self.chips = ChipBar(self, self.app, self.CHIPS, self._chip)
        self.chips.set_current(self.app.cfg.get("lib_chip", "all"))
        self.chips.pack(fill="x", pady=(0, S(2)))
        self.cg = CoverGrid(self, self.app, "library", self._open, self._ctx)
        self.cg.pack(fill="both", expand=True)
        self.units = {}

    def retheme(self):
        self.configure(bg=self.app.t["bg"])
        self.bar.redraw()
        self.chips.redraw()
        self.cg.retheme()
        self.refresh()

    def set_chip(self, key):
        self.chips.set_current(key)
        self._chip(key)

    def _chip(self, key):
        self.app.cfg["lib_chip"] = key
        self.refresh()

    def _changed(self, text):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(200, self.refresh)

    # ---- model: books (leaf folders) or series (folders of chapters)
    def _units(self):
        cfg, root = self.app.cfg, os.path.abspath(self.app.lib_root)
        favs, read = set(cfg.get("favs", [])), cfg["read"]
        order = {h.get("path"): i for i, h in enumerate(cfg["history"])}
        units, groups = [], {}
        for b in self.home.books:
            par = os.path.dirname(b["path"])
            if cfg.get("lib_group") and os.path.abspath(par) != root:
                groups.setdefault(par, []).append(b)
            else:
                p = b["path"]
                units.append(dict(key=p, open=p, path=p, title=b["title"], pct=read.get(p),
                                  sub=(b["parent"] + "  \u00b7  " if b["parent"] else "") + f"{b['n']} p",
                                  fav=p in favs, mtime=b["mtime"], seen=order.get(p, 10 ** 6),
                                  started=p in read and (read.get(p) or 0) > 0,
                                  done=(read.get(p) or 0) >= 95, delete=p))
        for par, bs in groups.items():
            bs.sort(key=lambda b: natural_key(b["title"]))
            pcts = [read.get(b["path"]) or 0 for b in bs]
            seen = min(order.get(b["path"], 10 ** 6) for b in bs)
            recent = next((b["path"] for b in sorted(bs, key=lambda b: order.get(b["path"], 10 ** 6))
                           if b["path"] in order), None)
            resume = recent or next((b["path"] for b, p in zip(bs, pcts) if p < 95), bs[0]["path"])
            units.append(dict(key=par, open=resume, path=bs[0]["path"], title=os.path.basename(par),
                              pct=int(sum(min(100, p) for p in pcts) / len(pcts)) or None,
                              sub=f"{len(bs)} chapters", fav=par in favs, mtime=max(b["mtime"] for b in bs),
                              seen=seen, started=any(pcts), done=all(p >= 95 for p in pcts), delete=par))
        return units

    def refresh(self):
        self._job = None
        app = self.app
        if not app.cfg.get("library_root"):
            self.units = {}
            self.cg.set_items([], "Choose the folder that holds your books\nand they will appear here.",
                              ("Choose folder", app.pick_library_root))
            return
        units = self._units()
        self.units = {u["key"]: u for u in units}
        chip, q = app.cfg.get("lib_chip", "all"), self.bar.text().lower()
        if chip == "progress":
            units = [u for u in units if u["started"] and not u["done"]]
        elif chip == "done":
            units = [u for u in units if u["done"]]
        elif chip == "fav":
            units = [u for u in units if u["fav"]]
        if q:
            units = [u for u in units if q in u["title"].lower() or q in u["sub"].lower()]
        srt = app.cfg.get("lib_sort", "added")
        if srt == "title":
            units.sort(key=lambda u: natural_key(u["title"]))
        elif srt == "read":
            units.sort(key=lambda u: u["seen"])
        else:
            units.sort(key=lambda u: -u["mtime"])
        if self.home.books or units:
            msg = "Nothing matches." if (q or chip != "all") else "No books found in this folder."
        else:
            msg = f"No books found in\n{app.lib_root}"
        self.cg.set_items([dict(u) for u in units], msg)

    def _open(self, key):
        u = self.units.get(key)
        if u:
            self.app.open_path(u["open"])

    def _ctx(self, e, key):
        u = self.units.get(key)
        if not u:
            return
        app = self.app
        m = app.themed_menu(self)
        m.add_command(label="Open", command=lambda: self._open(key))
        m.add_command(label="Remove from favourites" if u["fav"] else "Add to favourites",
                      command=lambda: self.home.toggle_fav(key))
        m.add_command(label="Show in file manager", command=lambda: app._reveal(u["delete"]))
        m.add_separator()
        m.add_command(label="Delete folder\u2026", command=lambda: app._delete_folder(u["delete"]))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _menu(self, e):
        app = self.app
        m = app.themed_menu(self)
        m.add_command(label="Set library folder\u2026", command=app.pick_library_root)
        m.add_command(label="Rescan", command=self.home.rescan)
        m.add_command(label="Show library in file manager", command=lambda: app._reveal(app.lib_root))
        m.add_separator()
        cur = app.cfg.get("lib_sort", "added")
        for key, label in (("added", "Recently added"), ("read", "Recently read"), ("title", "Title")):
            m.add_command(label=("\u2713 " if cur == key else "    ") + "Sort: " + label,
                          command=lambda k=key: self._sort(k))
        m.add_separator()
        m.add_checkbutton(label="Group chapters by folder", variable=self.group_var, command=self._group)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _sort(self, key):
        self.app.cfg["lib_sort"] = key
        self.refresh()

    def _group(self):
        self.app.cfg["lib_group"] = bool(self.group_var.get())
        self.refresh()

    def wheel(self, w, dy):
        self.cg.wheel(dy)


class ExplorePage(tk.Frame):
    def __init__(self, parent, home):
        super().__init__(parent, bd=0, highlightthickness=0)
        self.home, self.app = home, home.app
        app, S = self.app, self.app.S
        self.bar = SearchBar(self, app, "Search manga", on_enter=self._search, on_menu=self._menu)
        self.bar.pack(fill="x", padx=S(14), pady=(S(14), S(6)))
        self.sc = Scroller(self, app)
        self.sc.pack(fill="both", expand=True)
        inner = self.sc.inner
        self.tiles = Tiles(inner, app, (("local", "Local storage", "sd"), ("fav", "Bookmarks", "bookmark"),
                                        ("random", "Random", "dice"), ("dl", "Downloads", "download")),
                           self._tile)
        self.tiles.pack(fill="x", padx=S(14), pady=(S(6), S(4)))
        self.h1 = self._header(inner, "Suggestions", "More", lambda: home.show("library"))
        self.car = Carousel(inner, app, self.home.open_book)
        self.car.pack(fill="x", padx=0)
        self.h2 = self._header(inner, "Manga sources", "Manage", self._manage)
        self.src = tk.Frame(inner, bd=0, highlightthickness=0)
        self.src.pack(fill="x", padx=S(14))
        self.src_note = tk.Label(inner, justify="left", font=app.f_small, anchor="w")
        self.src_note.pack(fill="x", padx=S(16), pady=(S(4), S(40)))
        self.src.bind("<Configure>", lambda e: self._layout_sources())
        self.src_tiles = []

    def _header(self, parent, left, right, cmd):
        S = self.app.S
        f = tk.Frame(parent, bd=0, highlightthickness=0)
        f.pack(fill="x", padx=S(16), pady=(S(18), S(4)))
        l1 = tk.Label(f, text=left, font=self.app.f_h, anchor="w")
        l1.pack(side="left")
        l2 = tk.Label(f, text=right, font=self.app.f_brand, cursor="hand2")
        l2.pack(side="right")
        l2.bind("<Button-1>", lambda e: cmd())
        return (f, l1, l2)

    def retheme(self):
        t = self.app.t
        self.configure(bg=t["bg"])
        self.sc.recolor(t["bg"])
        self.bar.redraw()
        self.tiles.redraw()
        for f, l1, l2 in (self.h1, self.h2):
            f.configure(bg=t["bg"])
            l1.configure(bg=t["bg"], fg=t["fg"])
            l2.configure(bg=t["bg"], fg=t["fg"])
        self.src.configure(bg=t["bg"])
        self.src_note.configure(bg=t["bg"], fg=t["dim"])
        self.car.redraw()
        self.refresh()

    def refresh(self):
        self.car.set_items(self.home.suggestions())
        self._build_sources()

    # ---- sources grid
    def _build_sources(self):
        t, S, app = self.app.t, self.S, self.app
        for c in self.src_tiles:
            c.destroy()
        self.src_tiles = []
        if not HAVE_GDL:
            self.src_note.configure(text="Install gallery-dl to browse sources:  pip install -U gallery-dl")
            return
        self.src_note.configure(text="Sources marked ! have no rating filter and can return adult content."
                                if any(m == "Tags" and n not in RATING_TAG for n, m in self.home.sources()) else "")
        for name, mode in self.home.sources():
            c = tk.Canvas(self.src, width=S(98), height=S(104), bg=t["bg"], highlightthickness=0, bd=0,
                          cursor="hand2")
            col = source_color(name)
            rrect(c, S(17), S(4), S(81), S(68), S(16), fill=mix(t["bg"], col, 0.12), outline=col, width=2)
            c.create_text(S(49), S(36), text=name[:1].upper(), fill=col, font=app.f_title)
            label = name + (" !" if mode == "Tags" and name not in RATING_TAG else "")
            c.create_text(S(49), S(86), text=(wrap_lines(label, app.f_small, S(96), 1) or [""])[0],
                          fill=t["fg"], font=app.f_small)
            c.bind("<Button-1>", lambda e, n=name, m=mode: self.home.open_source(n, m))
            self.src_tiles.append(c)
        self._layout_sources()

    @property
    def S(self):
        return self.app.S

    def _layout_sources(self):
        S = self.S
        cell = S(104)
        cols = max(2, self.src.winfo_width() // cell)
        for i, c in enumerate(self.src_tiles):
            c.grid(row=i // cols, column=i % cols, padx=S(3), pady=S(3))

    # ---- actions
    def _search(self, text):
        if text:
            self.home.open_search(text)

    def _tile(self, key):
        h = self.home
        if key == "local":
            h.show("library")
            h.pages["library"].set_chip("all")
        elif key == "fav":
            h.show("library")
            h.pages["library"].set_chip("fav")
        elif key == "random":
            h.open_random()
        else:
            h.show_browse()

    def _manage(self):
        m = self.app.themed_menu(self)
        m.add_command(label="Download settings\u2026", command=self.home.open_dl_settings)
        m.add_command(label="Library folder\u2026", command=self.app.pick_library_root)
        try:
            m.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            m.grab_release()

    def _menu(self, e):
        self._manage()

    def wheel(self, w, dy):
        self.sc.wheel(dy)


class Home(tk.Frame):
    """Full-window start screen: History / Library / Explore with a floating nav pill."""

    def __init__(self, app):
        super().__init__(app, bd=0, highlightthickness=0)
        self.app, self.S = app, app.S
        self.books, self.scan_token = [], 0
        self.current = None
        self.last_main = app.cfg.get("home_page", "library")
        if self.last_main not in ("history", "library", "explore"):
            self.last_main = "library"
        self.nav_bar = tk.Frame(self, height=app.S(84), bd=0, highlightthickness=0)
        self.nav_bar.pack(side="bottom", fill="x")
        self.nav_bar.pack_propagate(False)
        self.body = tk.Frame(self, bd=0, highlightthickness=0)
        self.body.pack(side="top", fill="both", expand=True)
        self.nav = NavPill(self.nav_bar, app, self.show)
        self.nav.place(relx=0.5, rely=0.5, anchor="center")
        self.pages = {"history": HistoryPage(self.body, self), "library": LibraryPage(self.body, self),
                      "explore": ExplorePage(self.body, self)}
        self.browse = None
        self.retheme()
        if HAVE_GDL:
            app.info_pool.submit(dl_guard, dl_worker_sites, app.ready)
        self.rescan()

    # ---- pages
    def _all(self):
        d = dict(self.pages)
        if self.browse is not None:
            d["browse"] = self.browse
        return d

    def show(self, name):
        pages = self._all()
        if name not in pages:
            return
        for p in pages.values():
            p.pack_forget()
        page = pages[name]
        page.pack(fill="both", expand=True)
        self.current = name
        if name == "browse":
            self.nav_bar.pack_forget()
        else:
            self.last_main = name
            self.nav_bar.pack(side="bottom", fill="x", before=self.body)
            self.nav.set_current(name)
        if hasattr(page, "refresh"):
            page.refresh()

    def back(self):
        self.show(self.last_main)

    def escape(self):
        if self.current == "browse":
            self.back()
        elif self.app.paths:
            self.app.hide_home()

    def retheme(self):
        t = self.app.t
        for w in (self, self.body, self.nav_bar):
            w.configure(bg=t["bg"])
        for p in self._all().values():
            p.retheme()
        self.nav.redraw()

    def wheel(self, w, dy):
        page = self._all().get(self.current)
        if page is not None:
            page.wheel(w, dy)

    # ---- data
    def rescan(self):
        app = self.app
        root = app.lib_root
        if not app.cfg.get("library_root") or not os.path.isdir(root):
            self.books = []
            self.refresh_pages()
            return
        self.scan_token += 1
        app.io_pool.submit(home_scan, app.ready, self.scan_token, root)

    def on_lib(self, token, root, books):
        if token != self.scan_token:
            return
        self.books = books
        self.refresh_pages()

    def refresh_pages(self):
        if self.current in self.pages:
            self.pages[self.current].refresh()

    def on_cover(self, gid, key, im):
        if gid == "banner":
            self.pages["explore"].car.on_cover(key, im)
        elif gid in ("history", "library"):
            self.pages[gid].cg.on_cover(key, im)

    def on_sites(self):
        if self.current == "explore":
            self.pages["explore"]._build_sources()

    def suggestions(self):
        cfg, out, seen = self.app.cfg, [], set()
        for h in cfg["history"]:
            p = h.get("path", "")
            pct = cfg["read"].get(p)
            if os.path.isdir(p) and pct is not None and pct < 95:
                out.append(dict(path=p, title=os.path.basename(p), sub=f"Continue  \u00b7  {pct}% read"))
                seen.add(p)
        pool = [b for b in self.books if b["path"] not in seen and b["path"] not in cfg["read"]]
        random.Random(datetime.now().strftime("%Y-%m-%d")).shuffle(pool)
        for b in pool:
            out.append(dict(path=b["path"], title=b["title"],
                            sub=(b["parent"] + "  \u00b7  " if b["parent"] else "") + f"{b['n']} page{'' if b['n'] == 1 else 's'}"))
        return out[:8]

    def sources(self):
        out = [(n, "Search" if "search" in v else "Codes") for n, v in BOOK_SITES.items()]
        extra = [n for n in sorted(self.app.dl_tag_sites) if n not in BOOK_SITES]
        extra.sort(key=lambda n: (n not in RATING_TAG, n))
        return out + [(n, "Tags") for n in extra]

    def toggle_fav(self, key):
        favs = self.app.cfg.setdefault("favs", [])
        if key in favs:
            favs.remove(key)
        else:
            favs.append(key)
        self.refresh_pages()

    def open_book(self, path):
        self.app.open_path(path)

    def open_random(self):
        pool = [b["path"] for b in self.books] or [h["path"] for h in self.app.cfg["history"]
                                                   if os.path.isdir(h.get("path", ""))]
        if pool:
            self.open_book(random.choice(pool))
        else:
            messagebox.showinfo("Random", "Your library is empty. Choose a library folder first.")

    # ---- browsing sources / downloads
    def show_browse(self, mode=None, site=None, query=None, go=False):
        if not HAVE_GDL:
            messagebox.showinfo("gallery-dl is missing",
                                "Install it with:\n\n    pip install -U gallery-dl\n\nthen restart the reader.")
            return
        app = self.app
        if self.browse is None:
            self.browse = Discover(app, self.body)
            app.dm = self.browse
        self.show("browse")
        d = self.browse
        if mode:
            if mode == "Tags" and site and site not in RATING_TAG:
                d.sfw.set(False)
            d.mode.set(mode)
            d._mode_changed()
        if site and site in d.site_cb.cget("values"):
            d.site.set(site)
        if query is not None:
            d.query.set(query)
        if go:
            d._go()
        d.q_ent.focus_set()

    def open_source(self, name, mode):
        self.show_browse(mode, name)

    def open_search(self, text):
        sites = [n for n, v in BOOK_SITES.items() if "search" in v]
        self.show_browse("Search", sites[0] if sites else None, text, go=True)

    def open_dl_settings(self):
        self.show_browse()
        if self.browse is not None:
            self.browse.open_settings()


class WebtoonReader(tk.Tk):
    def __init__(self, start=None):
        super().__init__()
        self.ui = max(1.0, self.winfo_fpixels("1i") / 96.0)      # DPI scale factor
        self.pad_top = self.S(150)      # room for the chapter title card
        self.pad_bot = self.S(360)      # room for the end-of-chapter card
        self.hud_w, self.hud_h = self.S(250), self.S(38)
        self.seek_zone = self.S(12)
        self.bar_h = self.S(3)
        self.title("Webtoon Reader")
        self.geometry(f"{self.S(1000)}x{self.S(900)}")
        self.minsize(self.S(420), self.S(420))

        self.cfg = load_cfg()
        self.cfg.setdefault("progress", {})
        self.cfg.setdefault("read", {})
        if not isinstance(self.cfg.get("history"), list):
            self.cfg["history"] = []

        self.theme = tk.StringVar(value=self.cfg.get("theme", "Midnight"))
        if self.theme.get() not in THEMES:
            self.theme.set("Midnight")
        self.t = THEMES[self.theme.get()]
        self.pct = tk.DoubleVar(value=self.cfg.get("width_pct", 80))
        self.gap = tk.IntVar(value=self.cfg.get("gap", 0))
        self.speed = tk.DoubleVar(value=self.cfg.get("speed", 120))
        self.auto = tk.BooleanVar(value=False)
        self.panel_var = tk.BooleanVar(value=False)

        # worker pool + results queue (workers never touch Tk; they only post to the queue)
        self.pool = ThreadPoolExecutor(max_workers=WORKERS)          # page decoding
        self.io_pool = ThreadPoolExecutor(max_workers=3)             # folder scans, cover previews
        self.ready = queue.Queue()
        self.dl_pool = ThreadPoolExecutor(max_workers=1)    # download queue: one book at a time
        self.info_pool = ThreadPoolExecutor(max_workers=2)  # searches, details, covers
        self.dm = None                                      # Browse page (Discover), created on demand
        self.home = None                                    # Home screen
        self.home_on = False
        self.dl_tag_sites = {}
        self.gen = 0                 # bumps whenever the layout changes -> stale results are dropped
        self.pending = {}            # page index -> Future
        self._wanted = set()
        self.lib_token = 0
        self.cover_token = 0

        self.folder = None
        self.paths, self.dims = [], []
        self.tops, self.heights = [], []
        self.total = 1
        self.img_w = 100
        self.items = {}              # page index -> [(PhotoImage, canvas id), ...] (one per tile)
        self.ph = {}                 # page index -> placeholder rectangle id
        self.bad = set()
        self.target = 0.0
        self._anim = False
        self._last_t = 0.0
        self._render_job = None
        self._relayout_job = None
        self._hud_job = None
        self._drag_y = None
        self._seeking = False
        self._fullscreen = False
        self._toolbar_visible = True
        self._zen = False
        self._zen_state = (True, False)
        self._panel_was = False
        self._brand_shown = True
        self.bar_fg = None
        self.hud_txt = None

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._setup_fonts()

        self._build_ui()
        self.apply_theme()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.home = Home(self)

        self.after(30, self._auto_tick)
        self.after(10, self._pump)
        if start:
            self.after(100, lambda: self.open_path(start))
        else:
            self.after(30, self.show_home)

    def S(self, v):
        """Scale a pixel size for the current display DPI."""
        return int(round(v * self.ui))

    @property
    def panel_visible(self):
        """Source of truth is the paned window itself, so the state can never get out of sync."""
        try:
            return str(self.side) in [str(p) for p in self.paned.panes()]
        except tk.TclError:
            return False

    # --------------------------------------------------------------- fonts
    def _setup_fonts(self):
        fams = set(tkfont.families())
        fam = next((f for f in ("Inter", "Segoe UI", "SF Pro Text", "Roboto", "Noto Sans",
                                "Ubuntu", "DejaVu Sans") if f in fams), None)
        fam = fam or tkfont.nametofont("TkDefaultFont").actual("family")
        for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(n).configure(family=fam, size=10)
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_brand = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_hud = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_arrow = tkfont.Font(family=fam, size=20, weight="bold")
        self.f_title = tkfont.Font(family=fam, size=24, weight="bold")
        self.f_sub = tkfont.Font(family=fam, size=11)
        self.f_h = tkfont.Font(family=fam, size=13, weight="bold")
        self.f_card = tkfont.Font(family=fam, size=10)

    # ------------------------------------------------------------------ UI
    def _sep(self, parent):
        ttk.Separator(parent, orient="vertical").pack(side="left", fill="y", padx=10, pady=2)

    def _build_ui(self):
        tb = self.toolbar = ttk.Frame(self, padding=(12, 9))
        tb.pack(side="top", fill="x")

        # right side first so it never gets clipped on narrow windows
        ttk.Button(tb, text="Full", style="Tool.TButton", width=5,
                   command=self.toggle_fullscreen).pack(side="right")
        ttk.Button(tb, text="Zen", style="Tool.TButton", width=5,
                   command=self.toggle_zen).pack(side="right", padx=(0, 4))

        self.brand = ttk.Label(tb, text="\u25c6 WEBTOON", style="Brand.TLabel")
        self.brand.pack(side="left", padx=(0, 14))
        ttk.Button(tb, text="\u2302 Home", style="Tool.TButton", command=self.show_home).pack(side="left", padx=(0, 4))
        # Library is a real toggle button now: lit while the panel is open
        self.btn_lib = ttk.Checkbutton(tb, text="\u2630 Library", style="Toolbutton",
                                       variable=self.panel_var, takefocus=False,
                                       command=lambda: self.set_panel(self.panel_var.get()))
        self.btn_lib.pack(side="left")
        tb.bind("<Configure>", self._toolbar_resize)
        ttk.Button(tb, text="Folder", style="Tool.TButton", command=self.ask_folder).pack(side="left", padx=4)
        ttk.Button(tb, text="Files", style="Tool.TButton", command=self.ask_files).pack(side="left")
        ttk.Button(tb, text="\u21e9 Get", style="Tool.TButton", command=self.show_discover).pack(side="left", padx=4)
        ttk.Button(tb, text="\u25b6 Resume", style="Tool.TButton", command=self.continue_reading).pack(side="left")
        self._sep(tb)

        self.sw = tk.Canvas(tb, height=self.S(28), width=len(THEMES) * self.S(24) + self.S(4),
                            highlightthickness=0, bd=0)
        self.sw.pack(side="left")
        self.sw.bind("<Button-1>", self._swatch_click)
        self.sw.bind("<Motion>", self._swatch_hover)
        self._sep(tb)

        ttk.Label(tb, text="Width", style="Dim.TLabel").pack(side="left")
        ttk.Scale(tb, from_=30, to=100, variable=self.pct, length=self.S(90),
                  command=lambda v: self.sched_relayout()).pack(side="left", padx=(5, 12))
        ttk.Label(tb, text="Gap", style="Dim.TLabel").pack(side="left")
        sp = ttk.Spinbox(tb, from_=0, to=200, increment=4, width=4,
                         textvariable=self.gap, command=self.sched_relayout)
        sp.pack(side="left", padx=(5, 0))
        sp.bind("<Return>", lambda e: (self.sched_relayout(), self.canvas.focus_set()))
        self._sep(tb)

        ttk.Checkbutton(tb, text="\u25b6 Auto", style="Toolbutton", variable=self.auto,
                        takefocus=False, command=lambda: self.canvas.focus_set()).pack(side="left")
        ttk.Scale(tb, from_=20, to=800, variable=self.speed, length=self.S(80)).pack(side="left", padx=(8, 0))

        self.strip = tk.Canvas(self, height=self.bar_h, highlightthickness=0, bd=0)
        self.strip.pack(side="top", fill="x")
        self.strip.bind("<Configure>", lambda e: self._draw_strip())

        self.status = ttk.Label(self, anchor="w", padding=(12, 4), style="Status.TLabel")
        self.status.pack(side="bottom", fill="x")

        self.paned = ttk.PanedWindow(self, orient="horizontal")
        self.paned.pack(side="top", fill="both", expand=True)
        body = ttk.Frame(self.paned)
        self.paned.add(body, weight=1)
        self._build_side_panel()

        self.vbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas_yview)
        self.vbar.pack(side="right", fill="y")
        self.canvas = tk.Canvas(body, highlightthickness=0, bd=0, yscrollcommand=self._on_yscroll)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<Configure>", lambda e: self.sched_relayout())
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Double-Button-1>",
                         lambda e: self.toggle_toolbar() if e.y > self.seek_zone else None)
        self.bind_all("<MouseWheel>", self._wheel)
        self.bind_all("<Button-4>", self._wheel)
        self.bind_all("<Button-5>", self._wheel)
        self.canvas.focus_set()

        if self.cfg.get("panel_visible", True):
            self.after(50, lambda: self.set_panel(True))

    def _bind_keys(self):
        def k(keys, fn, home=False):
            for key in keys:
                self.bind(key, lambda e, f=fn, h=home: self._key(f, h))

        k(["<Down>", "<j>"], lambda: self.scroll_by(self.S(120)))
        k(["<Up>", "<k>"], lambda: self.scroll_by(-self.S(120)))
        k(["<space>", "<Next>"], lambda: self.scroll_by(self.canvas.winfo_height() * 0.9))
        k(["<Shift-space>", "<Prior>"], lambda: self.scroll_by(-self.canvas.winfo_height() * 0.9))
        k(["<Home>"], lambda: self.jump(0))
        k(["<End>"], lambda: self.jump(self.total))
        k(["<plus>", "<equal>", "<KP_Add>"], lambda: self.nudge_width(5))
        k(["<minus>", "<KP_Subtract>"], lambda: self.nudge_width(-5))
        k(["<t>"], self.cycle_theme, True)
        k(["<a>"], self.toggle_auto)
        k(["<bracketleft>"], lambda: self.speed.set(max(20, self.speed.get() - 30)))
        k(["<bracketright>"], lambda: self.speed.set(min(800, self.speed.get() + 30)))
        k(["<n>"], lambda: self.chapter(1))
        k(["<p>"], lambda: self.chapter(-1))
        k(["<o>"], self.ask_folder, True)
        k(["<l>"], self.toggle_panel)
        k(["<d>"], self.show_discover, True)
        k(["<c>"], self.continue_reading, True)
        k(["<z>"], self.toggle_zen)
        k(["<f>"], self.ask_files)
        k(["<h>"], self.toggle_toolbar)
        k(["<F11>"], self.toggle_fullscreen, True)
        k(["<Escape>"], self.escape, True)
        k(["<BackSpace>"], self.show_home)
        k(["<q>"], self.on_close, True)

    def _key(self, fn, home=False):
        if self.home_on and not home:
            return
        try:
            w = self.focus_get()
        except KeyError:
            w = None
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Treeview, tk.Text, tk.Listbox)):
            return
        fn()

    # --------------------------------------------------------------- theme
    def apply_theme(self):
        t = self.t = THEMES[self.theme.get()]
        s = self.style
        self.configure(bg=t["bg"])
        s.configure(".", background=t["panel"], foreground=t["fg"], fieldbackground=t["bg"],
                    bordercolor=t["border"], lightcolor=t["panel"], darkcolor=t["panel"],
                    troughcolor=t["bg"], focuscolor=t["panel"], insertcolor=t["fg"])
        s.configure("TFrame", background=t["panel"])
        s.configure("TLabel", background=t["panel"], foreground=t["fg"])
        s.configure("Brand.TLabel", foreground=t["accent"], font=self.f_brand)
        s.configure("Dim.TLabel", foreground=t["dim_p"], font=self.f_small)
        s.configure("Status.TLabel", foreground=t["dim_p"], font=self.f_small)
        s.configure("TSeparator", background=t["border"])

        s.configure("TButton", background=t["border"], foreground=t["fg"], padding=(10, 4), borderwidth=0)
        s.map("TButton", background=[("active", t["accent"]), ("pressed", t["accent"])],
              foreground=[("active", t["bg"]), ("pressed", t["bg"])])
        s.configure("Tool.TButton", background=t["panel"], foreground=t["fg"], padding=(10, 5), borderwidth=0)
        s.map("Tool.TButton", background=[("active", t["accent"]), ("pressed", t["accent2"])],
              foreground=[("active", t["bg"]), ("pressed", t["bg"])])
        s.configure("Toolbutton", background=t["panel"], foreground=t["fg"], padding=(10, 5), borderwidth=0)
        s.map("Toolbutton", background=[("selected", t["accent"]), ("active", t["border"])],
              foreground=[("selected", t["bg"])])
        s.configure("TCheckbutton", background=t["panel"], foreground=t["fg"])
        s.configure("TSpinbox", fieldbackground=t["bg"], background=t["border"],
                    foreground=t["fg"], arrowcolor=t["fg"], bordercolor=t["border"])
        s.configure("Horizontal.TScale", background=t["accent"], troughcolor=t["bg"],
                    bordercolor=t["border"])
        s.map("Horizontal.TScale", background=[("active", t["accent2"])])

        # slim, arrow-less scrollbars
        s.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure("Vertical.TScrollbar", background=t["border"], troughcolor=t["canvas"],
                    bordercolor=t["canvas"], lightcolor=t["border"], darkcolor=t["border"],
                    gripcount=0, arrowsize=self.S(10), width=self.S(10))
        s.map("Vertical.TScrollbar", background=[("active", t["accent"])])

        s.configure("Treeview", background=t["bg"], fieldbackground=t["bg"], foreground=t["fg"],
                    bordercolor=t["border"], rowheight=self.S(26))
        s.map("Treeview", background=[("selected", t["accent"])], foreground=[("selected", t["bg"])])
        s.configure("Treeview.Heading", background=t["border"], foreground=t["fg"], relief="flat",
                    padding=(6, 4))
        s.map("Treeview.Heading", background=[("active", t["accent"])],
              foreground=[("active", t["bg"])])
        s.configure("TNotebook", background=t["panel"], borderwidth=0)
        s.configure("TNotebook.Tab", background=t["border"], foreground=t["fg"], padding=(14, 5))
        s.map("TNotebook.Tab", background=[("selected", t["accent"])], foreground=[("selected", t["bg"])])
        s.configure("TPanedwindow", background=t["panel"])
        s.configure("Sash", sashthickness=self.S(5), gripcount=0, lightcolor=t["border"],
                    darkcolor=t["border"], bordercolor=t["border"])

        self.canvas.configure(bg=t["canvas"])
        self._theme_dl()
        self._draw_strip()
        self._draw_swatches()
        self._build_overlay()
        self._draw_deco()
        for cid in self.ph.values():
            self.canvas.itemconfigure(cid, fill=t["ph"])
        self.show_hint()
        self.place_overlay()
        self.update_status()
        self.flash_hud()

    def cycle_theme(self):
        names = list(THEMES)
        self.theme.set(names[(names.index(self.theme.get()) + 1) % len(names)])
        self.apply_theme()

    def _draw_strip(self):
        w = self.strip.winfo_width()
        if w < 2:
            return
        self._strip_img = ImageTk.PhotoImage(
            gradient_image(w, self.bar_h, self.t["accent"], self.t["accent2"]))
        self.strip.configure(bg=self.t["panel"])
        self.strip.delete("all")
        self.strip.create_image(0, 0, image=self._strip_img, anchor="nw")

    def _draw_swatches(self):
        c, S = self.sw, self.S
        c.configure(bg=self.t["panel"])
        c.delete("all")
        for i, (name, th) in enumerate(THEMES.items()):
            x, y = S(15) + i * S(24), S(14)
            r = S(8)
            c.create_oval(x - r, y - r, x + r, y + r, fill=th["canvas"], outline=th["border"])
            c.create_arc(x - r, y - r, x + r, y + r, start=-45, extent=180,
                         fill=th["accent"], outline="", style="pieslice")
            if name == self.theme.get():
                r2 = S(11)
                c.create_oval(x - r2, y - r2, x + r2, y + r2, outline=self.t["accent"], width=2)

    def _toolbar_resize(self, e):
        narrow = e.width < self.S(1100)
        if narrow and self._brand_shown:
            self.brand.pack_forget()
            self._brand_shown = False
        elif not narrow and not self._brand_shown:
            self.brand.pack(side="left", padx=(0, 14), before=self.btn_lib)
            self._brand_shown = True

    def _swatch_index(self, e):
        return max(0, min(len(THEMES) - 1, (e.x - self.S(3)) // self.S(24)))

    def _swatch_click(self, e):
        self.theme.set(list(THEMES)[self._swatch_index(e)])
        self.apply_theme()
        self.canvas.focus_set()

    def _swatch_hover(self, e):
        self.status.config(text=f"Theme: {list(THEMES)[self._swatch_index(e)]}")

    # ------------------------------------------------------ canvas overlay
    def _build_overlay(self):
        c, t = self.canvas, self.t
        c.delete("hud")
        c.delete("bar")
        rgb = lambda h, a: hex_rgb(h) + (a,)
        self.hud_img = ImageTk.PhotoImage(
            pill_image(self.hud_w, self.hud_h, rgb(t["panel"], 215), rgb(t["accent"], 150)))
        self.hud_bg = c.create_image(0, 0, image=self.hud_img, tags=("hud",), state="hidden")
        self.hud_txt = c.create_text(0, 0, text="", fill=t["fg"], font=self.f_hud,
                                     tags=("hud",), state="hidden")
        self.hud_prev = c.create_text(0, 0, text="\u2039", fill=t["accent"], font=self.f_arrow,
                                      tags=("hud", "hud_prev"), state="hidden")
        self.hud_next = c.create_text(0, 0, text="\u203a", fill=t["accent"], font=self.f_arrow,
                                      tags=("hud", "hud_next"), state="hidden")
        c.tag_bind("hud_prev", "<Button-1>", lambda e: self.chapter(-1))
        c.tag_bind("hud_next", "<Button-1>", lambda e: self.chapter(1))
        self.bar_bg = c.create_rectangle(0, 0, 0, 0, fill=t["track"], width=0, tags=("bar",))
        self.bar_fg = c.create_rectangle(0, 0, 0, 0, fill=t["accent"], width=0, tags=("bar",))
        c.tag_raise("bar")
        c.tag_raise("hud")

    def place_overlay(self):
        if self.bar_fg is None:
            return
        c = self.canvas
        top, cw, ch = c.canvasy(0), c.winfo_width(), c.winfo_height()
        cx, cy = cw / 2, top + ch - self.S(46)
        c.coords(self.hud_bg, cx, cy)
        c.coords(self.hud_txt, cx, cy)
        c.coords(self.hud_prev, cx - self.hud_w / 2 + self.S(22), cy - 2)
        c.coords(self.hud_next, cx + self.hud_w / 2 - self.S(22), cy - 2)
        maxy = max(1, self.total - ch)
        frac = min(1.0, top / maxy) if self.paths else 0.0
        c.coords(self.bar_bg, 0, top, cw, top + self.bar_h)
        c.coords(self.bar_fg, 0, top, cw * frac, top + self.bar_h)
        c.itemconfigure("bar", state="normal" if self.paths else "hidden")

    def flash_hud(self):
        if not self.paths or self.bar_fg is None:
            return
        self.canvas.itemconfigure("hud", state="normal")
        if self._hud_job:
            self.after_cancel(self._hud_job)
        self._hud_job = self.after(1800, self._hide_hud)

    def _hide_hud(self):
        self._hud_job = None
        self.canvas.itemconfigure("hud", state="hidden")

    def _draw_deco(self):
        """Reading-column backdrop, chapter title card and end card."""
        c, t = self.canvas, self.t
        c.delete("deco")
        if not self.paths:
            return
        cw = c.winfo_width()
        x0, cx = (cw - self.img_w) // 2, cw // 2
        c.create_rectangle(x0, 0, x0 + self.img_w, self.total, fill=t["col"], outline="",
                           tags=("deco", "col"))
        wrap = max(self.S(240), self.img_w - self.S(40))
        title = os.path.basename(self.folder or "") or "Untitled"
        c.create_text(cx, self.pad_top / 2 - self.S(10), text=title, fill=t["fg"], font=self.f_title,
                      width=wrap, justify="center", tags="deco")
        c.create_text(cx, self.pad_top / 2 + self.S(34), text=f"{len(self.paths)} pages",
                      fill=t["accent"], font=self.f_brand, tags="deco")
        y = self.total - self.pad_bot / 2
        c.create_text(cx, y - self.S(14), text="END OF CHAPTER", fill=t["accent"], font=self.f_title,
                      tags="deco")
        c.create_text(cx, y + self.S(26), text="N  next chapter     P  previous     Home  back to top",
                      fill=t["dim"], font=self.f_sub, tags="deco")
        c.tag_lower("deco")
        c.tag_lower("col")

    def show_hint(self):
        c, t, S = self.canvas, self.t, self.S
        c.delete("hint")
        if self.paths:
            return
        cx = max(c.winfo_width(), S(300)) // 2
        cy = max(c.winfo_height(), S(400)) // 2 - S(90)
        c.create_text(cx, cy, text="WEBTOON READER", font=self.f_title, fill=t["fg"], tags="hint")
        c.create_text(cx, cy + S(36), text="smooth vertical reading, themed your way",
                      font=self.f_sub, fill=t["accent"], tags="hint")
        y = cy + S(96)
        for key, desc in [("O", "open a folder"), ("L", "library & history"), ("D", "discover & download"), ("C", "continue reading"), ("T", "cycle theme"),
                          ("A", "auto-scroll"), ("Z", "zen mode"), ("F11", "fullscreen")]:
            c.create_text(cx - S(14), y, text=key, anchor="e", fill=t["accent2"], font=self.f_brand, tags="hint")
            c.create_text(cx + S(14), y, text=desc, anchor="w", fill=t["dim"], font=self.f_sub, tags="hint")
            y += S(28)

    # --------------------------------------------- background results pump
    def _pump(self):
        """Main-thread side of the worker pool: turns finished jobs into canvas / widget updates."""
        t0 = time.perf_counter()
        try:
            while time.perf_counter() - t0 < 0.012:
                msg = self.ready.get_nowait()
                kind = msg[0]
                try:
                    if kind == "page":
                        self._on_page(*msg[1:])
                    elif kind == "bad":
                        self._on_bad(*msg[1:])
                    elif kind == "lib":
                        self._on_lib(*msg[1:])
                    elif kind == "cover":
                        self._on_cover(*msg[1:])
                    elif kind == "dl":
                        self._on_dl(*msg[1:])
                    elif kind == "hcover":
                        if self.home is not None:
                            self.home.on_cover(*msg[1:])
                    elif kind == "hlib":
                        if self.home is not None:
                            self.home.on_lib(*msg[1:])
                except tk.TclError:
                    pass
        except queue.Empty:
            pass
        self.after(6 if not self.ready.empty() else 12, self._pump)

    # ------------------------------------------------------------- opening
    def ask_folder(self):
        d = filedialog.askdirectory(initialdir=self.cfg.get("last_dir", os.path.expanduser("~")))
        if d:
            self.open_path(d)

    def ask_files(self):
        types = [("Images", " ".join("*" + e for e in sorted(EXTS))), ("All files", "*.*")]
        fs = filedialog.askopenfilenames(initialdir=self.cfg.get("last_dir", os.path.expanduser("~")),
                                         filetypes=types)
        if fs:
            fs = sorted(fs, key=lambda p: natural_key(os.path.basename(p)))
            self.load(list(fs), os.path.dirname(fs[0]))

    def open_path(self, p):
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            self.load(list_images(p), p)
        elif os.path.isfile(p):
            folder = os.path.dirname(p)
            self.load(list_images(folder), folder)

    def load(self, paths, folder):
        self.save_progress()
        for f in self.pending.values():
            f.cancel()
        self.pending.clear()
        self.config(cursor="watch")
        self.update_idletasks()
        sizes = list(self.pool.map(image_size, paths))      # headers read in parallel
        self.config(cursor="")
        good, dims = [], []
        for p, sz in zip(paths, sizes):
            if sz and sz[0] > 0 and sz[1] > 0:
                good.append(p)
                dims.append(sz)
        if not good:
            self.status.config(text="No readable images found.")
            return
        self.folder, self.paths, self.dims = folder, good, dims
        self.cfg["last_dir"] = folder
        self.add_history(folder)
        self._reset_pages()
        self.bad.clear()
        self.canvas.delete("hint")
        self.hide_home()
        self.title(f"Webtoon Reader - {os.path.basename(folder) or folder}")
        self.relayout(frac=self.cfg["progress"].get(folder, 0.0))
        self.canvas.focus_set()
        self.flash_hud()
        if self.panel_visible:
            self.after(300, self.refresh_panel)

    def chapter(self, step):
        if not self.folder:
            return
        parent = os.path.dirname(self.folder)
        try:
            sibs = sorted((d for d in os.listdir(parent)
                           if os.path.isdir(os.path.join(parent, d))
                           and list_images(os.path.join(parent, d))), key=natural_key)
        except OSError:
            return
        name = os.path.basename(self.folder)
        if name not in sibs:
            return
        i = sibs.index(name) + step
        if 0 <= i < len(sibs):
            d = os.path.join(parent, sibs[i])
            if step > 0:
                self.cfg["progress"].pop(d, None)   # next chapters start at the top
            self.load(list_images(d), d)
            if step < 0:
                self.jump(self.total)               # previous chapter: land at its end
        else:
            self.status.config(text="No more chapters in that direction.")

    # -------------------------------------------------------------- layout
    def sched_relayout(self):
        if self._relayout_job:
            self.after_cancel(self._relayout_job)
        self._relayout_job = self.after(120, self.relayout)

    def relayout(self, frac=None):
        self._relayout_job = None
        cw = self.canvas.winfo_width()
        if cw < 10:
            return
        if not self.paths:
            self.show_hint()
            self.place_overlay()
            return
        if frac is None:
            frac = self.canvas.canvasy(0) / self.total if self.total else 0.0
        try:
            gap = max(0, int(self.gap.get()))
        except tk.TclError:
            gap = 0
        self.img_w = max(50, int(cw * self.pct.get() / 100))
        y, self.tops, self.heights = self.pad_top, [], []
        for ow, oh in self.dims:
            h = max(1, round(oh * self.img_w / ow))
            self.tops.append(y)
            self.heights.append(h)
            y += h + gap
        self.total = max(1, y - gap + self.pad_bot)
        self._reset_pages()
        self.canvas.configure(scrollregion=(0, 0, cw, self.total))
        self._draw_deco()
        self.canvas.yview_moveto(max(0.0, min(1.0, frac)))
        self.target = self.canvas.canvasy(0)
        self.render()
        self.place_overlay()

    def _reset_pages(self):
        """Throw away everything decoded for the old layout and invalidate in-flight jobs."""
        self.gen += 1
        for f in self.pending.values():
            f.cancel()
        self.pending.clear()
        self.clear_items()
        for cid in self.ph.values():
            self.canvas.delete(cid)
        self.ph.clear()

    def clear_items(self):
        for objs in self.items.values():
            for _, cid in objs:
                self.canvas.delete(cid)
        self.items.clear()

    # ----------------------------------------------------------- rendering
    def _on_yscroll(self, first, last):
        self.vbar.set(first, last)
        if not self._anim:
            self.target = self.canvas.canvasy(0)
        self.place_overlay()
        if not self.auto.get():
            self.flash_hud()
        if self._render_job is None:
            self._render_job = self.after_idle(self.render)

    @staticmethod
    def _decode_page(ready, gen, i, path, w, h):
        """Worker thread: decode, Lanczos-resize, and cut into tiles. No Tk calls in here."""
        try:
            with Image.open(path) as im:
                if im.mode not in ("RGB", "RGBA"):
                    alpha = "A" in im.getbands() or im.info.get("transparency") is not None
                    im = im.convert("RGBA" if alpha else "RGB")
                im = im.resize((w, h), SMOOTH)
            tiles = [(y, im.crop((0, y, w, min(h, y + TILE)))) for y in range(0, h, TILE)]
        except Exception:
            ready.put(("bad", gen, i))
            return
        ready.put(("page", gen, i, tiles))

    def render(self):
        self._render_job = None
        if not self.tops:
            return
        c = self.canvas
        top, h = c.canvasy(0), c.winfo_height()
        # decode a generous window around the viewport: 2 screens behind, 5 ahead
        first = max(0, bisect_right(self.tops, top - 2 * h) - 1)
        last = min(len(self.tops) - 1, bisect_right(self.tops, top + 5 * h) - 1)
        wanted = set(range(first, last + 1))
        self._wanted = wanted

        for i in [k for k in self.items if k not in wanted]:
            for _, cid in self.items.pop(i):
                c.delete(cid)
        for i in [k for k in self.ph if k not in wanted]:
            c.delete(self.ph.pop(i))
        for i in [k for k in self.pending if k not in wanted]:
            self.pending.pop(i).cancel()

        mid = top + h / 2
        todo = sorted((i for i in wanted
                       if i not in self.items and i not in self.pending and i not in self.bad),
                      key=lambda i: abs(self.tops[i] + self.heights[i] / 2 - mid))
        x = (c.winfo_width() - self.img_w) // 2
        for i in todo:
            if i not in self.ph:
                self.ph[i] = c.create_rectangle(x, self.tops[i], x + self.img_w,
                                                self.tops[i] + self.heights[i],
                                                fill=self.t["ph"], outline="", tags=("ph",))
            self.pending[i] = self.pool.submit(self._decode_page, self.ready, self.gen, i,
                                               self.paths[i], self.img_w, self.heights[i])
        if todo:
            c.tag_lower("ph")
            c.tag_lower("col")
        self.update_status()

    def _on_page(self, gen, i, tiles):
        if gen != self.gen:
            return
        self.pending.pop(i, None)
        if i in self.items or i not in self._wanted:
            return
        c = self.canvas
        x = (c.winfo_width() - self.img_w) // 2
        objs = []
        for y, tile in tiles:
            photo = ImageTk.PhotoImage(tile)
            cid = c.create_image(x, self.tops[i] + y, anchor="nw", image=photo)
            objs.append((photo, cid))
        self.items[i] = objs
        if i in self.ph:
            c.delete(self.ph.pop(i))
        c.tag_raise("bar")
        c.tag_raise("hud")

    def _on_bad(self, gen, i):
        if gen != self.gen:
            return
        self.pending.pop(i, None)
        self.bad.add(i)
        if i in self.ph:
            self.canvas.itemconfigure(self.ph[i], fill=mix(self.t["col"], "#ff3b3b", 0.25))

    def update_status(self):
        if not self.tops:
            return
        ch = self.canvas.winfo_height()
        top = self.canvas.canvasy(0)
        idx = max(0, min(len(self.paths) - 1, bisect_right(self.tops, top + ch / 3) - 1))
        maxy = max(1, self.total - ch)
        pct = int(100 * min(1.0, top / maxy))
        if self.folder:
            self.cfg["read"][self.folder] = pct
            self.cfg["progress"][self.folder] = round(top / self.total, 5)
        name = os.path.basename(self.folder or "")
        self.status.config(text=f"{name}   \u00b7   page {idx + 1}/{len(self.paths)}   \u00b7   {pct}%   "
                                f"\u00b7   width {int(self.pct.get())}%   \u00b7   {self.theme.get()}")
        if self.hud_txt:
            self.canvas.itemconfigure(self.hud_txt, text=f"{idx + 1} / {len(self.paths)}     {pct}%")

    # ----------------------------------------------------------- scrolling
    def canvas_yview(self, *args):
        self.canvas.yview(*args)

    def _max_y(self):
        return max(0, self.total - self.canvas.winfo_height())

    def scroll_by(self, dy):
        if not self.tops:
            return
        self.target = max(0.0, min(float(self._max_y()), self.target + dy))
        if not self._anim:
            self._anim = True
            self._last_t = time.perf_counter()
            self._tick()

    def jump(self, y):
        self.scroll_by(y - self.target)

    def _tick(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._last_t)
        self._last_t = now
        cur = self.canvas.canvasy(0)
        d = self.target - cur
        if abs(d) <= 0.5:
            step, done = d, True
        else:
            step, done = d * (1 - math.exp(-dt * 16)), False     # frame-rate independent easing
            if abs(step) < 1:
                step = math.copysign(1, d)
        self.canvas.yview_moveto((cur + step) / self.total)
        if done:
            self._anim = False
        else:
            self.after(6, self._tick)

    def _wheel(self, e):
        if e.num == 4:
            dy = -1.0
        elif e.num == 5:
            dy = 1.0
        elif sys.platform == "darwin":
            dy = -e.delta / 20
        else:
            dy = -e.delta / 120
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            w = None
        if w is not None and w.winfo_toplevel() is not self:
            if isinstance(w, tk.Listbox):
                w.yview_scroll(int(dy * 3) or (1 if dy > 0 else -1), "units")
            return
        if self.home_on:
            if self.home is not None:
                self.home.wheel(w, dy)
            return
        if w is not None and self._in_side(w):
            # pointer is over the side panel: scroll that list, never the reader
            if w in (self.lib, self.hist) and self.focus_get() is not w:
                w.yview_scroll(int(dy * 3) or (1 if dy > 0 else -1), "units")
            return
        if not self.tops:
            return
        if e.state & 0x4:  # Ctrl + wheel = zoom width
            self.nudge_width(5 if dy < 0 else -5)
        else:
            self.scroll_by(dy * self.S(110))

    def _in_side(self, w):
        p, side = str(w), str(self.side)
        return p == side or p.startswith(side + ".")

    def _seek(self, x):
        cw = max(1, self.canvas.winfo_width())
        y = max(0.0, min(float(self._max_y()), x / cw * self._max_y()))
        self._anim = False
        self.canvas.yview_moveto(y / self.total)
        self.target = y

    def _press(self, e):
        self.canvas.focus_set()
        if self.paths and e.y <= self.seek_zone:
            self._seeking = True
            self._seek(e.x)
            return
        self._drag_y = e.y

    def _drag(self, e):
        if self._seeking:
            self._seek(e.x)
            return
        if self._drag_y is None or not self.tops:
            return
        dy = self._drag_y - e.y
        self._drag_y = e.y
        self._anim = False
        y = max(0, min(self._max_y(), self.canvas.canvasy(0) + dy))
        self.canvas.yview_moveto(y / self.total)
        self.target = y

    def _release(self, e):
        self._drag_y = None
        self._seeking = False

    def _motion(self, e):
        self.canvas.configure(cursor="sb_h_double_arrow" if (self.paths and e.y <= self.seek_zone) else "")
        self.flash_hud()

    def nudge_width(self, d):
        self.pct.set(max(30, min(100, self.pct.get() + d)))
        self.sched_relayout()

    # --------------------------------------------------------- auto-scroll
    def toggle_auto(self):
        self.auto.set(not self.auto.get())
        self.canvas.focus_set()

    def _auto_tick(self):
        if self.auto.get() and self.tops and self.target < self._max_y():
            self.scroll_by(self.speed.get() * self.ui * 0.03)
        self.after(30, self._auto_tick)

    # ---------------------------------------------------------- side panel
    def _build_side_panel(self):
        S = self.S
        self.side = ttk.Frame(self.paned)
        self.tabs = ttk.Notebook(self.side)
        self.tabs.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.refresh_panel())

        # ---- Library tab: directory loader ----
        lib = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(lib, text="Library")
        bar = ttk.Frame(lib)
        bar.pack(fill="x")
        ttk.Button(bar, text="Set root", command=self.pick_library_root).pack(side="left")
        ttk.Button(bar, text="Up", command=self.library_up).pack(side="left", padx=4)
        ttk.Button(bar, text="Refresh", command=self.populate_library).pack(side="left")
        self.lib_path = ttk.Label(lib, anchor="w", style="Dim.TLabel")
        self.lib_path.pack(fill="x", pady=5)

        # packed from the bottom first so the tree takes whatever is left
        ttk.Button(lib, text="Open selected", command=self._lib_open).pack(side="bottom", fill="x", pady=(6, 0))
        cov = ttk.Frame(lib, height=S(190))
        cov.pack(side="bottom", fill="x", pady=(6, 0))
        cov.pack_propagate(False)
        self.cover_lbl = ttk.Label(cov, anchor="center", style="Dim.TLabel", text="Select a folder for a preview")
        self.cover_lbl.pack(fill="both", expand=True)
        self.cover_photo = None

        wrap = ttk.Frame(lib)
        wrap.pack(side="top", fill="both", expand=True)
        self.lib = ttk.Treeview(wrap, columns=("imgs", "read"), selectmode="browse")
        self.lib.heading("#0", text="Folder", anchor="w")
        self.lib.heading("imgs", text="Imgs")
        self.lib.heading("read", text="Read")
        self.lib.column("#0", width=S(170), stretch=True)
        self.lib.column("imgs", width=S(48), anchor="e", stretch=False)
        self.lib.column("read", width=S(48), anchor="e", stretch=False)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.lib.yview)
        self.lib.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.lib.pack(side="left", fill="both", expand=True)
        self.lib.bind("<<TreeviewOpen>>", lambda e: self._lib_ensure(self.lib.focus()))
        self.lib.bind("<<TreeviewSelect>>", self._lib_select)
        self.lib.bind("<Double-1>", self._lib_dblclick)
        self.lib.bind("<Return>", lambda e: self._lib_open())
        self.lib.bind("<Button-3>", self._lib_menu)
        self.lib.bind("<Button-2>", self._lib_menu)

        root = self.cfg.get("library_root")
        if not (root and os.path.isdir(root)):
            last = self.cfg.get("last_dir")
            root = os.path.dirname(last) if last and os.path.isdir(last) else os.path.expanduser("~")
        self.lib_root = root

        # ---- History tab ----
        hist = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(hist, text="History")
        hbar = ttk.Frame(hist)
        hbar.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(hbar, text="Open", command=self._hist_open).pack(side="left")
        ttk.Button(hbar, text="Remove", command=self._hist_remove).pack(side="left", padx=4)
        ttk.Button(hbar, text="Clear", command=self._hist_clear).pack(side="left")
        hwrap = ttk.Frame(hist)
        hwrap.pack(side="top", fill="both", expand=True)
        self.hist = ttk.Treeview(hwrap, columns=("read", "when"), selectmode="browse")
        self.hist.heading("#0", text="Title", anchor="w")
        self.hist.heading("read", text="Read")
        self.hist.heading("when", text="Opened")
        self.hist.column("#0", width=S(150), stretch=True)
        self.hist.column("read", width=S(45), anchor="e", stretch=False)
        self.hist.column("when", width=S(90), anchor="e", stretch=False)
        hsb = ttk.Scrollbar(hwrap, orient="vertical", command=self.hist.yview)
        self.hist.configure(yscrollcommand=hsb.set)
        hsb.pack(side="right", fill="y")
        self.hist.pack(side="left", fill="both", expand=True)
        self.hist.bind("<Double-1>", self._hist_dblclick)
        self.hist.bind("<Return>", lambda e: self._hist_open())

        self.populate_library()
        self.populate_history()

    def set_panel(self, show):
        """Idempotent: shows or hides the panel and always leaves the button in sync."""
        show = bool(show)
        try:
            if show and not self.panel_visible:
                self._panel_was = False
                self.paned.insert(0, self.side, weight=0)
                w = int(self.cfg.get("panel_w", self.S(300)))
                self.after(40, lambda: self._set_sash(w))
                try:
                    self.refresh_panel()
                except Exception:
                    pass
            elif not show and self.panel_visible:
                try:
                    self.cfg["panel_w"] = self.paned.sashpos(0)
                except tk.TclError:
                    pass
                self.paned.forget(self.side)
        finally:
            self.panel_var.set(self.panel_visible)

    def toggle_panel(self):
        self.set_panel(not self.panel_visible)

    def _set_sash(self, w):
        try:
            self.paned.sashpos(0, w)
        except tk.TclError:
            pass

    def _read_pct(self, path):
        r = self.cfg["read"].get(path)
        return f"{r}%" if r is not None else ""

    def refresh_panel(self):
        def walk(node):
            for c in self.lib.get_children(node):
                if not c.startswith(("dummy:", "loading:")):
                    self.lib.set(c, "read", self._read_pct(c))
                    walk(c)
        walk("")
        self.populate_history()

    # ---- library (directory scans happen on worker threads) ----
    @staticmethod
    def _scan_dir(path):
        n, sub = 0, False
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            sub = sub or not e.name.startswith(".")
                        elif os.path.splitext(e.name)[1].lower() in EXTS:
                            n += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return n, sub

    @staticmethod
    def _scan_children(ready, token, node):
        entries = []
        try:
            with os.scandir(node) as it:
                dirs = [e for e in it if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)]
            for e in sorted(dirs, key=lambda e: natural_key(e.name)):
                n, sub = WebtoonReader._scan_dir(e.path)
                entries.append((e.path, e.name, n, sub))
        except OSError:
            pass
        ready.put(("lib", token, node, WebtoonReader._scan_dir(node)[0], entries))

    def populate_library(self):
        self.lib_token += 1
        self.lib.delete(*self.lib.get_children())
        root = self.lib_root
        self.lib_path.config(text=root)
        if not os.path.isdir(root):
            return
        self.lib.insert("", "end", iid=root, text=os.path.basename(root) or root,
                        values=("", self._read_pct(root)))
        self._lib_fill(root)
        self.lib.item(root, open=True)

    def _lib_fill(self, node):
        self.lib.delete(*self.lib.get_children(node))
        self.lib.insert(node, "end", iid="loading:" + node, text="Loading\u2026")
        self.io_pool.submit(self._scan_children, self.ready, self.lib_token, node)

    def _on_lib(self, token, node, count, entries):
        if token != self.lib_token or not self.lib.exists(node):
            return
        self.lib.delete(*self.lib.get_children(node))
        self.lib.set(node, "imgs", count or "")
        for path, name, n, sub in entries:
            if self.lib.exists(path):
                continue
            self.lib.insert(node, "end", iid=path, text=name, values=(n or "", self._read_pct(path)))
            if sub:
                self.lib.insert(path, "end", iid="dummy:" + path, text="")

    def _lib_ensure(self, iid):
        kids = self.lib.get_children(iid) if iid else ()
        if kids and kids[0].startswith("dummy:"):
            self._lib_fill(iid)

    def _lib_select(self, e=None):
        sel = self.lib.selection()
        if not sel or sel[0].startswith(("dummy:", "loading:")):
            return
        self.cover_token += 1
        iid = sel[0]
        if self.lib.set(iid, "imgs"):
            self.io_pool.submit(self._make_cover, self.ready, self.cover_token, iid, self.S(260), self.S(180))
        else:
            self.cover_photo = None
            self.cover_lbl.configure(image="", text="No images in this folder")

    @staticmethod
    def _make_cover(ready, token, folder, w, h):
        try:
            imgs = list_images(folder)
            with Image.open(imgs[0]) as im:
                im = im.convert("RGB")
                im.thumbnail((w, h), SMOOTH)
            ready.put(("cover", token, im))
        except Exception:
            ready.put(("cover", token, None))

    def _on_cover(self, token, im):
        if token != self.cover_token:
            return
        if im is None:
            self.cover_photo = None
            self.cover_lbl.configure(image="", text="No preview")
            return
        self.cover_photo = ImageTk.PhotoImage(im)
        self.cover_lbl.configure(image=self.cover_photo, text="")

    def _lib_dblclick(self, e):
        iid = self.lib.identify_row(e.y)
        if iid:
            self.lib.selection_set(iid)
            self._lib_open()

    def _lib_open(self):
        sel = self.lib.selection()
        if not sel or sel[0].startswith(("dummy:", "loading:")):
            return
        iid = sel[0]
        if self.lib.set(iid, "imgs"):
            self.open_path(iid)
        else:
            self._lib_ensure(iid)
            is_open = str(self.lib.item(iid, "open")).lower() in ("1", "true")
            self.lib.item(iid, open=not is_open)

    def pick_library_root(self):
        d = filedialog.askdirectory(initialdir=self.lib_root)
        if d:
            self.set_library_root(d)

    def library_up(self):
        parent = os.path.dirname(self.lib_root.rstrip("/\\"))
        if parent and parent != self.lib_root:
            self.set_library_root(parent)

    def set_library_root(self, d):
        self.lib_root = os.path.abspath(d)
        self.cfg["library_root"] = self.lib_root
        self.populate_library()
        self.home_rescan()

    # ---- history ----
    def add_history(self, folder):
        hist = [h for h in self.cfg["history"] if h.get("path") != folder]
        hist.insert(0, {"path": folder, "time": datetime.now().strftime("%b %d %H:%M"), "ts": time.time()})
        self.cfg["history"] = hist[:100]

    def populate_history(self):
        self.hist.delete(*self.hist.get_children())
        for i, h in enumerate(self.cfg["history"]):
            p = h.get("path", "")
            parent = os.path.basename(os.path.dirname(p))
            name = os.path.basename(p) or p
            title = f"{parent} / {name}" if parent else name
            if not os.path.isdir(p):
                title += "  (missing)"
            self.hist.insert("", "end", iid=str(i), text=title,
                             values=(self._read_pct(p), h.get("time", "")))

    def _hist_index(self):
        sel = self.hist.selection()
        return int(sel[0]) if sel else None

    def _hist_dblclick(self, e):
        iid = self.hist.identify_row(e.y)
        if iid:
            self.hist.selection_set(iid)
            self._hist_open()

    def _hist_open(self):
        i = self._hist_index()
        if i is None:
            return
        p = self.cfg["history"][i]["path"]
        if os.path.isdir(p):
            self.open_path(p)
        else:
            self.status.config(text=f"Folder not found: {p}")

    def _hist_remove(self):
        i = self._hist_index()
        if i is not None:
            del self.cfg["history"][i]
            self.populate_history()

    def _hist_clear(self):
        if self.cfg["history"] and messagebox.askyesno("Clear history", "Clear all reading history?"):
            self.cfg["history"] = []
            self.populate_history()

    # ------------------------------------------------- discover / library extras
    def _theme_dl(self):
        t, s = self.t, self.style
        s.configure("TEntry", fieldbackground=t["bg"], foreground=t["fg"], insertcolor=t["fg"],
                    bordercolor=t["border"], lightcolor=t["border"], darkcolor=t["border"])
        s.configure("TCombobox", fieldbackground=t["bg"], background=t["border"], foreground=t["fg"],
                    arrowcolor=t["fg"], bordercolor=t["border"], lightcolor=t["border"],
                    darkcolor=t["border"], selectbackground=t["bg"], selectforeground=t["fg"])
        s.map("TCombobox", fieldbackground=[("readonly", t["bg"]), ("disabled", t["panel"])],
              foreground=[("readonly", t["fg"]), ("disabled", t["dim_p"])],
              selectbackground=[("readonly", t["bg"])], selectforeground=[("readonly", t["fg"])])
        self.option_add("*TCombobox*Listbox.background", t["bg"])
        self.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", t["bg"])
        s.configure("Accent.TButton", background=t["accent"], foreground=t["bg"], padding=(14, 5),
                    borderwidth=0)
        s.map("Accent.TButton", background=[("active", t["accent2"]), ("disabled", t["border"])],
              foreground=[("disabled", t["dim_p"])])
        cb, cs = mix(t["bg"], t["fg"], 0.07), mix(t["bg"], t["accent"], 0.38)
        for sel, bg in (("", cb), ("Sel", cs)):
            s.configure(f"Card{sel}.TFrame", background=bg)
            s.configure(f"CardTitle{sel}.TLabel", background=bg, foreground=t["fg"], font=self.f_small)
            s.configure(f"CardDim{sel}.TLabel", background=bg, foreground=mix(t["fg"], bg, 0.45),
                        font=self.f_small)
        if self.home is not None:
            self.home.retheme()

    def themed_menu(self, parent):
        t = self.t
        return tk.Menu(parent, tearoff=0, bg=t["panel"], fg=t["fg"], activebackground=t["accent"],
                       activeforeground=t["bg"], bd=0, relief="flat")

    def show_home(self, page=None):
        if self.home is None:
            return
        self.save_progress()
        self.home.place(in_=self, x=0, y=0, relwidth=1, relheight=1)
        self.home.lift()
        self.home_on = True
        self.home.show(page or self.home.last_main)
        self.title("Webtoon Reader")

    def hide_home(self):
        if not self.home_on:
            return
        self.home.place_forget()
        self.home_on = False
        self.canvas.focus_set()

    def home_rescan(self):
        if self.home is not None:
            self.home.rescan()

    def show_discover(self):
        if self.home is None:
            return
        self.show_home()
        self.home.show_browse()

    def ensure_dl_dir(self):
        if self.cfg.get("dl_dir"):
            return True
        parent = self.dm if self.dm is not None else self
        messagebox.showinfo("Download folder", "Choose a folder for downloads.\nIt also becomes your "
                            "Library root, so new books show up in the reader.", parent=parent)
        d = filedialog.askdirectory(parent=parent, initialdir=self.lib_root)
        if not d:
            return False
        self.cfg["dl_dir"] = d
        self.set_library_root(d)
        if self.dm is not None:
            self.dm._update_dir_label()
        return True

    def _on_dl(self, kind, *a):
        if kind == "sites":
            self.dl_tag_sites = a[0]
            if self.home is not None:
                self.home.on_sites()
        if self.dm is not None:
            try:
                self.dm.on_msg(kind, *a)
            except tk.TclError:
                pass

    def continue_reading(self):
        for h in self.cfg["history"]:
            p = h.get("path", "")
            if os.path.isdir(p):
                self.open_path(p)
                return
        self.status.config(text="Nothing to resume yet.")

    def _lib_menu(self, e):
        iid = self.lib.identify_row(e.y)
        if not iid or iid.startswith(("dummy:", "loading:")):
            return
        self.lib.selection_set(iid)
        m = self.themed_menu(self)
        m.add_command(label="Open", command=self._lib_open)
        m.add_command(label="Show in file manager", command=lambda: self._reveal(iid))
        m.add_separator()
        m.add_command(label="Delete folder\u2026", command=lambda: self._delete_folder(iid))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    @staticmethod
    def _reveal(path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def _delete_folder(self, path):
        root, p = os.path.abspath(self.lib_root), os.path.abspath(path)
        try:
            inside = os.path.commonpath([root, p]) == root and p != root
        except ValueError:
            inside = False
        if not inside:
            messagebox.showinfo("Delete folder", "Only folders inside the library root can be deleted here.")
            return
        if not messagebox.askyesno(
                "Delete folder",
                f"Permanently delete this folder and everything in it?\n\n{p}\n\n"
                "Tip: the download archive remembers deleted files. To download this book again, "
                "turn off 'Skip files already downloaded' in the Discover settings first.",
                icon="warning"):
            return
        try:
            shutil.rmtree(p)
        except OSError as ex:
            messagebox.showerror("Delete folder", f"Could not delete:\n{ex}")
            return
        self.cfg["history"] = [h for h in self.cfg["history"] if not str(h.get("path", "")).startswith(p)]
        self.populate_library()
        self.populate_history()
        self.home_rescan()
        if self.home is not None:
            self.home.refresh_pages()
        self.status.config(text=f"Deleted {os.path.basename(p)}")

    # --------------------------------------------------------------- window
    def toggle_toolbar(self):
        if self._toolbar_visible:
            self.toolbar.pack_forget()
            self.strip.pack_forget()
        else:
            self.toolbar.pack(side="top", fill="x", before=self.paned)
            self.strip.pack(side="top", fill="x", before=self.paned)
        self._toolbar_visible = not self._toolbar_visible

    def toggle_zen(self):
        self._zen = not self._zen
        if self._zen:
            self._zen_state = (self._toolbar_visible, self.panel_visible)
            if self._toolbar_visible:
                self.toggle_toolbar()
            self.set_panel(False)
            self.status.pack_forget()
        else:
            tb, pv = self._zen_state
            self.status.pack(side="bottom", fill="x", before=self.paned)
            if tb and not self._toolbar_visible:
                self.toggle_toolbar()
            if pv:
                self.set_panel(True)

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        if self._fullscreen and self._toolbar_visible:
            self.toggle_toolbar()
        elif not self._fullscreen and not self._toolbar_visible and not self._zen:
            self.toggle_toolbar()
        if self._fullscreen and self.panel_visible:
            self.set_panel(False)
            self._panel_was = True
        elif not self._fullscreen and self._panel_was and not self._zen:
            self.set_panel(True)

    def escape(self):
        if self._fullscreen:
            self.toggle_fullscreen()
        elif self._zen:
            self.toggle_zen()
        elif self.home_on:
            self.home.escape()
        else:
            self.show_home()

    def save_progress(self):
        if self.folder and self.total > 1:
            self.cfg["progress"][self.folder] = round(self.canvas.canvasy(0) / self.total, 5)

    def on_close(self):
        self.save_progress()
        if self.panel_visible:
            try:
                self.cfg["panel_w"] = self.paned.sashpos(0)
            except tk.TclError:
                pass
        self.cfg["panel_visible"] = (self.panel_visible or self._panel_was
                                     or (self._zen and self._zen_state[1]))
        self.cfg.update(theme=self.theme.get(), width_pct=self.pct.get(),
                        gap=self.gap.get(), speed=self.speed.get())
        if self.home is not None:
            self.cfg["home_page"] = self.home.last_main
        if self.dm is not None:
            self.dm.save_state()
            self.dm.shutdown()
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except OSError:
            pass
        for pool in (self.pool, self.io_pool, self.dl_pool, self.info_pool):
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=False)
        self.destroy()


if __name__ == "__main__":
    WebtoonReader(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
