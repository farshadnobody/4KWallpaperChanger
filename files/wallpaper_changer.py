#!/usr/bin/env python3
"""
4K Wallpaper Changer  (tray app for 4kwallpapers.com)
=====================================================
* Auto mode  : random wallpaper from the site (categories / popular / featured ...).
               Offline -> loops over the last 20/40 wallpapers saved on disk.
* Liked loop : loops over the wallpapers you liked + ticked. Files are saved locally,
               so it never re-downloads and works offline.

Run          :  python wallpaper_changer.py
Tray only    :  pythonw wallpaper_changer.py --hidden
Once & exit  :  python wallpaper_changer.py --once
Debug page   :  python wallpaper_changer.py --debug <wallpaper-page-url>

Deps: pip install -r requirements.txt
"""
import argparse
import ctypes
import json
import os
import platform
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://4kwallpapers.com"
APP_DIR = Path.home() / ".4kwall"
PICS = Path.home() / "Pictures" / "4KWallpaperChanger"
RECENT_DIR = PICS / "recent"
LIKED_DIR = PICS / "liked"
THUMB_DIR = APP_DIR / "thumbs"
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.json"
RECENT_PATH = APP_DIR / "recent.json"
LIKES_PATH = APP_DIR / "likes.json"
CURRENT_PATH = APP_DIR / "current.json"
PLAYED_PATH = APP_DIR / "played.json"      # what was shown, newest last (for "Previous")

# ---- theme (colours sampled from the site's dark theme) -------------------- #
BG = "#171c28"
HEADER = "#121722"
SURFACE = "#1f2635"
SURFACE2 = "#2a3346"
BORDER = "#34405a"
TEXT = "#f2f5fa"
MUTED = "#8b97ad"
ACCENT = "#7cfc00"
ACCENT_HOVER = "#a3ff47"
ACCENT_TEXT = "#0d1117"
ACCENT_DEEP = "#3f8f0a"

CATEGORIES = {
    "Abstract": "abstract", "Animals": "animals", "Anime": "anime",
    "Architecture": "architecture", "Bikes": "bikes", "Black/Dark": "black-dark",
    "Cars": "cars", "Celebrations": "celebrations", "Cute": "cute",
    "Fantasy": "fantasy", "Flowers": "flowers", "Food": "food", "Games": "games",
    "Gradients": "gradients", "CGI": "graphics-cgi", "Lifestyle": "lifestyle",
    "Love": "love", "Military": "military", "Minimal": "minimal",
    "Movies": "movies", "Music": "music", "Nature": "nature", "People": "people",
    "Photography": "photography", "Quotes": "quotes", "Sci-Fi": "sci-fi",
    "Space": "space", "Sports": "sports", "Technology": "technology",
    "World": "world",
}


def clean_categories(value, fallback=()):
    """Keep only slugs that really exist on the site (drops old / mistyped ones), in site order."""
    if not isinstance(value, list):
        return list(fallback)
    return [s for s in CATEGORIES.values() if s in value]


FEEDS = {
    "Recent (all)": "/",
    "Popular": "/most-popular-4k-wallpapers/",
    "Featured": "/best-4k-wallpapers/",
    "Random": "/random-wallpapers/",
}

QUALITIES = {"low": "Low", "4k": "4K", "high": "High"}
LOCK_MODES = {"off": "Don't touch", "windows": "Windows default", "same": "Same as desktop",
              "liked": "Liked wallpapers", "auto": "Auto (from the site)", "fixed": "Fixed image"}
LOCK_INTERVALS = {60: "1 hour", 180: "3 hours", 360: "6 hours", 720: "12 hours", 1440: "24 hours"}

DEFAULT_CONFIG = {
    "mode": "auto",                      # auto | liked
    "source": "Selected categories",     # or a FEEDS key
    "categories": ["nature", "space", "abstract"],
    "intervals": {"auto": 30, "liked": 10},   # minutes, per mode
    "max_pages": 15,
    "min_width": 0,                      # 0 = screen width (used by "low" quality)
    "quality": "low",                    # low = smallest size covering the screen | 4k | high = original
    "keep_recent": 40,                   # 10, 20 or 40
    "request_delay": 1.0,
    "auto": True,                        # False = paused
    "autostart": False,
    "liked_last": None,
    "pinned": None,                      # id of the desktop wallpaper the timer must not change
    # Windows lock / sign-in screen (own tab): mode, timer, fixed image, and for "auto" its own
    # categories / search depth / quality / how many downloaded images to keep
    "lock": {"mode": "off", "interval": 180, "fixed": "", "fixed_name": "",
             "source": "Selected categories", "quality": "low", "max_pages": 15, "keep": 10, "categories": None,
             "pin": None},               # file of the lock screen picture the timer must not change
}

UA = "4KWallpaperChanger/2.0 (personal desktop app; low-rate)"
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "en"})
_last_request = 0.0
_req_lock = threading.Lock()
DATA_LOCK = threading.RLock()
_screen_cache = None


# --------------------------------------------------------------------------- #
# json storage
# --------------------------------------------------------------------------- #
def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, data):
    with DATA_LOCK:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    saved = _load(CONFIG_PATH, {})
    # migrate v1 config
    if saved.get("mode") not in (None, "auto", "liked"):
        saved["source"] = saved.pop("mode")
    if "interval_min" in saved:
        cfg["intervals"]["auto"] = saved.pop("interval_min")
    saved.pop("keep_files", None)
    iv = saved.pop("intervals", {})
    lk = saved.pop("lock", {})
    cfg.update(saved)
    cfg["intervals"].update(iv)
    cfg["lock"].update(lk if isinstance(lk, dict) else {})
    if cfg["lock"]["mode"] not in LOCK_MODES:
        cfg["lock"]["mode"] = "off"
    if cfg["lock"]["interval"] not in LOCK_INTERVALS:
        cfg["lock"]["interval"] = 180
    cfg["categories"] = clean_categories(cfg.get("categories"), DEFAULT_CONFIG["categories"])
    lkc = cfg["lock"]
    if lkc.get("quality") not in QUALITIES:
        lkc["quality"] = "low"
    if lkc.get("source") not in (["Selected categories"] + list(FEEDS)):
        lkc["source"] = "Selected categories"
    if lkc.get("keep") not in (10, 20, 40):
        lkc["keep"] = 10
    try:
        lkc["max_pages"] = min(500, max(1, int(lkc.get("max_pages", 15))))
    except (TypeError, ValueError):
        lkc["max_pages"] = 15
    if not isinstance(lkc.get("categories"), list):          # first run: start from the desktop's list
        lkc["categories"] = list(cfg["categories"])
    lkc["categories"] = clean_categories(lkc["categories"])
    if cfg["mode"] not in ("auto", "liked"):
        cfg["mode"] = "auto"
    if cfg["keep_recent"] not in (10, 20, 40):
        cfg["keep_recent"] = 40
    if cfg.get("quality") not in QUALITIES:
        cfg["quality"] = "low"
    return cfg


def save_config(cfg):
    _save(CONFIG_PATH, cfg)


def load_recent():
    return _load(RECENT_PATH, [])


def load_likes():
    return _load(LIKES_PATH, [])


def load_current():
    return _load(CURRENT_PATH, None)


def load_history():
    return _load(HISTORY_PATH, [])


def is_liked(wid):
    return any(l["id"] == wid for l in load_likes())


# --------------------------------------------------------------------------- #
# HTTP (rate-limited)
# --------------------------------------------------------------------------- #
def http_get(url, delay=1.0, **kw):
    global _last_request
    with _req_lock:
        wait = delay - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
    kw.setdefault("timeout", 20)
    r = _session.get(url, **kw)
    r.raise_for_status()
    return r


def is_online(host="4kwallpapers.com", timeout=4):
    try:
        socket.create_connection((host, 443), timeout=timeout).close()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def parse_listing(html):
    """Return (list of wallpaper-page URLs, highest page number found)."""
    soup = BeautifulSoup(html, "html.parser")
    urls, seen = [], set()
    max_page = 1
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            max_page = max(max_page, int(m.group(1)))
        img = a.find("img")
        if not img:
            continue
        src = img.get("src") or img.get("data-src") or ""
        if "/images/walls/thumbs/" not in src:
            continue
        full = urljoin(BASE + "/", href)
        if full.endswith(".html") and full not in seen:
            seen.add(full)
            urls.append(full)
    return urls, max_page


def wallpaper_id(url):
    m = re.search(r"-(\d+)\.html", url)
    return m.group(1) if m else url


def url_category(url):
    """'https://4kwallpapers.com/cars/foo-123.html' -> 'cars' (None if the URL has no category)."""
    parts = urlparse(url or "").path.strip("/").split("/")
    return parts[0] if len(parts) >= 2 else None


NO_CAT = "?"                                   # liked image whose category is not known (yet)
CAT_LABEL = {slug: label for label, slug in CATEGORIES.items()}


def liked_category(entry):
    """Category slug of a liked wallpaper (from its page address), or NO_CAT when unknown."""
    return url_category(entry.get("page_url")) or NO_CAT


def liked_cat_options(likes):
    """{menu text: category slug (None = all)} - only categories that have liked images."""
    counts = {}
    for l in likes:
        c = liked_category(l)
        counts[c] = counts.get(c, 0) + 1
    known = list(CATEGORIES.values())
    order = ([c for c in known if c in counts]
             + sorted(c for c in counts if c not in known and c != NO_CAT)
             + ([NO_CAT] if NO_CAT in counts else []))
    opts = {f"All categories ({len(likes)})": None}
    for c in order:
        name = CAT_LABEL.get(c) or ("Unknown" if c == NO_CAT else c.replace("-", " ").title())
        opts[f"{name} ({counts[c]})"] = c
    return opts


def liked_view(likes, cat, sort):
    """The liked wallpapers filtered by category (None = all) and sorted newest / oldest first."""
    ranked = sorted(enumerate(likes), reverse=(sort == "new"),
                    key=lambda t: (t[1].get("liked_ts") or t[1].get("ts") or 0, t[0]))
    return [l for _, l in ranked if cat is None or liked_category(l) == cat]


def parse_title(html, url=""):
    soup = BeautifulSoup(html, "html.parser")
    t = None
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
    if not t:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            t = og["content"]
    if not t and soup.title:
        t = soup.title.get_text(strip=True)
    if t:
        t = re.sub(r"\s+Wallpapers?\b.*$", "", t, flags=re.I).strip(" -|")
    if not t:
        m = re.search(r"/([^/]+?)-\d+\.html", url)
        t = m.group(1).replace("-", " ").title() if m else "Wallpaper"
    return t


def parse_detail(html):
    """Return list of (width, height, url) download candidates on a wallpaper page."""
    soup = BeautifulSoup(html, "html.parser")
    cands, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"\.(jpe?g|png|webp)(\?.*)?$", href, re.I):
            continue
        if "/images/wallpapers/" not in href:
            continue
        full = urljoin(BASE + "/", href)
        if full in seen:
            continue
        seen.add(full)
        m = (re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", href)
             or re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", a.get_text(" ")))
        w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        cands.append((w, h, full))
    if not cands:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            cands.append((0, 0, urljoin(BASE + "/", og["content"])))
    return cands


def choose_candidate(cands, screen_w, screen_h, min_w=0):
    """Smallest image that still covers the screen; else the largest."""
    if not cands:
        return None
    target_w = min_w or screen_w
    landscape = screen_w >= screen_h
    known = [c for c in cands if c[0] and c[1]]
    if known:
        same = [c for c in known if (c[0] >= c[1]) == landscape]
        if not same:
            return None
        big = [c for c in same if c[0] >= target_w]
        if big:
            return min(big, key=lambda c: c[0])
        return max(same, key=lambda c: c[0])
    return cands[0]


def labelled_links(html):
    """The page's two big download buttons: 'Download in 4K (..)' and 'Download Original (..)'."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/images/wallpapers/" not in href or not re.search(r"\.(jpe?g|png|webp)(\?.*)?$", href, re.I):
            continue
        txt = a.get_text(" ", strip=True).lower()
        full = urljoin(BASE + "/", href)
        if "original" in txt:
            out["high"] = full
        elif re.search(r"\b4k\b", txt):
            out["4k"] = full
    return out


def pick_image(html, screen_w, screen_h, cfg):
    """Choose the file to download for the configured quality: low | 4k | high."""
    cands = parse_detail(html)
    quality = cfg.get("quality", "low")
    if quality == "low":
        return choose_candidate(cands, screen_w, screen_h, cfg.get("min_width", 0))
    landscape = screen_w >= screen_h
    known = [c for c in cands if c[0] and c[1]]
    same = [c for c in known if (c[0] >= c[1]) == landscape]
    if known and not same:                                  # wrong orientation, try another wallpaper
        return None
    links = labelled_links(html)
    by_url = {c[2]: c for c in cands}
    if quality == "high":
        if "high" in links:
            return by_url.get(links["high"], (0, 0, links["high"]))
        return max(same, key=lambda c: c[0] * c[1]) if same else (cands[0] if cands else None)
    if "4k" in links:                                       # quality == "4k"
        return by_url.get(links["4k"], (0, 0, links["4k"]))
    if same:
        return min(same, key=lambda c: abs(max(c[0], c[1]) - 3840))
    return cands[0] if cands else None


# --------------------------------------------------------------------------- #
# fetching from the site
# --------------------------------------------------------------------------- #
def listing_url(path, page):
    url = BASE + path
    return url if page <= 1 else f"{url}{'&' if '?' in url else '?'}page={page}"


def pick_listing_path(cfg):
    if cfg["source"] in FEEDS:
        return FEEDS[cfg["source"]]
    slugs = cfg.get("categories") or list(CATEGORIES.values())
    return f"/{random.choice(slugs)}/"


def fetch_random_wallpaper(cfg, screen_w, screen_h, log=print):
    delay = cfg["request_delay"]
    history = load_history()
    # only when browsing "Selected categories": keep wallpapers whose own category is ticked
    allowed = set(cfg.get("categories") or []) if cfg["source"] not in FEEDS else set()
    for _ in range(6):
        path = pick_listing_path(cfg)
        page = random.randint(1, max(1, cfg["max_pages"]))
        log(f"Looking in {path} (page {page}) ...")
        try:
            html = http_get(listing_url(path, page), delay).text
        except requests.HTTPError:
            html = http_get(listing_url(path, 1), delay).text
            page = 1
        urls, max_page = parse_listing(html)
        if page > max_page > 0 and page != 1:
            html = http_get(listing_url(path, max_page), delay).text
            urls, _ = parse_listing(html)
        urls = [u for u in urls if wallpaper_id(u) not in history]
        if allowed:          # the site's category pages also list wallpapers of OTHER categories
            urls = [u for u in urls if url_category(u) in allowed]
        if not urls:
            continue
        random.shuffle(urls)
        for page_url in urls[:3]:
            detail = http_get(page_url, delay).text
            best = pick_image(detail, screen_w, screen_h, cfg)
            if best:
                return page_url, best[2], parse_title(detail, page_url)
    raise RuntimeError("No suitable wallpaper found (run with --debug to check the page).")


def download(img_url, referer, delay=1.0, dest_dir=RECENT_DIR, progress=None):
    """progress(p) is called with p = 0..1 while the file is coming in (when the size is known)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = img_url.split("?")[0].rsplit("/", 1)[-1]
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        if progress:
            progress(1.0)
        return dest
    r = http_get(img_url, delay, headers={"Referer": referer}, stream=True, timeout=120)
    try:
        total = int(r.headers.get("Content-Length") or 0)
    except ValueError:
        total = 0
    done = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
            if progress and total:
                done += len(chunk)
                progress(min(1.0, done / total))
    tmp.replace(dest)
    return dest


# --------------------------------------------------------------------------- #
# thumbnails / recent / likes
# --------------------------------------------------------------------------- #
def make_thumb(src, wid):
    from PIL import Image, ImageOps
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    dest = THUMB_DIR / f"{wid}.jpg"
    if dest.exists():
        return dest
    try:
        with Image.open(src) as im:
            im.draft("RGB", (1280, 720))      # fast JPEG downscale
            im = ImageOps.fit(im.convert("RGB"), (640, 360), Image.LANCZOS)
            im.save(dest, "JPEG", quality=82)
        return dest
    except Exception:
        return None


def make_entry(page_url, img_url, title, file):
    return {"id": wallpaper_id(page_url), "page_url": page_url, "img_url": img_url,
            "title": title, "file": str(file), "ts": int(time.time())}


def prune_thumbs():
    keep = {e["id"] for e in load_recent()} | {e["id"] for e in load_likes()}
    cur = load_current()
    if cur:
        keep.add(cur["id"])
    if THUMB_DIR.exists():
        for p in THUMB_DIR.glob("*.jpg"):
            if p.stem not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass


def trim_recent(keep):
    """Keep only the newest `keep` saved wallpapers; delete older files."""
    with DATA_LOCK:
        rec = load_recent()
        removed = []
        while len(rec) > keep:
            removed.append(rec.pop(0))
        if removed:
            _save(RECENT_PATH, rec)
            for r in removed:
                try:
                    Path(r["file"]).unlink()
                except OSError:
                    pass
            prune_thumbs()


def add_recent(entry, keep):
    with DATA_LOCK:
        rec = [e for e in load_recent() if e["id"] != entry["id"]]
        rec.append(entry)
        _save(RECENT_PATH, rec)
        trim_recent(keep)


def toggle_like(entry):
    """Like/unlike. Liking copies the file into the liked folder. Returns new state."""
    with DATA_LOCK:
        likes = load_likes()
        for l in likes:
            if l["id"] == entry["id"]:
                likes.remove(l)
                _save(LIKES_PATH, likes)
                try:
                    Path(l["file"]).unlink()
                except OSError:
                    pass
                return False
        LIKED_DIR.mkdir(parents=True, exist_ok=True)
        src = Path(entry["file"])
        dest = LIKED_DIR / src.name
        if src.exists() and src != dest:
            shutil.copy2(src, dest)
        new = dict(entry)
        new.update({"file": str(dest), "selected": True, "liked_ts": int(time.time())})
        likes.append(new)
        _save(LIKES_PATH, likes)
        return True


def set_selected(wid, value):
    with DATA_LOCK:
        likes = load_likes()
        for l in likes:
            if l["id"] == wid:
                l["selected"] = bool(value)
        _save(LIKES_PATH, likes)


def set_lock_selected(wid, value):
    with DATA_LOCK:
        likes = load_likes()
        for l in likes:
            if l["id"] == wid:
                l["lock_sel"] = bool(value)
        _save(LIKES_PATH, likes)


def set_all_lock_selected(value):
    with DATA_LOCK:
        likes = load_likes()
        for l in likes:
            l["lock_sel"] = bool(value)
        _save(LIKES_PATH, likes)


def set_all_selected(value):
    with DATA_LOCK:
        likes = load_likes()
        for l in likes:
            l["selected"] = bool(value)
        _save(LIKES_PATH, likes)


def ensure_local(entry, cfg):
    """Make sure the image file exists on disk (download + save if it doesn't)."""
    p = Path(entry["file"])
    if p.exists() and p.stat().st_size > 0:
        return entry
    if not entry.get("img_url"):
        raise RuntimeError("Wallpaper file is missing")
    liked = is_liked(entry["id"])
    path = download(entry["img_url"], entry["page_url"], cfg["request_delay"],
                    LIKED_DIR if liked else RECENT_DIR)
    entry = dict(entry)
    entry["file"] = str(path)
    if liked:
        with DATA_LOCK:
            likes = load_likes()
            for l in likes:
                if l["id"] == entry["id"]:
                    l["file"] = str(path)
            _save(LIKES_PATH, likes)
    make_thumb(path, entry["id"])
    return entry


_QINFO = {}


def file_info(entry):
    """(quality, (w, h)) of a wallpaper file on disk, or (None, None) when it is missing.
    The quality is the one saved when the app downloaded it; otherwise it is guessed from the
    pixel size (>= 4400 px long side = High, >= 3600 = 4K, else Low)."""
    p = Path(entry.get("file") or "")
    try:
        st = p.stat()
    except OSError:
        return None, None
    key = (str(p), st.st_mtime, st.st_size)
    dims = _QINFO.get(key)
    if dims is None:
        try:
            from PIL import Image
            with Image.open(p) as im:
                dims = im.size
        except Exception:
            dims = (0, 0)
        if len(_QINFO) > 500:
            _QINFO.clear()
        _QINFO[key] = dims
    q = entry.get("quality")
    if q not in QUALITIES:
        long_side = max(dims)
        q = None if not long_side else "high" if long_side >= 4400 else "4k" if long_side >= 3600 else "low"
    return q, (dims if dims[0] else None)


def quality_matches(have, want, screen_long):
    if have is None or have == want:
        return True
    # on a 4K screen the smallest size that covers the screen is the 4K file
    return screen_long >= 3600 and {have, want} == {"low", "4k"}


def redownload_liked(wid, cfg, log=print, quality=None, progress=None):
    """Download a liked wallpaper again (quality: low | 4k | high, default = the one in Settings)
    and replace the old file.
    Returns (entry, changed). changed=False when the file on disk already is that version."""
    if quality in QUALITIES:
        cfg = dict(cfg, quality=quality)
    e = next((l for l in load_likes() if l["id"] == wid), None)
    if not e:
        raise RuntimeError("not in Liked any more")
    if not is_online():
        raise RuntimeError("offline")
    delay = cfg["request_delay"]
    page_url = e.get("page_url")
    if not page_url and e.get("slug"):
        res = resolve_page_url(e["slug"], wid, delay)
        page_url = res[0] if res else None
    if not page_url:
        raise RuntimeError("no wallpaper page known for this image")
    html = http_get(page_url, delay).text
    sw, sh = screen_size()
    best = pick_image(html, sw, sh, cfg)
    if not best:
        raise RuntimeError("no suitable image on the page")
    img_url = best[2]
    old = Path(e["file"])
    name = img_url.split("?")[0].rsplit("/", 1)[-1]
    if old.name == name and old.exists() and old.stat().st_size > 0:
        with DATA_LOCK:                                      # same file: just remember its quality
            likes = load_likes()
            for l in likes:
                if l["id"] == wid:
                    l["quality"] = cfg["quality"]
                    e = dict(l)
            _save(LIKES_PATH, likes)
        if progress:
            progress(1.0)
        return e, False
    log("Downloading ...")
    path = download(img_url, page_url, delay, LIKED_DIR, progress=progress)
    with DATA_LOCK:
        likes = load_likes()
        for l in likes:
            if l["id"] == wid:
                l.update(file=str(path), img_url=img_url, page_url=page_url, quality=cfg["quality"])
                e = dict(l)
        _save(LIKES_PATH, likes)
    if old != path and old.parent == LIKED_DIR and old.exists():
        try:
            old.unlink()
        except OSError:
            pass
    try:                                                     # new thumbnail from the new file
        (THUMB_DIR / f"{wid}.jpg").unlink()
    except OSError:
        pass
    make_thumb(path, wid)
    cur = load_current()
    if cur and cur.get("id") == wid:                         # it is the desktop wallpaper right now
        apply_entry(dict(cur, file=str(path), img_url=img_url), record=False)
    return e, True


# --------------------------------------------------------------------------- #
# set wallpaper (per OS)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# lock screen / sign-in screen (Windows)
# --------------------------------------------------------------------------- #
LOCK_DIR = APP_DIR / "lockscreen"
LOCK_DL = PICS / "lockscreen"                  # images downloaded for "Auto (from the site)"
_OLD_LOCK_DL = LOCK_DIR / "dl"                 # where they used to be (moved once, see below)
_lock_last = {"id": None}
LOCK_CURRENT_PATH = APP_DIR / "lock_current.json"     # what the app last put on the lock screen
LOCK_HIST_PATH = APP_DIR / "lock_history.json"        # base name; one history file per mode (auto / liked)


def load_lock_current():
    return _load(LOCK_CURRENT_PATH, None)


def _lock_meta(e, file):
    m = {k: e[k] for k in ("id", "page_url", "img_url", "slug") if e.get(k)}
    m.update(file=str(file), title=e.get("title") or Path(file).stem)
    return m


def _hist_path(mode):
    return LOCK_HIST_PATH.with_name(f"lock_history_{mode}.json")


def lock_hist_add(meta, mode):
    with DATA_LOCK:
        d = _load(_hist_path(mode), {"items": [], "pos": -1})
        items = d.get("items", [])
        if not items or items[-1].get("file") != meta.get("file"):
            items.append(meta)
        items = items[-60:]
        _save(_hist_path(mode), {"items": items, "pos": len(items) - 1})


def lock_hist_move(step, mode):
    """Move through the Prev / Next history of a mode; returns the entry or None."""
    with DATA_LOCK:
        d = _load(_hist_path(mode), {"items": [], "pos": -1})
        items, i = d.get("items", []), d.get("pos", -1) + step
        while 0 <= i < len(items):
            if Path(items[i]["file"]).exists():
                _save(_hist_path(mode), {"items": items, "pos": i})
                return items[i]
            i += step
    return None


def lock_hist_has_forward(mode):
    d = _load(_hist_path(mode), {"items": [], "pos": -1})
    return any(Path(x["file"]).exists() for x in d.get("items", [])[d.get("pos", -1) + 1:])


def spotlight_preview_path():
    """Best effort: the picture Windows Spotlight is showing now. Returns a path or None."""
    if platform.system() != "Windows":
        return None
    import winreg
    cands = []

    def read(root, sub, names):
        try:
            with winreg.OpenKey(root, sub) as k:
                for n in names:
                    try:
                        v, _ = winreg.QueryValueEx(k, n)
                    except OSError:
                        continue
                    if isinstance(v, str) and v:
                        cands.append(v)
        except OSError:
            pass

    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI\Creative"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(64):
                try:
                    sid = winreg.EnumKey(k, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(k, sid) as ks:
                        for j in range(64):
                            try:
                                sub = winreg.EnumKey(ks, j)
                            except OSError:
                                break
                            read(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{sid}\\{sub}", ("landscapeImage",))
                except OSError:
                    continue
    except OSError:
        pass
    read(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Lock Screen\Creative",
         ("LandscapeAssetPath",))
    for c in cands:
        try:
            if Path(c).is_file():
                return c
        except OSError:
            pass
    assets = (Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" /
              "Microsoft.Windows.ContentDeliveryManager_cw5n1h2txyewy" / "LocalState" / "Assets")
    try:
        if assets.is_dir():
            from PIL import Image
            files = sorted((f for f in assets.iterdir() if f.is_file() and f.stat().st_size > 150_000),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[:25]:
                try:
                    with Image.open(f) as im:
                        w, h = im.size
                    if w >= 1600 and w > h:
                        return str(f)
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        pass
    return None

_PS_LOCK = r"""param([string]$Path)
$ErrorActionPreference = 'Stop'
try {
    [Windows.System.UserProfile.LockScreen,Windows.System.UserProfile,ContentType=WindowsRuntime] | Out-Null
    [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Await($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }
    function AwaitAction($WinRtAction) {
        $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and !$_.IsGenericMethod })[0]
        $netTask = $asTask.Invoke($null, @($WinRtAction))
        $netTask.Wait(-1) | Out-Null
    }
    $image = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    AwaitAction ([Windows.System.UserProfile.LockScreen]::SetImageFileAsync($image))
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""


def set_spotlight(enabled):
    """Turn Windows Spotlight on the lock screen on / off (current user, no admin needed)."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager") as k:
        winreg.SetValueEx(k, "RotatingLockScreenEnabled", 0, winreg.REG_DWORD, 1 if enabled else 0)
        if enabled:
            winreg.SetValueEx(k, "RotatingLockScreenOverlayEnabled", 0, winreg.REG_DWORD, 1)


def restore_windows_default():
    """Give the lock screen back to Windows (Windows Spotlight)."""
    if platform.system() != "Windows":
        raise RuntimeError("Lock screen is only supported on Windows")
    set_spotlight(True)


def lock_recent_entries():
    """Images saved for the lock screen's Auto mode (newest first), as gallery entries.
    Page address / title come from the lock history when it knows the file."""
    if not LOCK_DL.exists():
        return []
    known = {}
    for m in (_load(_hist_path("auto"), {"items": []}) or {}).get("items", []):
        if m.get("file"):
            known[Path(m["file"]).name] = m
    files = []
    for f in LOCK_DL.iterdir():
        try:
            if f.is_file() and f.suffix.lower() in IMG_EXT and f.stat().st_size > 0:
                files.append((f.stat().st_mtime, f))
        except OSError:
            pass
    out = []
    for mt, f in sorted(files, key=lambda t: t[0], reverse=True):
        m = known.get(f.name, {})
        slug, wid = parse_wall_filename(f.stem)
        e = {"id": m.get("id") or wid or "lock-" + re.sub(r"[^A-Za-z0-9]+", "-", f.stem).strip("-")[:60],
             "file": str(f), "page_url": m.get("page_url") or "", "img_url": m.get("img_url") or "",
             "title": m.get("title") or (slug or f.stem).replace("-", " ").replace("_", " ").strip().title()
                      or "Wallpaper",
             "ts": int(mt)}
        if m.get("slug") or slug:
            e["slug"] = m.get("slug") or slug
        out.append(e)
    return out


def _migrate_lock_dl():
    """One time: move the lock screen images from ~/.4kwall/lockscreen/dl next to the liked / recent
    folders and fix the paths stored in the lock history."""
    try:
        if not _OLD_LOCK_DL.is_dir():
            return
        LOCK_DL.mkdir(parents=True, exist_ok=True)
        for f in list(_OLD_LOCK_DL.iterdir()):
            if f.is_file() and not (LOCK_DL / f.name).exists():
                shutil.move(str(f), str(LOCK_DL / f.name))
        paths = [LOCK_CURRENT_PATH] + list(LOCK_HIST_PATH.parent.glob("lock_history*.json"))
        for jp in paths:
            d = _load(jp, None)
            if not d:
                continue
            metas = d.get("items", []) if "items" in d else [d]
            for m in metas:
                if m.get("file") and Path(m["file"]).parent == _OLD_LOCK_DL:
                    m["file"] = str(LOCK_DL / Path(m["file"]).name)
            _save(jp, d)
        try:
            _OLD_LOCK_DL.rmdir()
        except OSError:
            pass
    except Exception:
        pass


_migrate_lock_dl()


def trim_lock_dl(keep):
    """Keep only the newest `keep` images downloaded for the lock screen."""
    if not LOCK_DL.exists():
        return
    files = sorted((f for f in LOCK_DL.iterdir() if f.is_file()),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def set_lock_screen(path):
    """Set the Windows lock screen image (also shown on the sign-in screen). No admin needed."""
    if platform.system() != "Windows":
        raise RuntimeError("Lock screen is only supported on Windows")
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(path)
    dst = LOCK_DIR / f"lock-{int(time.time() * 1000)}{src.suffix.lower()}"
    if src.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"):
        shutil.copyfile(src, dst)
    else:                                                   # e.g. webp -> jpg
        from PIL import Image
        dst = dst.with_suffix(".jpg")
        Image.open(src).convert("RGB").save(dst, "JPEG", quality=95)
    try:
        set_spotlight(False)                                # a picture needs Spotlight off
    except OSError:
        pass
    ps1 = LOCK_DIR / "set_lock.ps1"
    ps1.write_text(_PS_LOCK, encoding="utf-8-sig")
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Sysnative" / "powershell.exe"
    if not exe.exists():                                    # only exists for 32-bit processes
        exe = "powershell.exe"
    r = subprocess.run([str(exe), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(ps1), "-Path", str(dst)],
                       capture_output=True, text=True, timeout=90, creationflags=0x08000000)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "PowerShell failed").strip().splitlines()
        raise RuntimeError(msg[-1][:200] if msg else "PowerShell failed")
    for old in sorted(LOCK_DIR.glob("lock-*"), key=lambda f: f.stat().st_mtime, reverse=True)[3:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def lock_pick(cfg, log):
    """Choose the lock screen image for the configured mode -> (file, title)."""
    lk = cfg["lock"]
    mode = lk["mode"]
    if mode == "same":
        cur = load_current()
        if not cur or not Path(cur["file"]).exists():
            raise RuntimeError("no current wallpaper yet")
        return cur["file"], cur.get("title", ""), _lock_meta(cur, cur["file"])
    if mode == "fixed":
        f = lk.get("fixed")
        if not f or not Path(f).exists():
            raise RuntimeError("choose a fixed image first")
        title = lk.get("fixed_name") or Path(f).stem
        return f, title, {"file": f, "title": title}
    if mode == "liked":
        import_liked_folder()
        likes = [l for l in load_likes() if l.get("lock_sel", True)]
        if not likes:
            raise RuntimeError("no liked wallpapers are ticked for the lock screen")
        pool = [l for l in likes if l["id"] != _lock_last["id"]] or likes
        e = ensure_local(random.choice(pool), cfg)
        _lock_last["id"] = e["id"]
        return e["file"], e["title"], _lock_meta(e, e["file"])
    if mode == "auto":
        LOCK_DL.mkdir(parents=True, exist_ok=True)
        lcfg = {**cfg, "source": lk.get("source", "Selected categories"),
                "categories": lk.get("categories") or [],
                "max_pages": lk["max_pages"], "quality": lk["quality"]}
        try:
            if not is_online():
                raise RuntimeError("offline")
            sw, sh = screen_size()
            page_url, img_url, title = fetch_random_wallpaper(lcfg, sw, sh, log)
            path = download(img_url, page_url, cfg["request_delay"], LOCK_DL)
            os.utime(path)                                  # newest, so trimming keeps it
            trim_lock_dl(lk["keep"])
            return str(path), title, {"file": str(path), "title": title, "id": wallpaper_id(page_url),
                                      "page_url": page_url, "img_url": img_url}
        except (requests.RequestException, RuntimeError) as e:
            saved = [f for f in LOCK_DL.iterdir() if f.is_file() and f.suffix.lower() in IMG_EXT]
            if not saved:
                raise RuntimeError(f"{e} (and no saved lock screen images yet)")
            pool = [f for f in saved if f.name != _lock_last["id"]] or saved
            f = random.choice(pool)
            _lock_last["id"] = f.name
            slug, wid = parse_wall_filename(f.stem)
            meta = {"file": str(f), "title": (slug or f.stem).replace("-", " ").title()}
            if wid:
                meta.update(id=wid, slug=slug)
            return str(f), meta["title"], meta
    raise RuntimeError("lock screen is not managed by this app")


def apply_lock_entry(meta, mode):
    """Put a chosen image on the lock screen and remember it (for the Lock tab preview)."""
    dst = set_lock_screen(meta["file"])
    cur = {**meta, "preview": str(dst), "mode": mode}
    _save(LOCK_CURRENT_PATH, cur)
    return cur


def apply_lock_screen(cfg, log=print):
    mode = cfg["lock"]["mode"]
    if mode == "windows":
        restore_windows_default()
        return ("Windows default (Spotlight) switched on. If the picture does not change, choose "
                "\"Windows spotlight\" in Windows Settings > Personalization > Lock screen.")
    _, title, meta = lock_pick(cfg, log)
    apply_lock_entry(meta, mode)
    if mode in ("auto", "liked"):
        lock_hist_add(meta, mode)
    return f"Lock screen: {title or Path(meta['file']).name}"


# --------------------------------------------------------------------------- #
# your own files in the liked folder
# --------------------------------------------------------------------------- #
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_FILE_RE = re.compile(r"^(?P<slug>.+?)-\d{3,5}x\d{3,5}-(?P<id>\d+)$")      # brabus-bodo-1920x1080-27175
_FILE_RE2 = re.compile(r"^(?P<slug>.+?)-(?P<id>\d{4,7})$")                  # brabus-bodo-27175


def parse_wall_filename(stem):
    """'brabus-bodo-1920x1080-27175' -> ('brabus-bodo', '27175'); (None, None) if unknown."""
    m = _FILE_RE.match(stem) or _FILE_RE2.match(stem)
    return (m.group("slug"), m.group("id")) if m else (None, None)


def import_liked_folder():
    """Add images you dropped into the liked folder to the liked list. Returns how many were added."""
    LIKED_DIR.mkdir(parents=True, exist_ok=True)
    added = 0
    with DATA_LOCK:
        likes = load_likes()
        by_file = {Path(l["file"]).name.lower() for l in likes}
        by_id = {l["id"]: l for l in likes}
        changed = False
        files = [f for f in LIKED_DIR.iterdir()
                 if f.is_file() and f.suffix.lower() in IMG_EXT and f.stat().st_size > 0]
        for f in sorted(files, key=lambda x: x.stat().st_mtime):
            if f.name.lower() in by_file:
                continue
            slug, wid = parse_wall_filename(f.stem)
            if wid and wid in by_id:                         # same wallpaper, other resolution
                old = by_id[wid]
                if not Path(old["file"]).exists():           # repair a missing file
                    old["file"], changed = str(f), True
                continue
            wid = wid or "local-" + re.sub(r"[^A-Za-z0-9]+", "-", f.stem).strip("-")[:60]
            if wid in by_id:
                continue
            title = (slug or f.stem).replace("-", " ").replace("_", " ").strip().title()
            entry = {"id": wid, "page_url": "", "title": title or "Wallpaper", "file": str(f),
                     "img_url": (f"{BASE}/images/wallpapers/{f.name}"
                         if slug and f.suffix.lower() in (".jpg", ".jpeg") else ""),
                     "ts": int(f.stat().st_mtime), "selected": True,
                     "liked_ts": int(time.time()), "imported": True}
            if slug:
                entry["slug"] = slug
            likes.append(entry)
            by_id[wid] = entry
            by_file.add(f.name.lower())
            added += 1
            changed = True
        if changed:
            _save(LIKES_PATH, likes)
    return added


def resolve_page_url(slug, wid, delay=1.0):
    """Find the wallpaper page. The filename has no category, so try /<category>/<slug>-<id>.html
    for each category. Returns (url, title) or None. Network errors are raised."""
    name = f"{slug}-{wid}.html"
    for cat in CATEGORIES.values():
        url = f"{BASE}/{cat}/{name}"
        try:
            r = http_get(url, delay)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                continue
            raise
        soup = BeautifulSoup(r.text, "html.parser")
        link = soup.find("link", rel="canonical")
        final = (link.get("href") if link and link.get("href") else r.url)
        if wallpaper_id(final) != wid:                       # soft-404 / wrong page
            continue
        return final, parse_title(r.text, final)
    return None


def resolve_pending_links(delay=1.0):
    """Fill in page_url (and the real title) for imported wallpapers. Returns number resolved."""
    done = 0
    if not is_online():
        return 0
    for l in load_likes():
        if l.get("page_url") or not l.get("slug") or l.get("link_failed"):
            continue
        try:
            res = resolve_page_url(l["slug"], l["id"], delay)
        except requests.RequestException:
            break                                            # offline / site down: retry next time
        with DATA_LOCK:
            likes = load_likes()
            for x in likes:
                if x["id"] == l["id"]:
                    if res:
                        x["page_url"], x["title"] = res[0], res[1] or x["title"]
                    else:
                        x["link_failed"] = True
            _save(LIKES_PATH, likes)
        done += 1
    return done


def set_wallpaper(path):
    system = platform.system()
    path = str(Path(path).resolve())
    if system == "Windows":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "WallpaperStyle", 0, winreg.REG_SZ, "10")  # Fill
            winreg.SetValueEx(k, "TileWallpaper", 0, winreg.REG_SZ, "0")
        ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 3)
    elif system == "Darwin":
        script = ('tell application "System Events" to tell every desktop '
                  f'to set picture to POSIX file "{path}"')
        subprocess.run(["osascript", "-e", script], check=True)
    else:
        uri = "file://" + path
        try:
            for key in ("picture-uri", "picture-uri-dark"):
                subprocess.run(["gsettings", "set", "org.gnome.desktop.background", key, uri],
                               check=True)
            subprocess.run(["gsettings", "set", "org.gnome.desktop.background",
                            "picture-options", "zoom"], check=False)
        except (FileNotFoundError, subprocess.CalledProcessError):
            subprocess.run(["feh", "--bg-fill", path], check=True)


def screen_size():
    global _screen_cache
    if _screen_cache:
        return _screen_cache
    if platform.system() == "Windows":
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            u = ctypes.windll.user32
            _screen_cache = (u.GetSystemMetrics(0), u.GetSystemMetrics(1))
            return _screen_cache
        except Exception:
            pass
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        _screen_cache = (r.winfo_screenwidth(), r.winfo_screenheight())
        r.destroy()
        return _screen_cache
    except Exception:
        return 1920, 1080


# --------------------------------------------------------------------------- #
# the three ways a wallpaper gets picked
# --------------------------------------------------------------------------- #
def _played():
    """History of shown wallpapers: {"items": [...oldest..newest], "pos": index of current}."""
    d = _load(PLAYED_PATH, {"items": [], "pos": -1})
    if isinstance(d, list):                       # migrate older format
        d = {"items": d, "pos": len(d) - 1}
    n = len(d["items"])
    d["pos"] = min(max(d.get("pos", n - 1), -1), n - 1)
    return d


def _resolve(entry):
    """Entry with an existing file (falls back to the liked copy), or None."""
    if Path(entry["file"]).exists():
        return entry
    for l in load_likes():
        if l["id"] == entry["id"] and Path(l["file"]).exists():
            return dict(entry, file=l["file"])
    return None


def apply_entry(entry, record=True):
    set_wallpaper(entry["file"])
    _save(CURRENT_PATH, entry)
    if record:                                    # a newly shown wallpaper goes to the end
        with DATA_LOCK:
            d = _played()
            items = d["items"]
            if not items or items[-1]["id"] != entry["id"]:
                items.append(entry)
            d["items"] = items[-60:]
            d["pos"] = len(d["items"]) - 1
            _save(PLAYED_PATH, d)


def _move(direction=0, to_end=False):
    with DATA_LOCK:
        d = _played()
        items, pos = d["items"], d["pos"]
        if to_end:
            cand = range(len(items) - 1, pos, -1)
        elif direction > 0:
            cand = range(pos + 1, len(items))
        else:
            cand = range(pos - 1, -1, -1)
        for i in cand:
            e = _resolve(items[i])
            if e:
                d["pos"] = i
                _save(PLAYED_PATH, d)
                apply_entry(e, record=False)
                return e
    return None


def has_forward():
    d = _played()
    return any(_resolve(e) for e in d["items"][d["pos"] + 1:])


def go_previous():
    e = _move(-1)
    if not e:
        raise RuntimeError("No previous wallpaper yet")
    return e


def go_forward():
    e = _move(1)
    if not e:
        raise RuntimeError("Already at the newest wallpaper")
    return e


def go_latest():
    e = _move(to_end=True)
    if not e:
        raise RuntimeError("Already at the newest wallpaper")
    return e


def offline_fallback(cfg, log, reason):
    rec = [e for e in load_recent() if Path(e["file"]).exists()]
    if not rec:
        raise RuntimeError(f"{reason} and no saved wallpapers yet")
    if cfg["source"] not in FEEDS and cfg.get("categories"):
        same = [e for e in rec if url_category(e.get("page_url")) in cfg["categories"]]
        rec = same or rec                      # nothing saved from those categories: use any
    cur = load_current()
    ids = [e["id"] for e in rec]
    i = (ids.index(cur["id"]) + 1) % len(rec) if cur and cur["id"] in ids else 0
    entry = rec[i]
    apply_entry(entry)
    log(f"{reason} - showing a saved wallpaper ({i + 1}/{len(rec)})")
    return entry


def change_auto(cfg, log):
    if not is_online():
        return offline_fallback(cfg, log, "Offline")
    try:
        sw, sh = screen_size()
        page_url, img_url, title = fetch_random_wallpaper(cfg, sw, sh, log)
        log("Downloading ...")
        path = download(img_url, page_url, cfg["request_delay"], RECENT_DIR)
    except Exception as e:  # noqa: BLE001
        return offline_fallback(cfg, log, f"Site error ({type(e).__name__}: {str(e)[:50]})")
    entry = make_entry(page_url, img_url, title, path)
    make_thumb(path, entry["id"])
    add_recent(entry, cfg["keep_recent"])
    h = load_history()
    h.append(entry["id"])
    _save(HISTORY_PATH, h[-500:])
    apply_entry(entry)
    log(f"Set: {title}")
    return entry


def change_liked(cfg, log):
    import_liked_folder()
    likes = [l for l in load_likes() if l.get("selected")]
    if not likes:
        raise RuntimeError("No liked wallpapers ticked for the loop")
    ids = [l["id"] for l in likes]
    start = (ids.index(cfg["liked_last"]) + 1) % len(ids) if cfg.get("liked_last") in ids else 0
    last_err = None
    for k in range(len(likes)):
        entry = likes[(start + k) % len(likes)]
        try:
            entry = ensure_local(entry, cfg)
        except Exception as e:  # noqa: BLE001  (offline + file missing -> skip)
            last_err = e
            continue
        apply_entry(entry)
        cfg["liked_last"] = entry["id"]
        log(f"Liked loop: {entry['title']}")
        return entry
    raise RuntimeError(f"Could not load any liked wallpaper ({last_err})")


def change_cycle(cfg, log=print):
    return change_liked(cfg, log) if cfg["mode"] == "liked" else change_auto(cfg, log)


# --------------------------------------------------------------------------- #
# Windows autostart
# --------------------------------------------------------------------------- #
def set_autostart(enabled):
    if platform.system() != "Windows":
        return
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            exe = sys.executable
            if getattr(sys, "frozen", False):                # packaged .exe
                cmd = f'"{exe}" --hidden'
            else:
                if exe.lower().endswith("python.exe"):
                    exe = exe[:-10] + "pythonw.exe"
                cmd = f'"{exe}" "{Path(__file__).resolve()}" --hidden'
            winreg.SetValueEx(k, "4KWallpaperChanger", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "4KWallpaperChanger")
            except FileNotFoundError:
                pass


def open_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        os.startfile(str(path))  # noqa: S606
    elif platform.system() == "Darwin":
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])


def short(t, n):
    t = (t or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def make_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 8, 60, 48), 8, fill=(23, 28, 40, 255), outline=(124, 252, 0, 255), width=3)
    d.polygon([(8, 44), (24, 26), (34, 38), (42, 30), (56, 44)], fill=(124, 252, 0, 255))
    d.ellipse((42, 14, 51, 23), fill=(255, 255, 255, 255))
    d.rectangle((26, 48, 38, 56), fill=(124, 252, 0, 255))
    d.rectangle((18, 56, 46, 60), fill=(124, 252, 0, 255))
    return img


# --------------------------------------------------------------------------- #
# Tray app + modern dark UI
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# smooth animation helpers
# --------------------------------------------------------------------------- #
_hires_n = 0
_hires_lock = threading.Lock()


def hires_timer(on):
    """Windows ticks every ~15.6 ms, so Tk's after(16) really fires every ~31 ms (~32 fps).
    Asking for a 1 ms timer while an animation runs gives real 60+ fps. Calls are counted."""
    global _hires_n
    if platform.system() != "Windows":
        return
    with _hires_lock:
        try:
            if on:
                _hires_n += 1
                if _hires_n == 1:
                    ctypes.windll.winmm.timeBeginPeriod(1)
            elif _hires_n > 0:
                _hires_n -= 1
                if _hires_n == 0:
                    ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:  # noqa: BLE001
            pass


_CORNERS = {}


def _corner_mask(r):
    """Top-left quarter of an anti-aliased circle (cached)."""
    c = _CORNERS.get(r)
    if c is None:
        from PIL import Image, ImageDraw
        S = 4
        big = Image.new("L", (2 * r * S, 2 * r * S), 0)
        ImageDraw.Draw(big).ellipse((0, 0, 2 * r * S - 1, 2 * r * S - 1), fill=255)
        c = _CORNERS[r] = big.crop((0, 0, r * S, r * S)).resize((r, r), Image.LANCZOS)
    return c


def rounded_mask(w, h, r):
    """Rounded-rectangle mask built from cached corners (far cheaper than drawing it 2x per frame)."""
    from PIL import Image
    r = max(1, min(int(r), w // 2, h // 2))
    mk = Image.new("L", (w, h), 255)
    c = _corner_mask(r)
    mk.paste(c, (0, 0))
    mk.paste(c.transpose(Image.FLIP_LEFT_RIGHT), (w - r, 0))
    mk.paste(c.transpose(Image.FLIP_TOP_BOTTOM), (0, h - r))
    mk.paste(c.transpose(Image.ROTATE_180), (w - r, h - r))
    return mk


class ZoomJob:
    """Everything the big preview needs, prepared in background threads so the window never
    freezes: blurred backdrop, the picture, and every frame of the opening animation (rendered
    ahead of time while the user is still holding the preview)."""
    N = 24                                    # frames of the opening animation

    def __init__(self, path, sw, sh, margin, rect):
        self.path, self.sw, self.sh, self.m, self.rect = path, sw, sh, margin, rect
        self.ready = threading.Event()        # backdrop + picture are loaded
        self.cancel = threading.Event()
        self.error = None
        self.bg = self.base = self.geo = None
        self.frames = [None] * self.N
        self._todo = queue.Queue()
        threading.Thread(target=self._prepare, daemon=True).start()

    @classmethod
    def u_of(cls, j):
        t = j / (cls.N - 1)
        return 1 - (1 - t) ** 3               # ease-out: fast start, soft landing

    def _prepare(self):
        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageGrab
            sw, sh, m = self.sw, self.sh, self.m
            try:                              # blurred + dimmed screenshot
                bg = ImageGrab.grab().convert("RGB")
                if bg.size != (sw, sh):
                    bg = bg.resize((sw, sh))
                small = bg.resize((max(1, sw // 4), max(1, sh // 4)), Image.BILINEAR)
                small = small.filter(ImageFilter.GaussianBlur(6))
                bg = ImageEnhance.Brightness(small.resize((sw, sh), Image.BILINEAR)).enhance(0.5)
            except Exception:  # noqa: BLE001
                bg = Image.new("RGB", (sw, sh), (11, 14, 21))
            with Image.open(self.path) as im:
                im.draft("RGB", (sw, sh))
                rgb = im.convert("RGB")
            k = min((sw - 2 * m) / rgb.width, (sh - 2 * m) / rgb.height)
            w, h = max(1, int(rgb.width * k)), max(1, int(rgb.height * k))
            base = rgb.resize((w, h), Image.LANCZOS, reducing_gap=2.0)
            x, y = (sw - w) // 2, (sh - h) // 2
            if not self.rect or self.rect[2] < 20 or self.rect[3] < 20:
                self.rect = (x + w * 0.05, y + h * 0.05, w * 0.9, h * 0.9)
            self.bg, self.base, self.geo = bg, base, (x, y, w, h, max(10, m // 2))
            for j in range(self.N):
                self._todo.put(j)
        except Exception as ex:  # noqa: BLE001
            self.error = ex
            self.ready.set()
            return
        self.ready.set()
        for _ in range(max(1, min(3, (os.cpu_count() or 2) - 1)) - 1):   # helper threads
            threading.Thread(target=self._worker, daemon=True).start()
        self._worker()

    def _worker(self):
        while not self.cancel.is_set():
            try:
                j = self._todo.get_nowait()
            except queue.Empty:
                return
            try:
                self.render(j)
            except Exception:  # noqa: BLE001
                return

    def render(self, j):
        """Frame j -> (RGB image, x, y). Cached; computed on the spot if the workers are behind."""
        f = self.frames[j]
        if f is not None:
            return f
        from PIL import Image, ImageDraw
        x, y, w, h, r_end = self.geo
        rx, ry, rw, rh = self.rect
        u = self.u_of(j)
        cx, cy = int(rx + (x - rx) * u), int(ry + (y - ry) * u)
        cw, ch = max(8, int(rw + (w - rw) * u)), max(8, int(rh + (h - rh) * u))
        ru = max(4, int(12 + (r_end - 12) * u))
        piece = self.base if (cw, ch) == self.base.size else self.base.resize(
            (cw, ch), Image.BILINEAR, reducing_gap=2.0)
        region = self.bg.crop((cx, cy, cx + cw, cy + ch))
        region.paste(piece, (0, 0), rounded_mask(cw, ch, ru))
        ImageDraw.Draw(region).rounded_rectangle((0, 0, cw - 1, ch - 1), ru,
                                                 outline=(52, 64, 90), width=2)
        f = self.frames[j] = (region, cx, cy)
        return f


class HoldButton:
    """Lime 'Next' button: tap = on_click, hold = fill animation, then on_hold."""
    HOLD_DELAY = 0.15      # a quick tap never shows the animation
    HOLD_TIME = 0.75       # seconds to fill

    def __init__(self, master, text, on_click, on_hold, scale, bg, width=96, height=34):
        import tkinter as tk
        import tkinter.font as tkfont
        self.on_click, self.on_hold = on_click, on_hold
        self.text, self.hold_text = text, "Latest  ⏭"
        self.w, self.h = int(width * scale), int(height * scale)
        self.r = int(10 * scale)
        self.c = tk.Canvas(master, width=self.w, height=self.h, bg=bg,
                           highlightthickness=0, bd=0, cursor="hand2")
        self.font = tkfont.Font(family="Segoe UI", size=-int(14 * scale))
        self.state, self.t0, self.hover, self.job = "idle", 0.0, False, None
        self._hires = False
        self.c.bind("<ButtonPress-1>", self._press)
        self.c.bind("<ButtonRelease-1>", self._release)
        self.c.bind("<Enter>", lambda e: self._set_hover(True))
        self.c.bind("<Leave>", self._leave)
        self._draw()

    def pack(self, **kw):
        self.c.pack(**kw)
        return self

    def _round(self, x2, color):
        r = max(1, min(self.r, x2 / 2, self.h / 2))
        pts = [r, 0, x2 - r, 0, x2, 0, x2, r, x2, self.h - r, x2, self.h, x2 - r, self.h,
               r, self.h, 0, self.h, 0, self.h - r, 0, r, 0, 0]
        self.c.create_polygon(pts, smooth=True, fill=color, outline="")

    def _draw(self, p=0.0, flash=False):
        self.c.delete("all")
        if flash:
            base, txt, label = ACCENT_HOVER, ACCENT_TEXT, self.hold_text
        elif p > 0:
            base, txt, label = "#1c3309", TEXT, self.hold_text
        else:
            base = ACCENT_HOVER if self.hover else ACCENT
            txt, label = ACCENT_TEXT, self.text
        self._round(self.w, base)
        if p > 0 and not flash:
            self._round(max(p * self.w, 2 * self.r), ACCENT_DEEP)
        self.c.create_text(self.w / 2, self.h / 2, text=label, fill=txt, font=self.font)

    def _set_hover(self, v):
        self.hover = v
        if self.state == "idle":
            self._draw()

    def _end_hires(self):
        if self._hires:
            self._hires = False
            hires_timer(False)

    def _press(self, _e):
        self.state, self.t0 = "pressed", time.time()
        if not self._hires:
            self._hires = True
            hires_timer(True)
        self._tick()

    def _tick(self):
        if self.state != "pressed":
            return
        el = time.time() - self.t0
        if el >= self.HOLD_DELAY:
            p = min(1.0, (el - self.HOLD_DELAY) / self.HOLD_TIME)
            self._draw(p)
            if p >= 1.0:
                self.state = "done"
                self._end_hires()
                self._draw(flash=True)
                self.c.after(220, lambda: self.state == "idle" and self._draw())
                self.on_hold()
                return
        self.job = self.c.after(8, self._tick)

    def _release(self, _e):
        st, self.state = self.state, "idle"
        self._end_hires()
        self._draw()
        if st == "pressed":
            self.on_click()

    def _leave(self, _e):
        self.hover = False
        if self.state == "pressed":
            self.state = "idle"            # dragged off the button: cancel
            self._end_hires()
        if self.state == "idle":
            self._draw()


class TrayApp:
    PIN_OFF = "🔒  Lock"
    PIN_ON = "🔒  Locked"
    ZOOM_DELAY = 0.12      # a quick tap shows nothing
    ZOOM_HOLD = 0.65       # then this long to fill the ring and open the big view
    PAGES = ["Home", "Liked", "Recent"] + (["Lock"] if platform.system() == "Windows" else []) + ["Settings"]

    def __init__(self, start_hidden=False):
        import customtkinter as ctk
        import pystray
        from PIL import Image, ImageDraw, ImageTk
        self.ctk, self.pystray = ctk, pystray
        self.Image, self.ImageDraw, self.ImageTk = Image, ImageDraw, ImageTk

        self.cfg = load_config()
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.force = None
        self.nav_lock = threading.Lock()      # serialises Prev / Next / Latest / timer
        self.lock_wake = threading.Event()    # lock screen: apply now / settings changed
        self.lock_nav = threading.Lock()      # serialises lock screen changes (timer / Prev / Next)
        self.lock_img = self.lock_like_btn = self.lock_pin_btn = None
        self.paused = not self.cfg.get("auto", True)
        self.img_cache = {}
        self.current_page = "Home"
        self._zoom = None                     # the big preview window, when open
        self._zoom_job = None                 # its background loader (set while loading / open)
        self._redl = set()                    # liked wallpapers being re-downloaded right now
        self._dl_prog = {}                    # wid -> 0..1 progress of that download
        self._dl_btns = {}                    # wid -> its download button (Liked page)
        self._dl_prog_imgs = {}               # cache: filled-arrow icons by step
        self._dl_menu = None                  # the open quality menu, if any
        self._dl_state = {}                   # wid -> {"exists", "diff", "label"} for the Liked cards
        self._cards = {}                      # (kind, wid) -> card frame
        self._card_idx = {}                   # (kind, wid) -> grid index of the card
        self._f_tag = None
        self._thumb_pool = ThreadPoolExecutor(max_workers=2)   # previews are made off the UI thread
        self._thumb_wait = {}                 # (id, size) -> labels waiting for that preview
        self._lazy_tok = {}                   # kind -> token of the running "build more on scroll" loop
        self._liked_cat = None                # Liked page filter: category slug, NO_CAT or None (= all)
        self._liked_sort = "new"              # "new" | "old"
        self._liked_cat_map = {}              # menu text -> category slug
        self._lock_cat = None                 # same filter / sort for the Lock tab's liked list
        self._lock_sort = "new"
        self._lock_cat_map = {}
        self._dl_menu_bound = False
        self._eye_img = None

        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk(fg_color=BG)
        self.root.title("4K Wallpaper Changer")
        self.root.resizable(False, False)
        global _screen_cache
        _screen_cache = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())

        F = ctk.CTkFont
        self.f_title = F(family="Segoe UI", size=15, weight="bold")
        self.f_body = F(family="Segoe UI", size=13)
        self.f_small = F(family="Segoe UI", size=12)
        self.f_head = F(family="Segoe UI", size=18, weight="bold")

        self._build_header()
        self._build_tabs()
        self.container = ctk.CTkFrame(self.root, fg_color="transparent")
        self.container.pack(fill="both", expand=True)
        self.pages = {}
        self.pages["Home"] = self._build_home(self.container)
        self.pages["Liked"] = self._build_gallery_page(self.container, "liked")
        self.pages["Recent"] = self._build_gallery_page(self.container, "recent")
        if "Lock" in self.PAGES:
            self.pages["Lock"] = self._build_lock(self.container)
        self.pages["Settings"] = self._build_settings(self.container)

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda e: self._zoom_close() if self._zoom else self.hide())
        self.root.after(300, self._set_window_icon)

        import_liked_folder()
        self._resolving = False
        self.show_page("Home")
        self.refresh_home()
        self._build_tray()
        self.start_link_resolver()
        threading.Thread(target=self._loop, daemon=True).start()
        if platform.system() == "Windows":
            threading.Thread(target=self._lock_loop, daemon=True).start()
        self.root.after(200, self._poll)
        if start_hidden:
            self.root.withdraw()
        else:
            self.show()
        self.root.mainloop()

    # ------------------------------------------------------------------ UI -- #
    def _set_window_icon(self):
        try:
            self._icon_photo = self.ImageTk.PhotoImage(make_icon_image())
            self.root.iconphoto(False, self._icon_photo)
        except Exception:
            pass

    def _btn(self, parent, text, command, accent=False, width=None, height=34, **kw):
        ctk = self.ctk
        if accent:
            kw.update(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT)
        else:
            kw.setdefault("fg_color", SURFACE2)
            kw.setdefault("hover_color", BORDER)
            kw.setdefault("text_color", TEXT)
        if width:
            kw["width"] = width
        return ctk.CTkButton(parent, text=text, command=command, height=height,
                             corner_radius=10, font=self.f_body, **kw)

    def _build_header(self):
        ctk = self.ctk
        h = ctk.CTkFrame(self.root, fg_color=HEADER, corner_radius=0, height=60)
        h.pack(fill="x")
        h.pack_propagate(False)
        logo = ctk.CTkFrame(h, fg_color="transparent")
        logo.pack(side="left", padx=18)
        ctk.CTkLabel(logo, text="4K", font=ctk.CTkFont(family="Segoe UI", size=26, weight="bold"),
                     text_color=ACCENT).pack(side="left")
        ctk.CTkLabel(logo, text=" WALLPAPERS", font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
                     text_color=TEXT).pack(side="left", pady=(3, 0))
        self.state_lbl = ctk.CTkLabel(h, text="", font=self.f_small, text_color=MUTED)
        self.state_lbl.pack(side="right", padx=18)

    def _build_tabs(self):
        ctk = self.ctk
        bar = ctk.CTkFrame(self.root, fg_color="transparent")
        bar.pack(fill="x", pady=(12, 4))
        self.tabs = ctk.CTkSegmentedButton(
            bar, values=self.PAGES, command=self.show_page, height=38, font=self.f_body,
            fg_color=SURFACE, selected_color=SURFACE2, selected_hover_color=SURFACE2,
            unselected_color=SURFACE, unselected_hover_color="#252d3f", text_color=TEXT,
            corner_radius=12)
        self.tabs.pack()
        self.tabs.set("Home")

    # ---- Home ----
    def _build_home(self, parent):
        ctk = self.ctk
        f = ctk.CTkFrame(parent, fg_color="transparent")

        card = ctk.CTkFrame(f, fg_color=SURFACE, corner_radius=18)
        card.pack(fill="x", padx=16, pady=(8, 8))
        self.now_img = ctk.CTkLabel(card, text="No wallpaper yet", text_color=MUTED,
                                    width=468, height=263, fg_color=SURFACE, corner_radius=12)
        self.now_img.pack(padx=14, pady=(14, 8))
        self._zoomable(self.now_img, lambda: (load_current() or {}).get("file"))
        self.now_title = ctk.CTkLabel(card, text="", font=self.f_title, text_color=TEXT,
                                      anchor="w", justify="left", wraplength=460)
        self.now_title.pack(fill="x", padx=18)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(8, 14))
        self._btn(row, "⏮  Prev", self.prev_now, width=80).pack(side="left")
        self.like_btn = self._btn(row, "♡  Like", self.like_current, width=92)
        self.like_btn.pack(side="left", padx=4)
        self.next_btn = HoldButton(row, "Next  ⏭", self.next_now, self.latest_now,
                                   self._scale(), SURFACE, width=88).pack(side="left")
        self._btn(row, "Open page", self.open_current_page, width=88).pack(side="left", padx=4)
        self.pin_btn = self._btn(row, self.PIN_OFF, self.pin_current, width=88)
        self.pin_btn.pack(side="left")

        mc = ctk.CTkFrame(f, fg_color=SURFACE, corner_radius=18)
        mc.pack(fill="x", padx=16, pady=(0, 8))
        self.mode_seg = ctk.CTkSegmentedButton(
            mc, values=["Auto", "Liked loop"], command=self.on_mode, height=36, font=self.f_body,
            fg_color=SURFACE2, selected_color=ACCENT_DEEP, selected_hover_color="#4aa30e",
            unselected_color=SURFACE2, unselected_hover_color=BORDER, text_color=TEXT,
            corner_radius=10)
        self.mode_seg.pack(fill="x", padx=14, pady=(14, 6))
        self.mode_seg.set("Liked loop" if self.cfg["mode"] == "liked" else "Auto")
        self.hint = ctk.CTkLabel(mc, text="", font=self.f_small, text_color=MUTED,
                                 anchor="w", justify="left", wraplength=470)
        self.hint.pack(fill="x", padx=16)

        r2 = ctk.CTkFrame(mc, fg_color="transparent")
        r2.pack(fill="x", padx=14, pady=(8, 14))
        ctk.CTkLabel(r2, text="Change every", font=self.f_body, text_color=TEXT).pack(side="left")
        self.interval_var = ctk.StringVar(value=str(self.cfg["intervals"][self.cfg["mode"]]))
        self.interval_entry = ctk.CTkEntry(r2, textvariable=self.interval_var, width=60, height=32,
                                           fg_color=SURFACE2, border_color=BORDER, text_color=TEXT,
                                           justify="center")
        self.interval_entry.pack(side="left", padx=8)
        self.interval_entry.bind("<Return>", lambda e: self.commit_interval())
        self.interval_entry.bind("<FocusOut>", lambda e: self.commit_interval())
        ctk.CTkLabel(r2, text="min", font=self.f_body, text_color=MUTED).pack(side="left")
        self.run_var = ctk.BooleanVar(value=not self.paused)
        ctk.CTkSwitch(r2, text="Auto change", variable=self.run_var, command=self.on_run_toggle,
                      progress_color=ACCENT, button_color=TEXT, button_hover_color=TEXT,
                      font=self.f_body, text_color=TEXT).pack(side="right")

        self.status = ctk.CTkLabel(f, text="Ready", font=self.f_small, text_color=MUTED,
                                   anchor="w", wraplength=500, justify="left")
        self.status.pack(fill="x", padx=20, pady=(0, 8))
        return f

    # ---- gallery pages (Liked / Recent) ----
    def _build_gallery_page(self, parent, kind):
        ctk = self.ctk
        f = ctk.CTkFrame(parent, fg_color="transparent")
        head = ctk.CTkFrame(f, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(8, 0))
        bar = ctk.CTkFrame(head, fg_color=ACCENT, width=4, height=22, corner_radius=2)
        bar.pack(side="left")
        title = "Liked wallpapers" if kind == "liked" else "Last saved"
        t = ctk.CTkLabel(head, text=title, font=self.f_head, text_color=TEXT)
        t.pack(side="left", padx=8)
        if kind == "recent":
            self._btn(head, "📁", lambda: open_folder(RECENT_DIR), width=40, height=28).pack(side="right")
        if kind == "liked":
            self._btn(head, "None", lambda: self.select_all(False), width=60, height=28).pack(side="right")
            self._btn(head, "📁", lambda: open_folder(LIKED_DIR), width=40, height=28).pack(side="right", padx=(0, 6))
            self._btn(head, "All", lambda: self.select_all(True), width=50, height=28).pack(side="right", padx=6)
        if kind == "liked":                                  # filter + sort
            frow = ctk.CTkFrame(f, fg_color="transparent")
            frow.pack(fill="x", padx=18, pady=(8, 0))
            self.liked_cat_var = ctk.StringVar(value="All categories")
            self.liked_cat_menu = self._opt(frow, ["All categories"], self.liked_cat_var,
                                            self.on_liked_cat, 200, 30)
            self.liked_cat_menu.pack(side="left")
            self.liked_sort_var = ctk.StringVar(value="Newest first")
            self._opt(frow, ["Newest first", "Oldest first"], self.liked_sort_var,
                      self.on_liked_sort, 140, 30).pack(side="left", padx=8)
        info = ctk.CTkLabel(f, text="", font=self.f_small, text_color=MUTED, anchor="w",
                            wraplength=500, justify="left")
        info.pack(fill="x", padx=20, pady=(2, 4))
        holder = ctk.CTkScrollableFrame(f, fg_color="transparent",
                                        scrollbar_button_color=SURFACE2,
                                        scrollbar_button_hover_color=BORDER)
        holder.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 8))
        holder.grid_columnconfigure((0, 1), weight=1)
        setattr(self, f"{kind}_info", info)
        setattr(self, f"{kind}_holder", holder)
        return f

    def thumb_image(self, entry, size, radius=10):
        key = (entry["id"], size)
        if key in self.img_cache:
            return self.img_cache[key]
        p = THUMB_DIR / f"{entry['id']}.jpg"
        if not p.exists():
            p = make_thumb(entry["file"], entry["id"])
        if not p:
            return None
        try:
            im = self.Image.open(p).convert("RGBA")
            r = max(2, int(radius * im.width / size[0]))
            mask = self.Image.new("L", im.size, 0)
            self.ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), r, fill=255)
            im.putalpha(mask)
            ci = self.ctk.CTkImage(light_image=im, dark_image=im, size=size)
        except Exception:
            return None
        if len(self.img_cache) > 250:
            self.img_cache.clear()
        self.img_cache[key] = ci
        return ci

    @staticmethod
    def _alive(w):
        try:
            return bool(w.winfo_exists())
        except Exception:
            return False

    @staticmethod
    def _canvas_of(w):
        """The canvas of the scrollable frame this widget sits in."""
        while w is not None:
            c = getattr(w, "_parent_canvas", None)
            if c is not None:
                return c
            w = getattr(w, "master", None)
        return None

    def _thumb_into(self, lbl, e, size=(212, 119), radius=10):
        """Show the preview of entry e in label lbl: at once when cached, otherwise the label stays
        empty while a background thread makes it (the UI never waits for it)."""
        key = (e["id"], size)
        ci = self.img_cache.get(key)
        if ci is not None:
            lbl.configure(image=ci, text="")
            return
        waiters = self._thumb_wait.setdefault(key, [])
        waiters.append(lbl)
        if len(waiters) == 1:
            try:
                self._thumb_pool.submit(self._thumb_job, dict(e), size, radius, key)
            except RuntimeError:                              # pool already shut down (closing)
                self._thumb_wait.pop(key, None)

    def _thumb_job(self, e, size, radius, key):
        """Background thread: read / make the preview and round its corners. No Tk calls here."""
        im = None
        try:
            p = THUMB_DIR / f"{e['id']}.jpg"
            if not p.exists():
                p = make_thumb(e["file"], e["id"])
            if p:
                im = self.Image.open(p).convert("RGBA")
                r = max(2, int(radius * im.width / size[0]))
                mask = self.Image.new("L", im.size, 0)
                self.ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), r, fill=255)
                im.putalpha(mask)
        except Exception:
            im = None
        self.q.put(("thumb", (key, im)))

    def _thumb_ready(self, key, im):
        """UI thread: a preview is ready - put it in every label that was waiting for it."""
        waiters = self._thumb_wait.pop(key, [])
        ci = None
        if im is not None:
            ci = self.ctk.CTkImage(light_image=im, dark_image=im, size=key[1])
            if len(self.img_cache) > 250:
                self.img_cache.clear()
            self.img_cache[key] = ci
        for lbl in waiters:
            try:
                if lbl.winfo_exists():
                    lbl.configure(image=ci, text="" if ci else "no preview")
            except Exception:
                pass

    def _lazy_cards(self, container, kind, entries, liked_ids, first=10, batch=10):
        """Build the first boxes now and the rest while the user scrolls down. Boxes that already
        exist (filter / sort change) are only moved to their new place - not built again."""
        token = object()
        self._lazy_tok[kind] = token
        pos = [0]

        def build(n):
            made = 0
            while pos[0] < len(entries) and made < n:
                i, e = pos[0], entries[pos[0]]
                key = (kind, e["id"])
                old = self._cards.get(key)
                if old is not None and self._alive(old):
                    self._card_idx[key] = i
                    old.grid(row=i // 2, column=i % 2, padx=5, pady=5, sticky="n")
                else:
                    self._make_card(container, kind, e, i, liked_ids)
                    made += 1
                pos[0] += 1

        build(first)

        def tick():
            if self._lazy_tok.get(kind) is not token or pos[0] >= len(entries):
                return
            try:
                if not container.winfo_exists():
                    return
                if container.winfo_viewable():
                    canvas = self._canvas_of(container)
                    if canvas is None:
                        build(len(entries))
                    else:
                        bottom = container.winfo_rooty() + container.winfo_height()
                        view = canvas.winfo_rooty() + canvas.winfo_height()
                        if bottom - view < 600:               # the user is close to the end
                            build(batch)
            except Exception:
                return
            container.after(150, tick)
        container.after(150, tick)

    def _clear_gallery(self, holder, kind):
        for w in holder.winfo_children():
            w.destroy()
        self._cards = {k: v for k, v in self._cards.items() if k[0] != kind}
        if kind == "liked":
            self._dl_btns = {k: v for k, v in self._dl_btns.items() if k[0] != "liked"}

    def refresh_gallery(self, kind, reuse=False):
        """reuse=True (filter / sort changed): keep the boxes that are already built and only
        re-arrange them; otherwise everything is rebuilt from the saved data."""
        ctk = self.ctk
        holder = getattr(self, f"{kind}_holder")
        info = getattr(self, f"{kind}_info")
        self._close_dl_menu()
        if not reuse:
            self._clear_gallery(holder, kind)
        if kind == "liked":
            import_liked_folder()
            self.start_link_resolver()
            all_likes = load_likes()
            opts = liked_cat_options(all_likes)
            if self._liked_cat not in opts.values():         # that category has no liked images any more
                self._liked_cat = None
            self._liked_cat_map = opts
            self.liked_cat_menu.configure(values=list(opts))
            self.liked_cat_var.set(next(t for t, c in opts.items() if c == self._liked_cat))
            entries = liked_view(all_likes, self._liked_cat, self._liked_sort)
            info.configure(text=self._liked_info_text(all_likes))
            empty = ("Nothing liked yet.\nPress ♡ Like on the Home tab (or the tray menu),\n"
                     "or drop images into the liked folder.")
        else:
            entries = list(reversed(load_recent()))
            info.configure(text=f"Newest {self.cfg['keep_recent']} wallpapers are kept on disk. "
                                "Auto mode loops through them when you're offline.")
            empty = "No saved wallpapers yet."
        if not entries:
            if reuse:
                self._clear_gallery(holder, kind)
            self._lazy_tok[kind] = None
            ctk.CTkLabel(holder, text=empty, font=self.f_body, text_color=MUTED,
                         justify="center").grid(row=0, column=0, columnspan=2, pady=60)
            return
        if reuse:                                            # hide all, then show the ones that match
            for key, c in list(self._cards.items()):
                if key[0] == kind and self._alive(c):
                    c.grid_remove()
        liked_ids = {l["id"] for l in load_likes()}
        self._lazy_cards(holder, kind, entries, liked_ids)

    def _make_card(self, holder, kind, e, i, liked_ids):
        """One wallpaper box. kind: "liked" (Liked tab), "lock" (Lock tab's liked list), "recent"."""
        ctk = self.ctk
        key = (kind, e["id"])
        self._card_idx[key] = i
        card = ctk.CTkFrame(holder, fg_color=SURFACE, corner_radius=16)
        self._cards[key] = card
        card.grid(row=i // 2, column=i % 2, padx=5, pady=5, sticky="n")
        lbl = ctk.CTkLabel(card, text="", image=None, width=212, height=119, text_color=MUTED)
        lbl.pack(padx=10, pady=(10, 6))
        self._thumb_into(lbl, e)                             # preview arrives from a background thread
        self._zoomable(lbl, lambda en=e: self._entry_file(en), size=26)
        ctk.CTkLabel(card, text=short(e["title"], 30), font=self.f_small, text_color=TEXT,
                     anchor="w").pack(fill="x", padx=12)
        is_liked = kind in ("liked", "lock")
        if kind == "lock":
            var = ctk.BooleanVar(value=bool(e.get("lock_sel", True)))
            ctk.CTkCheckBox(card, text="Show on lock screen", variable=var, checkbox_width=20,
                            checkbox_height=20, corner_radius=6, border_width=2, fg_color=ACCENT,
                            hover_color=ACCENT_HOVER, checkmark_color=ACCENT_TEXT, border_color=MUTED,
                            text_color=TEXT, font=self.f_small,
                            command=lambda wid=e["id"], v=var: self.on_lock_select(wid, v.get())
                            ).pack(anchor="w", padx=12, pady=(6, 0))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(6, 10))
        bw = 30 if is_liked else 34                          # liked rows hold one more button
        if kind == "liked":
            var = ctk.BooleanVar(value=bool(e.get("selected", True)))
            ctk.CTkCheckBox(row, text="Loop", variable=var, width=62, checkbox_width=20,
                            checkbox_height=20, corner_radius=6, border_width=2,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER, checkmark_color=ACCENT_TEXT,
                            border_color=MUTED, text_color=TEXT, font=self.f_small,
                            command=lambda wid=e["id"], v=var: self.on_select(wid, v.get())
                            ).pack(side="left")
        if is_liked:
            heart = self._btn(row, "♥", lambda wid=e["id"]: self.unlike(wid), width=bw, height=28,
                              text_color=ACCENT)
            heart.pack(side="right")
        else:
            heart = self._btn(row, "♥" if e["id"] in liked_ids else "♡", None, width=34, height=28,
                              text_color=ACCENT if e["id"] in liked_ids else TEXT)
            heart.configure(command=lambda en=e, b=heart: self.toggle_from_recent(en, b))
            heart.pack(side="left")
        self._btn(row, "↗", lambda en=e: self.open_page(en), width=bw, height=28
                  ).pack(side="right", padx=3 if is_liked else 4)
        set_cmd = self.set_lock_entry if kind in ("lock", "lockrecent") else self.set_entry  # Lock tab -> lock screen
        self._btn(row, "Set", lambda en=e: set_cmd(en), accent=True,
                  width=42 if is_liked else 46, height=28).pack(side="right")
        if is_liked:                                         # between the checkbox and Set
            wid = e["id"]
            info = self._dl_state[wid] = self._dl_info(e)
            dbtn = self._btn(row, "", None, width=bw, height=28, image=self._dl_img("light"))
            dbtn.pack(side="right", padx=(0, 3))
            self._dl_btns[(kind, wid)] = dbtn
            self._dl_hold(dbtn, wid)
            self._apply_dl_state(wid)
            if info["label"]:                                # quality of the file on disk (faint)
                if self._f_tag is None:
                    self._f_tag = ctk.CTkFont(family="Segoe UI", size=10)
                tag = ctk.CTkLabel(card, text=info["label"], font=self._f_tag, text_color=MUTED,
                                   fg_color="#0d1117", corner_radius=6, height=18)
                tag.place(in_=lbl, x=8, rely=1.0, y=-8, anchor="sw")

    # ---- Settings ----
    def _section(self, parent, text):
        ctk = self.ctk
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=6, pady=(14, 4))
        ctk.CTkFrame(r, fg_color=ACCENT, width=4, height=18, corner_radius=2).pack(side="left")
        ctk.CTkLabel(r, text=text, font=self.f_title, text_color=TEXT).pack(side="left", padx=8)

    def _build_settings(self, parent):
        ctk = self.ctk
        f = ctk.CTkScrollableFrame(parent, fg_color="transparent",
                                   scrollbar_button_color=SURFACE2, scrollbar_button_hover_color=BORDER)

        self._section(f, "Auto mode source")
        self.source_var = ctk.StringVar(value=self.cfg["source"])
        self.source_menu = ctk.CTkOptionMenu(
            f, values=["Selected categories"] + list(FEEDS), variable=self.source_var,
            command=self.on_source, fg_color=SURFACE, button_color=SURFACE2,
            button_hover_color=BORDER, dropdown_fg_color=SURFACE,
            dropdown_hover_color=SURFACE2, text_color=TEXT, font=self.f_body,
            dropdown_font=self.f_body, width=260, height=34)
        self.source_menu.pack(anchor="w", padx=8)

        self.cat_box = ctk.CTkFrame(f, fg_color="transparent")     # shown only for "Selected categories"
        self._section(self.cat_box, "Categories")
        grid = ctk.CTkFrame(self.cat_box, fg_color=SURFACE, corner_radius=16)
        grid.pack(fill="x", padx=6)
        self.cat_vars = {}
        for i, (label, slug) in enumerate(CATEGORIES.items()):
            v = ctk.BooleanVar(value=slug in self.cfg["categories"])
            self.cat_vars[slug] = v
            ctk.CTkCheckBox(grid, text=label, variable=v, command=self.on_categories, width=140,
                            checkbox_width=20, checkbox_height=20, corner_radius=6, border_width=2,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER, checkmark_color=ACCENT_TEXT,
                            border_color=MUTED, text_color=TEXT, font=self.f_small
                            ).grid(row=i // 3, column=i % 3, sticky="w", padx=12, pady=6)
        r = ctk.CTkFrame(grid, fg_color="transparent")
        r.grid(row=11, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 10))
        self._btn(r, "All", lambda: self.set_all_cats(True), width=60, height=28).pack(side="left")
        self._btn(r, "None", lambda: self.set_all_cats(False), width=60, height=28).pack(side="left", padx=6)
        self._show_cats(self.cat_box, self.source_menu, self.cfg["source"] == "Selected categories")

        self._section(f, "Search depth (pages)")
        self.depth_var = ctk.StringVar(value=str(self.cfg["max_pages"]))
        e = ctk.CTkEntry(f, textvariable=self.depth_var, width=80, height=34, fg_color=SURFACE,
                         border_color=BORDER, text_color=TEXT, justify="center")
        e.pack(anchor="w", padx=8)
        e.bind("<Return>", lambda ev: self.commit_depth())
        e.bind("<FocusOut>", lambda ev: self.commit_depth())
        ctk.CTkLabel(f, text="Small = newest wallpapers only, large = more variety.",
                     font=self.f_small, text_color=MUTED).pack(anchor="w", padx=10, pady=(2, 0))

        self._section(f, "Download quality")
        self.quality_seg = ctk.CTkSegmentedButton(
            f, values=list(QUALITIES.values()), command=self.on_quality, height=34, font=self.f_body,
            fg_color=SURFACE, selected_color=ACCENT_DEEP, selected_hover_color="#4aa30e",
            unselected_color=SURFACE, unselected_hover_color=SURFACE2, text_color=TEXT)
        self.quality_seg.pack(anchor="w", padx=8)
        self.quality_seg.set(QUALITIES[self.cfg["quality"]])
        ctk.CTkLabel(f, text="Low: smallest size that covers your screen (fast, default). "
                             "4K: the site's 4K download. High: the Original (largest, "
                             "often 5K+ and several MB). Applies to new downloads only.",
                     font=self.f_small, text_color=MUTED, wraplength=470, justify="left"
                     ).pack(anchor="w", padx=10, pady=(2, 0))

        self._section(f, "Keep last wallpapers on disk")
        self.keep_seg = ctk.CTkSegmentedButton(
            f, values=["10", "20", "40"], command=self.on_keep, height=34, font=self.f_body,
            fg_color=SURFACE, selected_color=ACCENT_DEEP, selected_hover_color="#4aa30e",
            unselected_color=SURFACE, unselected_hover_color=SURFACE2, text_color=TEXT)
        self.keep_seg.pack(anchor="w", padx=8)
        self.keep_seg.set(str(self.cfg["keep_recent"]))
        ctk.CTkLabel(f, text="Oldest one is deleted when a new one is added. Liked ones are never deleted.",
                     font=self.f_small, text_color=MUTED, wraplength=470, justify="left"
                     ).pack(anchor="w", padx=10, pady=(2, 0))

        self._section(f, "General")
        if platform.system() == "Windows":
            self.auto_var = ctk.BooleanVar(value=self.cfg.get("autostart", False))
            ctk.CTkSwitch(f, text="Start with Windows", variable=self.auto_var, command=self.on_autostart,
                          progress_color=ACCENT, button_color=TEXT, button_hover_color=TEXT,
                          font=self.f_body, text_color=TEXT).pack(anchor="w", padx=10, pady=4)
        self._btn(f, "Open wallpapers folder", lambda: open_folder(PICS), width=200).pack(
            anchor="w", padx=8, pady=(8, 20))
        return f

    # ------------------------------------------------------------- window -- #
    def show_page(self, name):
        self.current_page = name
        for p in self.pages.values():
            p.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        if name == "Home":
            self.refresh_home()
        elif name == "Liked":
            self.refresh_gallery("liked")
        elif name == "Recent":
            self.refresh_gallery("recent")
        elif name == "Lock":
            self.refresh_lock()

    def show(self):
        ctk = self.ctk
        W = 560
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            s = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:
            s = 1.0
        H = int(min(780, sh / s - 110))
        x = int(sw - (W + 16) * s)
        y = int(sh - (H + 64) * s)
        self.root.geometry(f"{W}x{H}+{max(0, x)}+{max(0, y)}")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.show_page(self.current_page)

    def hide(self):
        self._zoom_close(now=True)
        self.root.withdraw()

    # ------------------------------------------------------------ actions -- #
    def say(self, msg):
        self.status.configure(text=str(msg)[:160])
        try:
            self.icon.title = ("4K Wallpaper: " + str(msg))[:60]
        except Exception:
            pass

    def hint_text(self):
        if self._pinned_now():
            return "Locked: the timer will not change this wallpaper. Press Lock, Next or Prev to release it."
        if self.cfg["mode"] == "auto":
            src = (f"{len(self.cfg['categories'])} categories" if self.cfg["source"] == "Selected categories"
                   else self.cfg["source"])
            return (f"New random wallpaper from {src}. Offline: loops your last "
                    f"{self.cfg['keep_recent']} saved ones.")
        n = sum(1 for l in load_likes() if l.get("selected"))
        return f"Loops {n} ticked liked wallpaper(s) in order. Saved on disk, works offline."

    def _pinned_now(self):
        pid, cur = self.cfg.get("pinned"), load_current()
        return bool(pid and cur and cur.get("id") == pid)

    def _unpin_desktop(self):
        """Any manual change of the wallpaper releases the pin."""
        if self.cfg.get("pinned"):
            self.cfg["pinned"] = None
            save_config(self.cfg)
            self.q.put(("pin", None))

    def _timer_action(self):
        return "advance" if (not self.paused and not self._pinned_now()) else None

    def pin_current(self):
        cur = load_current()
        if not cur:
            self.say("Nothing to lock yet - change the wallpaper first")
            return
        if self._pinned_now():
            self.cfg["pinned"] = None
            self.say("Unlocked - the timer changes the wallpaper again")
        else:
            self.cfg["pinned"] = cur["id"]
            self.say("Locked - the timer will not change this wallpaper")
        save_config(self.cfg)
        self.refresh_home()

    def refresh_state_label(self):
        mode = "Liked loop" if self.cfg["mode"] == "liked" else "Auto"
        if self._pinned_now():
            self.state_lbl.configure(text="● locked", text_color=ACCENT)
        else:
            self.state_lbl.configure(
                text=("● paused" if self.paused else f"● {mode}"),
                text_color=MUTED if self.paused else ACCENT)

    def refresh_home(self):
        cur = load_current()
        if cur:
            img = self.thumb_image(cur, (468, 263), 14)
            if img:
                self.now_img.configure(image=img, text="")
            self.now_title.configure(text=short(cur["title"], 70))
            pinned = self._pinned_now()
            self.pin_btn.configure(text=self.PIN_ON if pinned else self.PIN_OFF,
                                   fg_color=ACCENT if pinned else SURFACE2,
                                   hover_color=ACCENT_HOVER if pinned else BORDER,
                                   text_color=ACCENT_TEXT if pinned else TEXT)
            liked = is_liked(cur["id"])
            self.like_btn.configure(text="♥  Liked" if liked else "♡  Like",
                                    text_color=ACCENT if liked else TEXT)
        self.hint.configure(text=self.hint_text())
        self.refresh_state_label()

    def like_current(self):
        cur = load_current()
        if not cur:
            self.say("Nothing to like yet - change the wallpaper first")
            return
        liked = toggle_like(cur)
        self.say("Added to Liked" if liked else "Removed from Liked")
        self.refresh_home()
        if self.current_page == "Liked":
            self.refresh_gallery("liked")

    def unlike(self, wid):
        for l in load_likes():
            if l["id"] == wid:
                toggle_like(l)
        if self.current_page == "Lock":
            self._fill_lock_cards()
            self._update_lock_liked_info()
        else:
            self.refresh_gallery("liked")
        self.refresh_home()

    def toggle_from_recent(self, entry, btn):
        liked = toggle_like(entry)
        btn.configure(text="♥" if liked else "♡", text_color=ACCENT if liked else TEXT)
        self.refresh_home()

    def _liked_info_text(self, likes=None):
        likes = load_likes() if likes is None else likes
        shown = [l for l in likes if self._liked_cat is None or liked_category(l) == self._liked_cat]
        n = sum(1 for l in shown if l.get("selected"))
        head = f"{len(shown)} of {len(likes)} liked" if self._liked_cat else f"{len(likes)} liked"
        return f"{head} · {n} ticked for the loop · hold the download arrow to pick the quality"

    def on_liked_cat(self, value):
        self._liked_cat = self._liked_cat_map.get(value)
        self.refresh_gallery("liked", reuse=True)
        self._scroll_top(self.liked_holder)

    def on_liked_sort(self, value):
        self._liked_sort = "old" if value.startswith("Oldest") else "new"
        self.refresh_gallery("liked", reuse=True)
        self._scroll_top(self.liked_holder)

    @staticmethod
    def _scroll_top(holder):
        try:
            holder._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def on_select(self, wid, val):
        set_selected(wid, val)
        self.liked_info.configure(text=self._liked_info_text())
        self.hint.configure(text=self.hint_text())

    def select_all(self, val):
        set_all_selected(val)
        self.refresh_gallery("liked")
        self.hint.configure(text=self.hint_text())

    def open_current_page(self):
        cur = load_current()
        if cur:
            self.open_page(cur)

    def open_page(self, entry):
        """Open the wallpaper's page; for imported files, find it first (category is guessed)."""
        def run():
            e = next((l for l in load_likes() if l["id"] == entry["id"]), entry)
            url = e.get("page_url") or entry.get("page_url")
            if not url and e.get("slug"):
                self.q.put(("status", "Looking for the wallpaper page ..."))
                try:
                    res = resolve_page_url(e["slug"], e["id"], self.cfg["request_delay"])
                except Exception:  # noqa: BLE001
                    res = None
                if res:
                    url = res[0]
                    with DATA_LOCK:
                        likes = load_likes()
                        for x in likes:
                            if x["id"] == e["id"]:
                                x["page_url"], x["title"] = res[0], res[1] or x["title"]
                                x.pop("link_failed", None)
                        _save(LIKES_PATH, likes)
                    self.q.put(("links", None))
            if not url:                                       # last resort: web search
                from urllib.parse import quote_plus
                url = "https://www.google.com/search?q=" + quote_plus(
                    "site:4kwallpapers.com " + (e.get("slug") or e["title"]).replace("-", " "))
            webbrowser.open(url)
        threading.Thread(target=run, daemon=True).start()

    def start_link_resolver(self):
        """Background: guess page links of imported wallpapers (once, results are saved)."""
        if self._resolving or not any(
                not l.get("page_url") and l.get("slug") and not l.get("link_failed")
                for l in load_likes()):
            return
        self._resolving = True

        def run():
            try:
                if resolve_pending_links(self.cfg["request_delay"]):
                    self.q.put(("links", None))
            finally:
                self._resolving = False
        threading.Thread(target=run, daemon=True).start()

    # ---- big preview ("lightbox") ----
    def _eye_pil(self, S, col=(242, 245, 250, 255), pupil=(124, 252, 0, 255)):
        import math
        k = S / 96
        im = self.Image.new("RGBA", (max(8, int(S)), max(8, int(S))), (0, 0, 0, 0))
        d = self.ImageDraw.Draw(im)
        cx = cy = im.width / 2
        top = [(cx - 40 * k + 80 * k * i / 24, cy - 24 * k * math.sin(math.pi * i / 24)) for i in range(25)]
        bot = [(x, 2 * cy - y) for x, y in reversed(top)]
        d.line(top + bot + [top[0]], fill=col, width=max(2, int(7 * k)), joint="curve")
        d.ellipse((cx - 13 * k, cy - 13 * k, cx + 13 * k, cy + 13 * k), fill=pupil)
        return im

    def _eye_icon(self):
        if self._eye_img is None:
            im = self._eye_pil(96)
            self._eye_img = self.ctk.CTkImage(light_image=im, dark_image=im, size=(18, 18))
        return self._eye_img

    def _entry_file(self, e):
        p = Path(e.get("file") or "")
        if p.is_file():
            return str(p)
        for l in load_likes():
            if l["id"] == e["id"] and Path(l["file"]).is_file():
                return l["file"]
        t = THUMB_DIR / f"{e['id']}.jpg"
        return str(t) if t.is_file() else None

    def _lock_zoom_path(self):
        lk = self.cfg["lock"]
        mode = lk["mode"]
        if mode == "fixed":
            return lk.get("fixed")
        if mode == "windows":
            try:
                return spotlight_preview_path()
            except Exception:  # noqa: BLE001
                return None
        cur = load_lock_current()
        if cur and cur.get("mode") == mode:
            for k in ("file", "preview"):
                if cur.get(k) and Path(cur[k]).is_file():
                    return cur[k]
        return None

    def _ring(self, p, H):
        """Progress ring with the eye icon in the middle (RGBA, anti-aliased)."""
        Image, ImageDraw = self.Image, self.ImageDraw
        r = 0.12 * H
        sw_ = max(3, int(0.017 * H))
        D = int(2 * r + 2 * sw_ + 8)
        SS = 3
        big = Image.new("RGBA", (D * SS, D * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        c, R = D * SS / 2, r * SS
        box = (c - R, c - R, c + R, c + R)
        f = min(1.0, p * 6)                                          # ring fades in
        d.ellipse(box, fill=(13, 17, 23, int(150 * f)))
        d.arc(box, 0, 360, fill=(255, 255, 255, int(70 * f)), width=sw_ * SS)
        if p > 0.002:
            d.arc(box, -90, -90 + 360 * min(p, 1.0), fill=(124, 252, 0, int(255 * f)), width=sw_ * SS)
        eye = self._eye_pil(int(r * 1.05 * SS), col=(242, 245, 250, int(235 * f)),
                            pupil=(124, 252, 0, int(255 * f)))
        big.alpha_composite(eye, (int(c - eye.width / 2), int(c - eye.height / 2)))
        return big.resize((D, D), Image.LANCZOS)

    @staticmethod
    def _rgb(hexcol):
        return tuple(int(hexcol[i:i + 2], 16) for i in (1, 3, 5))

    def _dl_icon(self):
        """Down arrow with a line under it (the download symbol), drawn so it never depends on fonts."""
        if getattr(self, "_dl_img", None) is None:
            S = 96
            im = self.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d = self.ImageDraw.Draw(im)
            c = (242, 245, 250, 255)
            d.rounded_rectangle((42, 10, 54, 52), 5, fill=c)                 # shaft
            d.polygon([(22, 40), (74, 40), (48, 68)], fill=c)                # arrow head
            d.rounded_rectangle((20, 78, 76, 88), 5, fill=c)                 # the line below
            self._dl_img = self.ctk.CTkImage(light_image=im, dark_image=im, size=(16, 16))
        return self._dl_img

    def _dl_img(self, mode="light", p=0.0, dot=False):
        """Icon for the download button (22x22, the arrow is 16x16 in the middle).
        mode: light = white arrow | dark = dark arrow (green button) | fill = arrow filling with
        green from top to bottom (p = 0..1).  dot = small red dot in the top-right corner."""
        steps = 32
        k = int(round(max(0.0, min(1.0, p)) * steps)) if mode == "fill" else 0
        key = (mode, k, bool(dot))
        img = self._dl_prog_imgs.get(key)
        if img is not None:
            return img
        Image, ImageDraw = self.Image, self.ImageDraw
        S, N, off = 96, 132, 18
        mask = Image.new("L", (S, S), 0)
        d = ImageDraw.Draw(mask)
        d.rounded_rectangle((42, 10, 54, 52), 5, fill=255)                   # shaft
        d.polygon([(22, 40), (74, 40), (48, 68)], fill=255)                  # arrow head
        d.rounded_rectangle((20, 78, 76, 88), 5, fill=255)                   # the line below
        if mode == "fill":
            glyph = Image.new("RGBA", (S, S), (88, 98, 120, 255))            # empty part (dim)
            cut = int(10 + (88 - 10) * k / steps)                            # arrow spans y 10..88
            if cut > 0:
                glyph.paste(Image.new("RGBA", (S, cut), self._rgb(ACCENT) + (255,)), (0, 0))
        else:
            col = self._rgb(ACCENT_TEXT if mode == "dark" else TEXT)
            glyph = Image.new("RGBA", (S, S), col + (255,))
        glyph.putalpha(mask)
        canvas = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        canvas.alpha_composite(glyph, (off, off))
        if dot:
            ImageDraw.Draw(canvas).ellipse((N - 42, 0, N, 42), fill=(255, 59, 48, 255))
        img = self.ctk.CTkImage(light_image=canvas, dark_image=canvas, size=(22, 22))
        self._dl_prog_imgs[key] = img
        return img

    def _dl_info(self, e):
        """State of a Liked card's download button: is the file in the Liked folder, and does its
        quality differ from the one in Settings?"""
        try:
            ok = Path(e["file"]).is_file()
        except Exception:
            ok = False
        info = {"exists": ok, "diff": False, "label": None}
        if ok and not e.get("imported"):
            q, dims = file_info(e)
            sw, sh = screen_size()
            if q and not quality_matches(q, self.cfg["quality"], max(sw, sh)):
                info["diff"] = True
                info["label"] = QUALITIES[q] + (f" · {dims[0]}×{dims[1]}" if dims else "")
        return info

    def _apply_dl_state(self, wid):
        """Colour the button: grey + arrow filling while downloading, green when the file is in
        the Liked folder (red dot when its quality differs from Settings). Updates the button on
        the Liked tab and on the Lock tab."""
        st = self._dl_state.get(wid) or {}
        p = self._dl_prog.get(wid)
        for kind in ("liked", "lock"):
            b = self._dl_btns.get((kind, wid))
            if b is None:
                continue
            try:
                if p is not None and p < 1.0:
                    b.configure(image=self._dl_img("fill", p), fg_color=SURFACE2, hover_color=BORDER)
                elif p is not None or st.get("exists"):      # finished (or already on disk)
                    b.configure(image=self._dl_img("dark", dot=bool(st.get("diff")) and p is None),
                                fg_color=ACCENT, hover_color=ACCENT_HOVER)
                else:
                    b.configure(image=self._dl_img("light"), fg_color=SURFACE2, hover_color=BORDER)
            except Exception:  # button was destroyed by a refresh
                pass

    def _set_dl_prog(self, wid, p):
        """p = None -> back to the normal state."""
        if p is None:
            self._dl_prog.pop(wid, None)
        else:
            self._dl_prog[wid] = p
        self._apply_dl_state(wid)

    def _refresh_card(self, wid, kind="liked"):
        """Rebuild only this card (not the whole page). False if it can't."""
        old = self._cards.get((kind, wid))
        i = self._card_idx.get((kind, wid))
        e = next((l for l in load_likes() if l["id"] == wid), None)
        if old is None or i is None or e is None or self.current_page != ("Liked" if kind == "liked" else "Lock"):
            return False
        try:
            if not old.winfo_exists():
                return False
            if not old.winfo_manager():                      # hidden by the filter: just forget it
                old.destroy()
                self._cards.pop((kind, wid), None)
                return True
            holder = old.master
            old.destroy()
        except Exception:
            return False
        self.img_cache = {k: v for k, v in self.img_cache.items() if k[0] != wid}
        self._make_card(holder, kind, e, i, set())
        return True

    def _dl_hold(self, btn, wid):
        """Short click = download in the Settings quality; press-and-hold = quality menu."""
        st = {"job": None, "fired": False}

        def fire():
            st["job"] = None
            try:
                if not btn.winfo_exists():
                    return
            except Exception:
                return
            st["fired"] = True
            self._open_dl_menu(btn, wid)

        def press(_e):
            st["fired"] = False
            self._close_dl_menu()
            st["job"] = btn.after(450, fire)

        def release(_e):
            if st["job"] is not None:
                btn.after_cancel(st["job"])
                st["job"] = None
            if st["fired"]:                       # the menu opened: the release is not a click
                return
            try:
                x, y = btn.winfo_pointerxy()
                inside = (btn.winfo_rootx() <= x < btn.winfo_rootx() + btn.winfo_width()
                          and btn.winfo_rooty() <= y < btn.winfo_rooty() + btn.winfo_height())
            except Exception:
                inside = True
            if inside:
                self.redownload(wid)

        btn.bind("<ButtonPress-1>", press, add="+")
        btn.bind("<ButtonRelease-1>", release, add="+")

    def _close_dl_menu(self):
        m, self._dl_menu = self._dl_menu, None
        if m is not None:
            try:
                m.destroy()
            except Exception:
                pass

    def _open_dl_menu(self, btn, wid):
        """Three-option quality menu under the button (above it when there is no room below)."""
        ctk = self.ctk
        self._close_dl_menu()
        if wid in self._redl:
            return
        root = self.root
        if not self._dl_menu_bound:               # bound once; closes the menu on any outside action
            self._dl_menu_bound = True

            def outside(e):
                m = self._dl_menu
                if m is not None and not str(e.widget).startswith(str(m)):
                    self._close_dl_menu()
            root.bind("<Button-1>", outside, add="+")
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Escape>"):
                root.bind(seq, lambda e: self._close_dl_menu(), add="+")
        m = ctk.CTkFrame(root, fg_color=SURFACE, border_color=BORDER, border_width=1, corner_radius=12)
        cur = self.cfg["quality"]
        for key, label in (("low", "Low"), ("4k", "4K"), ("high", "High (original)")):
            ctk.CTkButton(m, text=("✓  " if key == cur else "     ") + label, anchor="w", width=140,
                          height=30, corner_radius=8, fg_color="transparent", hover_color=SURFACE2,
                          text_color=ACCENT if key == cur else TEXT, font=self.f_small,
                          command=lambda k=key: self._dl_pick(wid, k)).pack(fill="x", padx=4, pady=2)
        root.update_idletasks()
        mw, mh = m.winfo_reqwidth(), m.winfo_reqheight()
        rw, rh = root.winfo_width(), root.winfo_height()
        bx = btn.winfo_rootx() - root.winfo_rootx()
        by = btn.winfo_rooty() - root.winfo_rooty()
        bh = btn.winfo_height()
        y = by + bh + 4
        if y + mh > rh - 4 and by - mh - 4 >= 0:          # no room below -> open above
            y = by - mh - 4
        x = max(4, min(bx, rw - mw - 4))
        try:
            sc = m._get_widget_scaling()                  # place() wants unscaled units
        except Exception:
            sc = 1.0
        m.place(x=x / sc, y=y / sc)
        m.lift()
        self._dl_menu = m

    def _dl_pick(self, wid, key):
        self._close_dl_menu()
        self.redownload(wid, key)

    def redownload(self, wid, quality=None):
        """Liked page: download this wallpaper again (default: the quality from Settings) and
        replace it. The arrow fills from top to bottom while it downloads."""
        if wid in self._redl:
            return
        quality = quality if quality in QUALITIES else self.cfg["quality"]
        ql = QUALITIES[quality]
        self._redl.add(wid)
        self._set_dl_prog(wid, 0.0)
        self.say(f"Downloading in {ql} quality ...")
        last = [-1.0]

        def on_prog(p):
            if p >= 1.0 or p - last[0] >= 0.02:
                last[0] = p
                self.q.put(("redl_prog", (wid, p)))

        def run():
            try:
                e, changed = redownload_liked(wid, self.cfg, quality=quality, progress=on_prog)
                msg = (f"Replaced with the {ql} version: {e['title']}"
                       if changed else f"Already the {ql} version: {e['title']}")
                self.q.put(("redl", (wid, msg)))
            except Exception as ex:  # noqa: BLE001
                self.q.put(("status", f"Download failed: {str(ex)[:90]}"))
            finally:
                self._redl.discard(wid)
                self.q.put(("redl_prog", (wid, None)))
        threading.Thread(target=run, daemon=True).start()

    def _zoomable(self, label, path_fn, size=30):
        """Eye button (top-left of a preview) + press-and-hold -> soft push-in, a filling ring,
        then the big view grows out of the preview.

        The push-in is drawn on a light plain-Tk overlay at the exact pixel size of the preview.
        (Going through CTkImage rebuilt + rescaled an RGBA picture on every frame, which is what
        made it stutter.) While the user holds, the big view is already being prepared."""
        import tkinter as tk
        ctk, Image = self.ctk, self.Image
        eye = ctk.CTkFrame(label.master, width=size, height=size, corner_radius=size // 2,
                           fg_color="#0d1117")
        ic = ctk.CTkLabel(eye, text="", image=self._eye_icon(), fg_color="transparent")
        ic.place(relx=0.5, rely=0.5, anchor="center")
        for w in (eye, ic):
            w.bind("<Button-1>", lambda e: self.open_zoom(path_fn(), src=label))
            w.bind("<Enter>", lambda e: eye.configure(fg_color=ACCENT_DEEP))
            w.bind("<Leave>", lambda e: eye.configure(fg_color="#0d1117"))
        eye.place(in_=label, x=12, y=10)
        label._eye = eye

        st = {"mode": None, "p": 0.0, "t": 0.0, "el": 0.0, "ov": None, "ov_shown": False,
              "photo": None, "size": None, "rgb": None, "mask": None, "bgimg": None, "black": None,
              "prep": None, "prep_started": False, "path": None, "hires": False}

        def setup(img):
            scale = self._scale()
            sz = img.cget("size")
            iw, ih = max(8, int(round(sz[0] * scale))), max(8, int(round(sz[1] * scale)))
            src = img.cget("light_image").convert("RGBA")
            st["rgb"] = src.convert("RGB")
            st["mask"] = src.getchannel("A").resize((iw, ih), Image.LANCZOS)
            st["black"] = Image.new("RGB", (iw, ih), (0, 0, 0))
            st["bgimg"] = Image.new("RGB", (iw, ih), self._rgb(SURFACE))
            st["size"] = (iw, ih)
            st["photo"] = self.ImageTk.PhotoImage(st["bgimg"])
            ov = tk.Label(label.master, image=st["photo"], bd=0, highlightthickness=0,
                          padx=0, pady=0, bg=SURFACE)
            ov.image = st["photo"]
            ov.bind("<ButtonRelease-1>", release)
            ov.bind("<Leave>", leave)
            st["ov"] = ov

        def show_ov():
            if st["ov_shown"] or st["ov"] is None:
                return
            st["ov_shown"] = True
            inner = getattr(label, "_label", None)
            if inner is None:
                inner = label
            st["ov"].place(in_=inner, relx=0.5, rely=0.5, anchor="center")
            try:
                eye.lift()                                   # keep the eye above the overlay
            except Exception:  # noqa: BLE001
                pass

        def render(p):
            iw, ih = st["size"]
            w0, h0 = st["rgb"].size
            e = p * p * (3 - 2 * p)                          # smoothstep
            k = 1 + 0.05 * e                                 # slow push-in
            bw, bh = w0 / k, h0 / k
            fr = st["rgb"].resize((iw, ih), Image.BICUBIC,
                                  box=((w0 - bw) / 2, (h0 - bh) / 2, (w0 + bw) / 2, (h0 + bh) / 2))
            fr = Image.blend(fr, st["black"], 0.40 * e)      # soft dim
            ring = self._ring(p, ih)
            fr.paste(ring, ((iw - ring.width) // 2, (ih - ring.height) // 2), ring)
            fr = Image.composite(fr, st["bgimg"], st["mask"])   # rounded corners
            st["photo"].paste(fr)

        def reset():
            ov = st["ov"]
            if ov is not None:
                try:
                    ov.place_forget()
                    ov.destroy()
                except Exception:  # noqa: BLE001
                    pass
            if st["prep"] is not None:
                st["prep"].cancel.set()
            if st["hires"]:
                hires_timer(False)
            st.update(mode=None, p=0.0, ov=None, ov_shown=False, photo=None, size=None, rgb=None,
                      mask=None, bgimg=None, black=None, prep=None, prep_started=False, path=None,
                      hires=False)

        def step():
            if st["mode"] is None:
                return
            t0 = time.perf_counter()
            dt, st["t"] = t0 - st["t"], t0
            if st["mode"] == "hold":
                st["el"] += dt
                p = max(0.0, (st["el"] - self.ZOOM_DELAY) / self.ZOOM_HOLD)
                if p > 0 and not st["prep_started"]:         # start loading the big view now
                    st["prep_started"] = True
                    try:
                        path = path_fn()
                        if path and Path(path).is_file():
                            st["path"] = path
                            st["prep"] = self._zoom_prepare(path, label)
                    except Exception:  # noqa: BLE001
                        pass
                if p >= 1.0:
                    st["mode"] = "done"
                    show_ov()
                    render(1.0)
                    job, st["prep"] = st["prep"], None       # the big view owns it from now on
                    self.open_zoom(st["path"] or path_fn(), src=label, on_shown=reset, prep=job)
                    return
                if p > 0:
                    st["p"] = p
                    show_ov()
                    render(p)
            elif st["mode"] == "back":                       # let go early: ease back
                st["p"] -= dt / 0.22
                if st["p"] <= 0:
                    reset()
                    return
                render(st["p"])
            label.after(max(1, int(9 - (time.perf_counter() - t0) * 1000)), tick)

        def tick():
            try:
                step()
            except tk.TclError:                              # the preview was rebuilt meanwhile
                try:
                    reset()
                except Exception:  # noqa: BLE001
                    pass

        def press(_e=None):
            if st["mode"] is not None:
                return
            img = label.cget("image")
            if img is None:
                return
            try:
                setup(img)
            except Exception:  # noqa: BLE001
                reset()
                return
            st.update(mode="hold", p=0.0, el=0.0, t=time.perf_counter(), hires=True)
            hires_timer(True)
            tick()

        def release(_e=None):
            if st["mode"] == "hold":
                if st["prep"] is not None:                   # let go early: stop preparing
                    st["prep"].cancel.set()
                    st["prep"] = None
                if st["p"] <= 0:
                    reset()
                else:
                    st["mode"] = "back"
            elif st["mode"] == "done" and self._zoom is None and self._zoom_job is None:
                reset()                                      # the big view could not open

        def leave(_e=None):
            # the overlay sits on top of the label, so Tk reports a "leave" when it appears;
            # only cancel when the pointer is really outside the preview
            try:
                px, py = label.winfo_pointerxy()
                x0, y0 = label.winfo_rootx(), label.winfo_rooty()
                if x0 <= px < x0 + label.winfo_width() and y0 <= py < y0 + label.winfo_height():
                    return
            except tk.TclError:
                pass
            release()

        label.bind("<ButtonPress-1>", press)
        label.bind("<ButtonRelease-1>", release)
        label.bind("<Leave>", leave)

    # ---- the big view -------------------------------------------------------------------- #
    def _zoom_prepare(self, path, src):
        """Start loading the big view in the background (screenshot + blur, picture, frames)."""
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            m = int(round(self.root.winfo_fpixels("1c")))           # 1 cm in pixels
        except Exception:  # noqa: BLE001
            m = 38
        m = min(max(m, 20), min(sw, sh) // 6)
        try:                                                         # where the preview sits now
            rect = (src.winfo_rootx(), src.winfo_rooty(), src.winfo_width(), src.winfo_height())
        except Exception:  # noqa: BLE001
            rect = None
        return ZoomJob(path, sw, sh, m, rect)

    def open_zoom(self, path, src=None, on_shown=None, prep=None):
        """Big preview: grows out of `src` to a box ~1 cm from every screen edge, while the screen
        behind it fades to a blurred, dimmed copy. Closing plays the same way backwards.
        Nothing heavy runs on the UI thread: the window opens as soon as the loader is ready."""
        if self._zoom is not None or self._zoom_job is not None:
            if prep is not None:
                prep.cancel.set()
            return
        if not path or not Path(path).is_file():
            self.say("No image to enlarge yet")
            if prep is not None:
                prep.cancel.set()
            if on_shown:
                on_shown()
            return
        if prep is None or prep.path != path:
            if prep is not None:
                prep.cancel.set()
            prep = self._zoom_prepare(path, src)
        self._zoom_job = prep
        hires_timer(True)
        self._zoom_wait(prep, on_shown)

    def _zoom_wait(self, job, on_shown):
        if self._zoom_job is not job:                                # closed / replaced meanwhile
            return
        if not job.ready.is_set():
            self.root.after(8, lambda: self._zoom_wait(job, on_shown))
            return
        if job.error is not None:
            self._zoom_job = None
            hires_timer(False)
            self.say(f"Cannot open image: {job.error}")
            if on_shown:
                on_shown()
            return
        self._zoom_show(job, on_shown)

    def _zoom_show(self, job, on_shown):
        import tkinter as tk
        ImageTk = self.ImageTk
        sw, sh, m, N = job.sw, job.sh, job.m, job.N

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.configure(bg="#0b0e15")
        top.geometry(f"{sw}x{sh}+0+0")
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        cv = tk.Canvas(top, width=sw, height=sh, highlightthickness=0, bd=0, bg="#0b0e15")
        cv.pack()
        top._bg = ImageTk.PhotoImage(job.bg)
        cv.create_image(0, 0, image=top._bg, anchor="nw")
        cv.create_text(sw // 2, max(10, m // 2), text="click anywhere or press Esc to close",
                       fill=MUTED, font=("Segoe UI", 10))
        item = cv.create_image(0, 0, anchor="nw")
        state = {"j": None, "cur": 0, "alpha": 0.0, "job": None, "closing": False}

        def show(j):
            if j == state["j"]:                                      # nothing moved: skip the work
                return
            state["j"] = state["cur"] = j
            region, cx, cy = job.render(j)                           # ready-made by the workers
            ph = ImageTk.PhotoImage(region)
            cv.itemconfig(item, image=ph)
            cv.coords(item, cx, cy)
            top._ph = ph
            a = min(1.0, job.u_of(j) / 0.5)                          # backdrop cross-fades in
            if a != state["alpha"]:                                  # only touch -alpha when it changes
                state["alpha"] = a
                try:
                    top.attributes("-alpha", a)
                except tk.TclError:
                    pass

        def run(direction, dur, j0, done=None):
            t0 = time.perf_counter()

            def step():
                if self._zoom is not top:
                    return
                t = min(1.0, (time.perf_counter() - t0) / dur)
                if direction > 0:                                    # frames are evenly spaced in time
                    j = int(round(j0 + (N - 1 - j0) * t))
                else:                                                # exactly the same frames, reversed
                    j = int(round(j0 * (1 - t)))
                show(j)
                if t < 1.0:
                    state["job"] = top.after(2, step)
                elif done:
                    done()
            step()

        def start_close():
            if state["closing"]:
                return
            state["closing"] = True
            if state["job"]:
                try:
                    top.after_cancel(state["job"])
                except Exception:  # noqa: BLE001
                    pass
            j0 = state["cur"]
            run(-1, max(0.12, 0.26 * j0 / (N - 1)), j0, lambda: self._zoom_close(now=True))

        top._anim_close = start_close
        cv.bind("<Button-1>", lambda e: self._zoom_close())
        top.bind("<Escape>", lambda e: self._zoom_close())
        self._zoom = top
        show(0)
        top.update_idletasks()
        top.focus_force()
        if on_shown:
            top.after(180, on_shown)                                 # by now the backdrop covers it
        run(1, 0.42, 0)

    def _zoom_close(self, now=False):
        z = self._zoom
        if z is None:
            job, self._zoom_job = self._zoom_job, None               # still loading: just stop
            if job is not None:
                job.cancel.set()
                hires_timer(False)
            return
        if not now and getattr(z, "_anim_close", None):
            z._anim_close()
            return
        self._zoom = None
        job, self._zoom_job = self._zoom_job, None
        if job is not None:
            job.cancel.set()
        hires_timer(False)
        try:
            z.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _scale(self):
        try:
            return float(self.ctk.ScalingTracker.get_window_scaling(self.root))
        except Exception:
            return 1.0

    def _sync_liked_last(self, e):
        if e["id"] in {l["id"] for l in load_likes()}:
            self.cfg["liked_last"] = e["id"]       # liked loop continues from here
            save_config(self.cfg)

    def _advance(self, log):
        """Next in history if we went back, otherwise a brand-new wallpaper."""
        if has_forward():
            e = go_forward()
            log(f"Next: {e['title']}")
        else:
            e = change_cycle(self.cfg, log)
            save_config(self.cfg)
        self._sync_liked_last(e)
        return e

    def next_now(self):
        """Tap Next: forward in history if possible, else a new wallpaper."""
        self._unpin_desktop()

        def run():
            with self.nav_lock:
                try:
                    e = self._advance(lambda m: self.q.put(("status", m)))
                    self.q.put(("changed", e))
                except Exception as ex:  # noqa: BLE001
                    self.q.put(("status", f"Error: {ex}"))
            self.wake.set()                              # restart the timer only
        self.say("Working ...")
        threading.Thread(target=run, daemon=True).start()

    def latest_now(self):
        """Hold on Next: jump to the newest wallpaper in history."""
        self._unpin_desktop()

        def run():
            with self.nav_lock:
                try:
                    e = go_latest()
                    self._sync_liked_last(e)
                    self.q.put(("changed", e))
                    self.q.put(("status", f"Latest: {e['title']}"))
                except Exception as ex:  # noqa: BLE001
                    self.q.put(("status", f"{ex}"))
            self.wake.set()
        threading.Thread(target=run, daemon=True).start()

    def prev_now(self):
        self._unpin_desktop()

        def run():
            with self.nav_lock:
                try:
                    e = go_previous()
                    self._sync_liked_last(e)
                    self.q.put(("changed", e))
                    self.q.put(("status", f"Previous: {e['title']}"))
                except Exception as ex:  # noqa: BLE001
                    self.q.put(("status", f"{ex}"))
            self.wake.set()                              # restart the timer, no forced change
        threading.Thread(target=run, daemon=True).start()

    def set_entry(self, entry):
        self._unpin_desktop()

        def run():
            try:
                e = ensure_local(entry, self.cfg)
                apply_entry(e)
                self.q.put(("changed", e))
                self.q.put(("status", f"Set: {e['title']}"))
            except Exception as ex:  # noqa: BLE001
                self.q.put(("status", f"Error: {ex}"))
        self.say("Setting wallpaper ...")
        threading.Thread(target=run, daemon=True).start()

    def on_mode(self, value):
        self._unpin_desktop()
        self.cfg["mode"] = "liked" if value == "Liked loop" else "auto"
        self.interval_var.set(str(self.cfg["intervals"][self.cfg["mode"]]))
        save_config(self.cfg)
        self.force = "new" if not self.paused else None
        self.wake.set()
        self.hint.configure(text=self.hint_text())
        self.refresh_state_label()

    def set_mode(self, mode):          # from tray
        self.mode_seg.set("Liked loop" if mode == "liked" else "Auto")
        self.on_mode("Liked loop" if mode == "liked" else "Auto")

    def commit_interval(self):
        try:
            v = max(1, int(self.interval_var.get()))
        except ValueError:
            v = self.cfg["intervals"][self.cfg["mode"]]
        self.interval_var.set(str(v))
        if v != self.cfg["intervals"][self.cfg["mode"]]:
            self.cfg["intervals"][self.cfg["mode"]] = v
            save_config(self.cfg)
            self.wake.set()            # restart timer, no forced change

    def on_run_toggle(self):
        self.paused = not self.run_var.get()
        self.cfg["auto"] = not self.paused
        save_config(self.cfg)
        self.wake.set()
        self.refresh_state_label()
        self.say("Paused" if self.paused else "Auto change on")

    def toggle_pause(self):            # from tray
        self.run_var.set(self.paused)
        self.on_run_toggle()

    @staticmethod
    def _show_cats(box, anchor, on):
        """Show the category list (right under its source menu) or hide it."""
        if on:
            box.pack(fill="x", after=anchor)
        else:
            box.pack_forget()

    def on_source(self, value):
        self.cfg["source"] = value
        save_config(self.cfg)
        self._show_cats(self.cat_box, self.source_menu, value == "Selected categories")
        self.hint.configure(text=self.hint_text())

    def on_categories(self):
        self.cfg["categories"] = [s for s, v in self.cat_vars.items() if v.get()]
        save_config(self.cfg)
        self.hint.configure(text=self.hint_text())

    def set_all_cats(self, val):
        for v in self.cat_vars.values():
            v.set(val)
        self.on_categories()

    def commit_depth(self):
        try:
            v = min(500, max(1, int(self.depth_var.get())))
        except ValueError:
            v = self.cfg["max_pages"]
        self.depth_var.set(str(v))
        self.cfg["max_pages"] = v
        save_config(self.cfg)

    # ---- Lock tab ----
    def _opt(self, parent, values, var, cmd, width=200, height=34):
        return self.ctk.CTkOptionMenu(
            parent, values=values, variable=var, command=cmd, fg_color=SURFACE, button_color=SURFACE2,
            button_hover_color=BORDER, dropdown_fg_color=SURFACE, dropdown_hover_color=SURFACE2,
            text_color=TEXT, font=self.f_body, dropdown_font=self.f_body, width=width, height=height)

    def _seg(self, parent, values, cmd):
        return self.ctk.CTkSegmentedButton(
            parent, values=values, command=cmd, height=34, font=self.f_body, fg_color=SURFACE,
            selected_color=ACCENT_DEEP, selected_hover_color="#4aa30e", unselected_color=SURFACE,
            unselected_hover_color=SURFACE2, text_color=TEXT)

    def _note(self, parent, text):
        self.ctk.CTkLabel(parent, text=text, font=self.f_small, text_color=MUTED, wraplength=470,
                          justify="left").pack(anchor="w", padx=10, pady=(6, 0))

    def _build_lock(self, parent):
        ctk = self.ctk
        f = ctk.CTkScrollableFrame(parent, fg_color="transparent",
                                   scrollbar_button_color=SURFACE2, scrollbar_button_hover_color=BORDER)
        self._section(f, "Lock screen / sign-in screen")
        self.lock_mode_var = ctk.StringVar(value=LOCK_MODES[self.cfg["lock"]["mode"]])
        self._opt(f, list(LOCK_MODES.values()), self.lock_mode_var, self.on_lock_mode, 260).pack(
            anchor="w", padx=8)
        self.lock_status = ctk.CTkLabel(f, text="", font=self.f_small, text_color=MUTED,
                                        wraplength=470, justify="left")
        self.lock_status.pack(anchor="w", padx=10, pady=(6, 0))
        self.lock_body = ctk.CTkFrame(f, fg_color="transparent")
        self.lock_body.pack(fill="x", pady=(4, 0))
        self.refresh_lock()
        return f

    def _lock_pinned(self):
        pin, cur = self.cfg["lock"].get("pin"), load_lock_current()
        return bool(pin and cur and cur.get("file") == pin and cur.get("mode") == self.cfg["lock"]["mode"])

    def _lock_unpin(self):
        if self.cfg["lock"].get("pin"):
            self.cfg["lock"]["pin"] = None
            save_config(self.cfg)
            self.q.put(("pin", None))

    def pin_lock(self):
        cur = load_lock_current()
        mode = self.cfg["lock"]["mode"]
        if not cur or cur.get("mode") != mode:
            self.lock_status.configure(text="Nothing on the lock screen to lock yet.")
            return
        if self._lock_pinned():
            self.cfg["lock"]["pin"] = None
            msg = "Unlocked - the lock screen changes again."
            if mode == "same":
                self.lock_wake.set()                              # catch up with the desktop
        else:
            self.cfg["lock"]["pin"] = cur.get("file")
            msg = "Locked - the lock screen stays on this picture."
        save_config(self.cfg)
        self.lock_status.configure(text=msg)
        self.refresh_lock_preview()

    def _lock_preview_card(self, body, mode):
        ctk = self.ctk
        card = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=18)
        card.pack(fill="x", padx=16, pady=(10, 4))
        self.lock_img = ctk.CTkLabel(card, text="", text_color=MUTED, width=468, height=263,
                                     fg_color=SURFACE, corner_radius=12)
        self.lock_img.pack(padx=14, pady=(14, 8))
        self._zoomable(self.lock_img, self._lock_zoom_path)
        self.lock_title = ctk.CTkLabel(card, text="", font=self.f_title, text_color=TEXT, anchor="w",
                                       justify="left", wraplength=460)
        self.lock_title.pack(fill="x", padx=18)
        if mode in ("auto", "same", "liked"):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(8, 14))
            if mode in ("auto", "liked"):
                self._btn(row, "⏮  Prev", self.lock_prev, width=80).pack(side="left", padx=(0, 4))
            self.lock_like_btn = self._btn(row, "♡  Like", self.like_lock, width=92)
            self.lock_like_btn.pack(side="left")
            if mode in ("auto", "liked"):
                self._btn(row, "Next  ⏭", self.lock_next, width=80).pack(side="left", padx=4)
            self._btn(row, "Open page", self.open_lock_page, width=88).pack(side="left", padx=4)
            self.lock_pin_btn = self._btn(row, self.PIN_OFF, self.pin_lock, width=88)
            self.lock_pin_btn.pack(side="left")
        else:
            ctk.CTkFrame(card, fg_color="transparent", height=10).pack()

    def _lock_ctkimage(self, path):
        from PIL import ImageOps
        im = self.Image.open(path).convert("RGB")
        im = ImageOps.fit(im, (936, 526))
        im = im.convert("RGBA")
        mask = self.Image.new("L", im.size, 0)
        self.ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), 28, fill=255)
        im.putalpha(mask)
        return self.ctk.CTkImage(light_image=im, dark_image=im, size=(468, 263))

    def refresh_lock_preview(self):
        """Show the picture that is (or will be) on the lock screen for the current mode."""
        if self.lock_img is None or not self.lock_img.winfo_exists():
            return
        lk = self.cfg["lock"]
        mode = lk["mode"]
        path = title = cur = None
        note = "Nothing applied yet. Press Apply now."
        if mode == "windows":
            try:
                path = spotlight_preview_path()
            except Exception:  # noqa: BLE001  (registry layouts differ between Windows builds)
                path = None
            title = "Windows Spotlight"
            note = "Windows chooses this picture. A preview is not available."
        elif mode == "fixed":
            path, title = lk.get("fixed"), lk.get("fixed_name")
            note = "No fixed image chosen yet."
        else:
            cur = load_lock_current()
            if cur and cur.get("mode") == mode:
                path, title = cur.get("preview") or cur.get("file"), cur.get("title")
            else:
                cur = None
        img = None
        if path and Path(path).exists():
            try:
                img = self._lock_ctkimage(path)
            except Exception:  # noqa: BLE001
                img = None
        self.lock_img_ref = img                                 # keep a reference
        self.lock_img.configure(image=img, text="" if img else note)
        self.lock_title.configure(text=short(title or "", 70) if img else "")
        if self.lock_like_btn is not None and self.lock_like_btn.winfo_exists():
            liked = bool(cur and cur.get("id") and is_liked(cur["id"]))
            self.lock_like_btn.configure(text="♥  Liked" if liked else "♡  Like",
                                         text_color=ACCENT if liked else TEXT)
        if self.lock_pin_btn is not None and self.lock_pin_btn.winfo_exists():
            pinned = self._lock_pinned()
            self.lock_pin_btn.configure(text=self.PIN_ON if pinned else self.PIN_OFF,
                                        fg_color=ACCENT if pinned else SURFACE2,
                                        hover_color=ACCENT_HOVER if pinned else BORDER,
                                        text_color=ACCENT_TEXT if pinned else TEXT)

    def like_lock(self):
        cur = load_lock_current()
        if not cur or not cur.get("id"):
            self.lock_status.configure(text="Nothing to like here (this picture has no wallpaper ID).")
            return
        entry = {k: cur[k] for k in ("id", "title", "page_url", "img_url", "slug") if cur.get(k)}
        src = cur.get("file")
        if not (src and Path(src).exists()):                    # original is gone (e.g. just unliked)
            prev = cur.get("preview")
            if not (prev and Path(prev).exists()):
                self.lock_status.configure(text="The image file is no longer available.")
                return
            name = Path(cur["img_url"]).name if cur.get("img_url") else f"{cur['id']}.jpg"
            tmp = LOCK_DIR / "relike"
            tmp.mkdir(parents=True, exist_ok=True)
            src = tmp / name
            shutil.copyfile(prev, src)
        entry.update(file=str(src), ts=int(time.time()))
        liked = toggle_like(entry)
        self.lock_status.configure(text="Added to Liked" if liked else "Removed from Liked")
        self.refresh_lock_preview()
        self.refresh_home()
        if self.current_page == "Liked":
            self.refresh_gallery("liked")

    def open_lock_page(self):
        cur = load_lock_current()
        if cur and cur.get("id"):
            self.open_page(cur)
        else:
            self.lock_status.configure(text="This picture has no page to open.")

    def set_lock_entry(self, entry):
        """Lock tab: put this liked wallpaper on the lock screen (not on the desktop)."""
        mode = self.cfg["lock"]["mode"]
        self._lock_unpin()
        self.lock_status.configure(text="Working ...")

        def run():
            with self.lock_nav:
                try:
                    e = ensure_local(entry, self.cfg)
                    meta = _lock_meta(e, e["file"])
                    apply_lock_entry(meta, mode)
                    if mode in ("auto", "liked"):
                        lock_hist_add(meta, mode)
                    _lock_last["id"] = Path(e["file"]).name if mode == "auto" else e["id"]   # not repeated next
                    msg = f"Lock screen: {e.get('title') or Path(e['file']).name}"
                except Exception as ex:  # noqa: BLE001
                    msg = f"Lock screen error: {str(ex)[:120]}"
            self.q.put(("lock_status", msg))
        threading.Thread(target=run, daemon=True).start()

    def lock_prev(self):
        self._lock_nav(-1)

    def lock_next(self):
        self._lock_nav(1)

    def _lock_nav(self, step):
        mode = self.cfg["lock"]["mode"]
        if mode not in ("auto", "liked"):
            return
        self._lock_unpin()

        def run():
            with self.lock_nav:
                try:
                    if step < 0:
                        meta = lock_hist_move(-1, mode)
                        if not meta:
                            self.q.put(("lock_status", "No earlier lock screen image."))
                            return
                        apply_lock_entry(meta, mode)
                        msg = f"Lock screen: {meta.get('title', '')}"
                    elif lock_hist_has_forward(mode):
                        meta = lock_hist_move(1, mode)
                        apply_lock_entry(meta, mode)
                        msg = f"Lock screen: {meta.get('title', '')}"
                    else:
                        msg = apply_lock_screen(self.cfg, lambda m: None)
                except Exception as e:  # noqa: BLE001
                    msg = f"Lock screen error: {e}"
            self.q.put(("lock_status", msg))
        self.lock_status.configure(text="Working ...")
        threading.Thread(target=run, daemon=True).start()

    def _lock_interval_row(self, body):
        ctk = self.ctk
        lk = self.cfg["lock"]
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(anchor="w", padx=8, pady=(10, 0))
        ctk.CTkLabel(row, text="Change every", font=self.f_small, text_color=MUTED).pack(side="left")
        self.lock_iv_var = ctk.StringVar(value=LOCK_INTERVALS[lk["interval"]])
        self._opt(row, list(LOCK_INTERVALS.values()), self.lock_iv_var, self.on_lock_interval,
                  120, 30).pack(side="left", padx=8)
        self._btn(row, "Apply now", self.on_lock_now, width=100, height=30).pack(side="left")

    def refresh_lock(self):
        ctk = self.ctk
        lk = self.cfg["lock"]
        mode = lk["mode"]
        body = self.lock_body
        for w in body.winfo_children():
            w.destroy()
        self.lock_img = self.lock_like_btn = self.lock_pin_btn = None
        if mode != "off":
            self._lock_preview_card(body, mode)
            self.refresh_lock_preview()
        if mode == "off":
            self._note(body, "The app does not touch your lock screen.")
        elif mode == "windows":
            self._note(body, "Gives the lock screen back to Windows (Windows Spotlight). Your own "
                             "pictures are not deleted.")
            self._btn(body, "Switch to Windows default now", self.on_lock_now, accent=True,
                      width=230).pack(anchor="w", padx=8, pady=(10, 0))
        elif mode == "same":
            self._note(body, "The lock screen follows every desktop wallpaper change (Prev / Next / "
                             "timer).")
            self._btn(body, "Apply now", self.on_lock_now, width=110).pack(anchor="w", padx=8, pady=(10, 0))
        elif mode == "fixed":
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(anchor="w", padx=8, pady=(10, 0))
            self._btn(row, "Choose image ...", self.on_lock_pick, width=150).pack(side="left")
            ctk.CTkLabel(row, text=lk.get("fixed_name") or "none", font=self.f_small,
                         text_color=MUTED).pack(side="left", padx=10)
            self._note(body, "This one picture stays on the lock screen until you change it.")
        elif mode == "liked":
            self._lock_interval_row(body)
            self._lock_liked(body)
        elif mode == "auto":
            self._lock_interval_row(body)
            self._section(body, "Source")
            self.lock_source_var = ctk.StringVar(value=lk.get("source", "Selected categories"))
            self.lock_src_menu = self._opt(body, ["Selected categories"] + list(FEEDS),
                                           self.lock_source_var, self.on_lock_source, 260, 34)
            self.lock_src_menu.pack(anchor="w", padx=8)
            self.lock_cat_box = ctk.CTkFrame(body, fg_color="transparent")   # only for "Selected categories"
            self._section(self.lock_cat_box, "Categories")
            grid = ctk.CTkFrame(self.lock_cat_box, fg_color=SURFACE, corner_radius=16)
            grid.pack(fill="x", padx=6)
            self.lock_cat_vars = {}
            for i, (label, slug) in enumerate(CATEGORIES.items()):
                v = ctk.BooleanVar(value=slug in (lk.get("categories") or []))
                self.lock_cat_vars[slug] = v
                ctk.CTkCheckBox(grid, text=label, variable=v, command=self.on_lock_cats, width=140,
                                checkbox_width=20, checkbox_height=20, corner_radius=6, border_width=2,
                                fg_color=ACCENT, hover_color=ACCENT_HOVER, checkmark_color=ACCENT_TEXT,
                                border_color=MUTED, text_color=TEXT, font=self.f_small
                                ).grid(row=i // 3, column=i % 3, sticky="w", padx=12, pady=6)
            r = ctk.CTkFrame(grid, fg_color="transparent")
            r.grid(row=11, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 10))
            self._btn(r, "All", lambda: self.set_all_lock_cats(True), width=60, height=28).pack(side="left")
            self._btn(r, "None", lambda: self.set_all_lock_cats(False), width=60, height=28).pack(
                side="left", padx=6)
            self._note(self.lock_cat_box, "Nothing ticked = any category.")
            self._show_cats(self.lock_cat_box, self.lock_src_menu,
                            lk.get("source", "Selected categories") == "Selected categories")

            self._section(body, "Search depth (pages)")
            self.lock_depth_var = ctk.StringVar(value=str(lk["max_pages"]))
            e = ctk.CTkEntry(body, textvariable=self.lock_depth_var, width=80, height=34, fg_color=SURFACE,
                             border_color=BORDER, text_color=TEXT, justify="center")
            e.pack(anchor="w", padx=8)
            e.bind("<Return>", lambda ev: self.commit_lock_depth())
            e.bind("<FocusOut>", lambda ev: self.commit_lock_depth())

            self._section(body, "Download quality")
            seg = self._seg(body, list(QUALITIES.values()), self.on_lock_quality)
            seg.pack(anchor="w", padx=8)
            seg.set(QUALITIES[lk["quality"]])
            self._note(body, "Low: smallest size that covers your screen. 4K: the site's 4K download. "
                             "High: the Original (largest).")

            self._section(body, "Keep last wallpapers on disk")
            seg = self._seg(body, ["10", "20", "40"], self.on_lock_keep)
            seg.pack(anchor="w", padx=8)
            seg.set(str(lk["keep"]))
            self._note(body, "Downloaded lock screen images. The oldest one is deleted when a new one is "
                             "added. If you are offline, one of the saved ones is used.")
            self._lock_recent(body)
        if mode in ("liked", "auto", "fixed", "same", "windows"):
            self._note(body, "The sign-in (password) screen shows the lock screen picture. In Windows "
                             "Settings > Personalization > Lock screen choose \"Picture\" (not Windows "
                             "Spotlight) and keep \"Show the lock screen background picture on the "
                             "sign-in screen\" turned on.")
        ctk.CTkFrame(body, fg_color="transparent", height=16).pack()

    def _lock_liked(self, body):
        ctk = self.ctk
        self._section(body, "Liked wallpapers to show")
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", padx=8)
        self.lock_liked_info = ctk.CTkLabel(head, text="", font=self.f_small, text_color=MUTED)
        self.lock_liked_info.pack(side="left")
        self._btn(head, "None", lambda: self.lock_select_all(False), width=60, height=28).pack(side="right")
        self._btn(head, "All", lambda: self.lock_select_all(True), width=50, height=28).pack(
            side="right", padx=6)
        self._update_lock_liked_info()
        frow = ctk.CTkFrame(body, fg_color="transparent")        # filter + sort, like the Liked tab
        frow.pack(fill="x", padx=8, pady=(8, 0))
        self.lock_cat_var = ctk.StringVar(value="All categories")
        self.lock_cat_menu = self._opt(frow, ["All categories"], self.lock_cat_var,
                                       self.on_lock_liked_cat, 200, 30)
        self.lock_cat_menu.pack(side="left")
        self.lock_sort_var = ctk.StringVar(value="Oldest first" if self._lock_sort == "old" else "Newest first")
        self._opt(frow, ["Newest first", "Oldest first"], self.lock_sort_var,
                  self.on_lock_liked_sort, 140, 30).pack(side="left", padx=8)
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x", pady=(6, 0))
        grid.grid_columnconfigure((0, 1), weight=1)
        self.lock_cards_grid = grid
        self._fill_lock_cards()

    def _lock_recent(self, body):
        ctk = self.ctk
        self._section(body, "Recent wallpapers to show")
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", padx=8)
        self.lock_recent_info = ctk.CTkLabel(head, text="", font=self.f_small, text_color=MUTED)
        self.lock_recent_info.pack(side="left")
        self._btn(head, "📁", lambda: open_folder(LOCK_DL), width=40, height=28).pack(side="right")
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x", pady=(6, 0))
        grid.grid_columnconfigure((0, 1), weight=1)
        self.lock_recent_grid = grid
        self._fill_lock_recent()

    def _fill_lock_recent(self):
        """(Re)build the boxes of the Lock tab's recent list - only that part of the page."""
        ctk = self.ctk
        grid = getattr(self, "lock_recent_grid", None)
        try:
            if grid is None or not grid.winfo_exists():
                return
        except Exception:
            return
        for w in grid.winfo_children():
            w.destroy()
        self._cards = {k: v for k, v in self._cards.items() if k[0] != "lockrecent"}
        entries = lock_recent_entries()
        self.lock_recent_info.configure(text=f"{len(entries)} saved for the lock screen")
        if not entries:
            ctk.CTkLabel(grid, text="Nothing saved yet. Images show up here after the lock screen "
                                    "gets its first wallpaper from the site.", font=self.f_small,
                         text_color=MUTED, wraplength=470, justify="left").grid(
                row=0, column=0, columnspan=2, sticky="w", padx=10, pady=6)
            return
        liked_ids = {l["id"] for l in load_likes()}
        self._lazy_cards(grid, "lockrecent", entries, liked_ids)

    def _fill_lock_cards(self, reuse=False):
        """(Re)build the boxes of the Lock tab's liked list - only that part of the page.
        reuse=True (filter / sort changed): keep the boxes that exist and re-arrange them."""
        ctk = self.ctk
        grid = getattr(self, "lock_cards_grid", None)
        try:
            if grid is None or not grid.winfo_exists():
                return
        except Exception:
            return
        self._close_dl_menu()
        all_likes = load_likes()
        reuse = reuse and bool(all_likes)
        if reuse:
            for key, c in list(self._cards.items()):
                if key[0] == "lock" and self._alive(c):
                    c.grid_remove()
        else:
            for w in grid.winfo_children():
                w.destroy()
            self._cards = {k: v for k, v in self._cards.items() if k[0] != "lock"}
            self._dl_btns = {k: v for k, v in self._dl_btns.items() if k[0] != "lock"}
        if not all_likes:
            ctk.CTkLabel(grid, text="Nothing liked yet. Press ♡ Like on the Home tab, or drop images "
                                    "into the liked folder.", font=self.f_small, text_color=MUTED,
                         wraplength=470, justify="left").grid(row=0, column=0, columnspan=2,
                                                              sticky="w", padx=10, pady=6)
            return
        opts = liked_cat_options(all_likes)
        if self._lock_cat not in opts.values():
            self._lock_cat = None
        self._lock_cat_map = opts
        self.lock_cat_menu.configure(values=list(opts))
        self.lock_cat_var.set(next(t for t, c in opts.items() if c == self._lock_cat))
        self._lazy_cards(grid, "lock", liked_view(all_likes, self._lock_cat, self._lock_sort), set())

    def on_lock_liked_cat(self, value):
        self._lock_cat = self._lock_cat_map.get(value)
        self._fill_lock_cards(reuse=True)

    def on_lock_liked_sort(self, value):
        self._lock_sort = "old" if value.startswith("Oldest") else "new"
        self._fill_lock_cards(reuse=True)

    def _update_lock_liked_info(self):
        likes = load_likes()
        n = sum(1 for l in likes if l.get("lock_sel", True))
        self.lock_liked_info.configure(text=f"{n} of {len(likes)} shown on the lock screen")

    def on_lock_select(self, wid, val):
        set_lock_selected(wid, val)
        self._update_lock_liked_info()

    def lock_select_all(self, val):
        set_all_lock_selected(val)
        self._fill_lock_cards()
        self._update_lock_liked_info()

    def on_lock_mode(self, label):
        self.cfg["lock"]["pin"] = None
        self.cfg["lock"]["mode"] = next(k for k, v in LOCK_MODES.items() if v == label)
        save_config(self.cfg)
        self.lock_status.configure(text="" if self.cfg["lock"]["mode"] == "off" else "Applying ...")
        self.refresh_lock()
        self.lock_wake.set()

    def on_lock_pick(self):
        from tkinter import filedialog
        f = filedialog.askopenfilename(title="Lock screen image", filetypes=[
            ("Images", "*.jpg *.jpeg *.png *.bmp *.webp"), ("All files", "*.*")])
        if not f:
            return
        try:
            LOCK_DIR.mkdir(parents=True, exist_ok=True)
            for old in LOCK_DIR.glob("fixed.*"):
                old.unlink()
            dst = LOCK_DIR / ("fixed" + Path(f).suffix.lower())
            shutil.copyfile(f, dst)
        except OSError as e:
            self.lock_status.configure(text=f"Could not copy the image: {e}")
            return
        self.cfg["lock"].update(fixed=str(dst), fixed_name=Path(f).name, mode="fixed", pin=None)
        save_config(self.cfg)
        self.lock_mode_var.set(LOCK_MODES["fixed"])
        self.refresh_lock()
        self.lock_wake.set()

    def on_lock_interval(self, label):
        self.cfg["lock"]["interval"] = next(k for k, v in LOCK_INTERVALS.items() if v == label)
        save_config(self.cfg)               # takes effect after the current wait

    def on_lock_now(self):
        self._lock_unpin()
        if self.cfg["lock"]["mode"] != "off":
            self.lock_status.configure(text="Applying ...")
            self.lock_wake.set()

    def on_lock_source(self, value):
        self.cfg["lock"]["source"] = value
        save_config(self.cfg)
        self._show_cats(self.lock_cat_box, self.lock_src_menu, value == "Selected categories")

    def on_lock_cats(self):
        self.cfg["lock"]["categories"] = [s for s, v in self.lock_cat_vars.items() if v.get()]
        save_config(self.cfg)

    def set_all_lock_cats(self, val):
        for v in self.lock_cat_vars.values():
            v.set(val)
        self.on_lock_cats()

    def commit_lock_depth(self):
        try:
            v = min(500, max(1, int(self.lock_depth_var.get())))
        except ValueError:
            v = self.cfg["lock"]["max_pages"]
        self.lock_depth_var.set(str(v))
        self.cfg["lock"]["max_pages"] = v
        save_config(self.cfg)

    def on_lock_quality(self, label):
        self.cfg["lock"]["quality"] = next(k for k, v in QUALITIES.items() if v == label)
        save_config(self.cfg)

    def on_lock_keep(self, value):
        self.cfg["lock"]["keep"] = int(value)
        save_config(self.cfg)
        threading.Thread(target=trim_lock_dl, args=(int(value),), daemon=True).start()

    def on_quality(self, label):
        self.cfg["quality"] = next(k for k, v in QUALITIES.items() if v == label)
        save_config(self.cfg)

    def on_keep(self, value):
        self.cfg["keep_recent"] = int(value)
        save_config(self.cfg)
        threading.Thread(target=trim_recent, args=(int(value),), daemon=True).start()
        self.hint.configure(text=self.hint_text())

    def on_autostart(self):
        self.cfg["autostart"] = bool(self.auto_var.get())
        save_config(self.cfg)
        try:
            set_autostart(self.cfg["autostart"])
        except Exception as e:  # noqa: BLE001
            self.say(f"Autostart error: {e}")

    # ------------------------------------------------------- worker + tray -- #
    def _loop(self):
        action = self._timer_action()
        log = lambda m: self.q.put(("status", m))  # noqa: E731
        while not self.stop.is_set():
            if action:
                with self.nav_lock:
                    try:
                        if action == "new":                  # e.g. mode switch
                            entry = change_cycle(self.cfg, log)
                            save_config(self.cfg)
                            self._sync_liked_last(entry)
                        else:                                # timer: replay history, then new
                            entry = self._advance(log)
                        self.q.put(("changed", entry))
                    except Exception as e:  # noqa: BLE001
                        log(f"Error: {e}")
            self.wake.clear()
            mins = max(1, int(self.cfg["intervals"].get(self.cfg["mode"], 30)))
            woke = self.wake.wait(mins * 60)
            if self.stop.is_set():
                break
            if woke:
                action, self.force = self.force, None        # only on_mode sets force ("new")
            else:
                action = self._timer_action()

    def _lock_loop(self):
        """Keeps the lock / sign-in screen image up to date (own timer, independent of the desktop)."""
        apply_now = self.cfg["lock"]["mode"] in ("liked", "auto", "fixed") and not self._lock_pinned()
        while not self.stop.is_set():
            lk = self.cfg["lock"]
            if apply_now and lk["mode"] != "off":
                try:
                    self.q.put(("lock_status", "Setting lock screen ..."))
                    with self.lock_nav:
                        msg = apply_lock_screen(self.cfg, lambda m: None)
                except Exception as e:  # noqa: BLE001
                    msg = f"Lock screen error: {e}"
                self.q.put(("lock_status", msg))
            timed = lk["mode"] in ("liked", "auto")
            woke = self.lock_wake.wait(int(lk.get("interval", 180)) * 60 if timed else None)
            self.lock_wake.clear()
            apply_now = bool(woke) or not self._lock_pinned()      # a timer tick never replaces a pinned one

    def _build_tray(self):
        pystray, q = self.pystray, self.q
        MI, M = pystray.MenuItem, pystray.Menu
        like_text = lambda item: ("♡  Unlike current" if (load_current() and is_liked(load_current()["id"]))  # noqa: E731
                                  else "♥  Like current")
        menu = M(
            MI("Open", lambda i, it: q.put(("show", None)), default=True),
            MI("Previous wallpaper", lambda i, it: q.put(("prev", None))),
            MI("Next wallpaper", lambda i, it: q.put(("next", None))),
            MI(like_text, lambda i, it: q.put(("like", None))),
            M.SEPARATOR,
            MI("Mode", M(
                MI("Auto (4kwallpapers)", lambda i, it: q.put(("mode", "auto")),
                   checked=lambda it: self.cfg["mode"] == "auto", radio=True),
                MI("Liked loop", lambda i, it: q.put(("mode", "liked")),
                   checked=lambda it: self.cfg["mode"] == "liked", radio=True))),
            MI("Pause auto change", lambda i, it: q.put(("pause", None)),
               checked=lambda it: self.paused),
            M.SEPARATOR,
            MI("Quit", lambda i, it: q.put(("quit", None))),
        )
        self.icon = pystray.Icon("4kwall", make_icon_image(), "4K Wallpaper Changer", menu)
        self.icon.run_detached()

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "status":
                    self.say(val)
                elif kind == "pin":
                    self.refresh_state_label()
                    if hasattr(self, "pin_btn"):
                        self.refresh_home()
                    if self.current_page == "Lock":
                        self.refresh_lock_preview()
                elif kind == "lock_status":
                    if hasattr(self, "lock_status"):
                        self.lock_status.configure(text=val)
                        if self.current_page == "Lock":
                            self.refresh_lock_preview()
                            if str(val).startswith("Lock screen:") and self.cfg["lock"]["mode"] == "auto":
                                self._fill_lock_recent()
                elif kind == "changed":
                    if self.cfg["lock"]["mode"] == "same" and not self._lock_pinned():
                        self.lock_wake.set()
                    if self.current_page == "Home":
                        self.refresh_home()
                    elif self.current_page == "Recent":
                        self.refresh_gallery("recent")
                elif kind == "thumb":
                    self._thumb_ready(*val)
                elif kind == "redl_prog":
                    self._set_dl_prog(*val)
                elif kind == "redl":
                    wid, msg = val
                    self.img_cache = {k: v for k, v in self.img_cache.items() if k[0] != wid}
                    self.say(msg)
                    self._dl_prog.pop(wid, None)
                    if self.current_page == "Home":
                        self.refresh_home()
                    elif self.current_page == "Liked":
                        if not self._refresh_card(wid):
                            self.refresh_gallery("liked")
                    elif self.current_page == "Lock":
                        if not self._refresh_card(wid, "lock"):
                            self._fill_lock_cards()
                elif kind == "links":
                    if self.current_page == "Liked":
                        self.refresh_gallery("liked")
                elif kind == "show":
                    self.show()
                elif kind == "next":
                    self.next_now()
                elif kind == "prev":
                    self.prev_now()
                elif kind == "like":
                    self.like_current()
                elif kind == "mode":
                    self.set_mode(val)
                elif kind == "pause":
                    self.toggle_pause()
                elif kind == "quit":
                    save_config(self.cfg)
                    self.stop.set()
                    self.wake.set()
                    self.lock_wake.set()
                    self.icon.stop()
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(200, self._poll)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="change wallpaper once and exit")
    ap.add_argument("--hidden", action="store_true", help="start in the tray without the window")
    ap.add_argument("--debug", metavar="URL", help="print download candidates of a wallpaper page")
    args = ap.parse_args()

    if args.debug:
        html = http_get(args.debug).text
        cands = parse_detail(html)
        print("title:", parse_title(html, args.debug))
        for c in cands:
            print(c)
        sw, sh = screen_size()
        for q in QUALITIES:
            print(f"quality {q}:", pick_image(html, sw, sh, {"quality": q, "min_width": 0}))
        print("chosen:", choose_candidate(cands, *screen_size()))
        return
    if args.once:
        cfg = load_config()
        change_cycle(cfg)
        save_config(cfg)
        return
    TrayApp(start_hidden=args.hidden)


if __name__ == "__main__":
    sys.exit(main())
