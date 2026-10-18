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
from pathlib import Path
from urllib.parse import urljoin

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
FEEDS = {
    "Recent (all)": "/",
    "Popular": "/most-popular-4k-wallpapers/",
    "Featured": "/best-4k-wallpapers/",
    "Random": "/random-wallpapers/",
}

QUALITIES = {"low": "Low", "4k": "4K", "high": "High"}

DEFAULT_CONFIG = {
    "mode": "auto",                      # auto | liked
    "source": "Selected categories",     # or a FEEDS key
    "categories": ["nature", "space", "abstract"],
    "intervals": {"auto": 30, "liked": 10},   # minutes, per mode
    "max_pages": 15,
    "min_width": 0,                      # 0 = screen width (used by "low" quality)
    "quality": "low",                    # low = smallest size covering the screen | 4k | high = original
    "keep_recent": 40,                   # 20 or 40
    "request_delay": 1.0,
    "auto": True,                        # False = paused
    "autostart": False,
    "liked_last": None,
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
    cfg.update(saved)
    cfg["intervals"].update(iv)
    if cfg["mode"] not in ("auto", "liked"):
        cfg["mode"] = "auto"
    if cfg["keep_recent"] not in (20, 40):
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
        if not urls:
            continue
        random.shuffle(urls)
        for page_url in urls[:3]:
            detail = http_get(page_url, delay).text
            best = pick_image(detail, screen_w, screen_h, cfg)
            if best:
                return page_url, best[2], parse_title(detail, page_url)
    raise RuntimeError("No suitable wallpaper found (run with --debug to check the page).")


def download(img_url, referer, delay=1.0, dest_dir=RECENT_DIR):
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = img_url.split("?")[0].rsplit("/", 1)[-1]
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    r = http_get(img_url, delay, headers={"Referer": referer}, stream=True, timeout=120)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
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


# --------------------------------------------------------------------------- #
# set wallpaper (per OS)
# --------------------------------------------------------------------------- #
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

    def _press(self, _e):
        self.state, self.t0 = "pressed", time.time()
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
                self._draw(flash=True)
                self.c.after(220, lambda: self.state == "idle" and self._draw())
                self.on_hold()
                return
        self.job = self.c.after(16, self._tick)

    def _release(self, _e):
        st, self.state = self.state, "idle"
        self._draw()
        if st == "pressed":
            self.on_click()

    def _leave(self, _e):
        self.hover = False
        if self.state == "pressed":
            self.state = "idle"            # dragged off the button: cancel
        if self.state == "idle":
            self._draw()


class TrayApp:
    PAGES = ["Home", "Liked", "Recent", "Settings"]

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
        self.paused = not self.cfg.get("auto", True)
        self.img_cache = {}
        self.current_page = "Home"

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
        self.pages["Settings"] = self._build_settings(self.container)

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.after(300, self._set_window_icon)

        import_liked_folder()
        self._resolving = False
        self.show_page("Home")
        self.refresh_home()
        self._build_tray()
        self.start_link_resolver()
        threading.Thread(target=self._loop, daemon=True).start()
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
        self.now_title = ctk.CTkLabel(card, text="", font=self.f_title, text_color=TEXT,
                                      anchor="w", justify="left", wraplength=460)
        self.now_title.pack(fill="x", padx=18)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(8, 14))
        self._btn(row, "⏮  Prev", self.prev_now, width=88).pack(side="left")
        self.like_btn = self._btn(row, "♡  Like", self.like_current, width=100)
        self.like_btn.pack(side="left", padx=6)
        self.next_btn = HoldButton(row, "Next  ⏭", self.next_now, self.latest_now,
                                   self._scale(), SURFACE).pack(side="left")
        self._btn(row, "Open page", self.open_current_page, width=100).pack(side="left", padx=6)

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
        if kind == "liked":
            self._btn(head, "None", lambda: self.select_all(False), width=60, height=28).pack(side="right")
            self._btn(head, "📁", lambda: open_folder(LIKED_DIR), width=40, height=28).pack(side="right", padx=(0, 6))
            self._btn(head, "All", lambda: self.select_all(True), width=50, height=28).pack(side="right", padx=6)
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

    def refresh_gallery(self, kind):
        ctk = self.ctk
        holder = getattr(self, f"{kind}_holder")
        info = getattr(self, f"{kind}_info")
        for w in holder.winfo_children():
            w.destroy()
        if kind == "liked":
            import_liked_folder()
            self.start_link_resolver()
            entries = list(reversed(load_likes()))
            n_sel = sum(1 for e in entries if e.get("selected"))
            info.configure(text=f"{len(entries)} liked · {n_sel} ticked for the loop "
                                "· drop your own images into the liked folder to add them")
            empty = ("Nothing liked yet.\nPress ♡ Like on the Home tab (or the tray menu),\n"
                     "or drop images into the liked folder.")
        else:
            entries = list(reversed(load_recent()))
            info.configure(text=f"Newest {self.cfg['keep_recent']} wallpapers are kept on disk. "
                                "Auto mode loops through them when you're offline.")
            empty = "No saved wallpapers yet."
        if not entries:
            ctk.CTkLabel(holder, text=empty, font=self.f_body, text_color=MUTED,
                         justify="center").grid(row=0, column=0, columnspan=2, pady=60)
            return
        liked_ids = {l["id"] for l in load_likes()}
        for i, e in enumerate(entries):
            card = ctk.CTkFrame(holder, fg_color=SURFACE, corner_radius=16)
            card.grid(row=i // 2, column=i % 2, padx=5, pady=5, sticky="n")
            img = self.thumb_image(e, (212, 119))
            ctk.CTkLabel(card, text="" if img else "no preview", image=img, width=212, height=119,
                         text_color=MUTED).pack(padx=10, pady=(10, 6))
            ctk.CTkLabel(card, text=short(e["title"], 30), font=self.f_small, text_color=TEXT,
                         anchor="w").pack(fill="x", padx=12)
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=(6, 10))
            if kind == "liked":
                var = ctk.BooleanVar(value=bool(e.get("selected", True)))
                ctk.CTkCheckBox(row, text="Loop", variable=var, width=62, checkbox_width=20,
                                checkbox_height=20, corner_radius=6, border_width=2,
                                fg_color=ACCENT, hover_color=ACCENT_HOVER, checkmark_color=ACCENT_TEXT,
                                border_color=MUTED, text_color=TEXT, font=self.f_small,
                                command=lambda wid=e["id"], v=var: self.on_select(wid, v.get())
                                ).pack(side="left")
                heart = self._btn(row, "♥", lambda wid=e["id"]: self.unlike(wid), width=34, height=28,
                                  text_color=ACCENT)
                heart.pack(side="right")
            else:
                heart = self._btn(row, "♥" if e["id"] in liked_ids else "♡", None, width=34, height=28,
                                  text_color=ACCENT if e["id"] in liked_ids else TEXT)
                heart.configure(command=lambda en=e, b=heart: self.toggle_from_recent(en, b))
                heart.pack(side="left")
            self._btn(row, "↗", lambda en=e: self.open_page(en), width=34, height=28
                      ).pack(side="right", padx=4)
            self._btn(row, "Set", lambda en=e: self.set_entry(en), accent=True, width=46, height=28
                      ).pack(side="right")

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
        ctk.CTkOptionMenu(f, values=["Selected categories"] + list(FEEDS), variable=self.source_var,
                          command=self.on_source, fg_color=SURFACE, button_color=SURFACE2,
                          button_hover_color=BORDER, dropdown_fg_color=SURFACE,
                          dropdown_hover_color=SURFACE2, text_color=TEXT, font=self.f_body,
                          dropdown_font=self.f_body, width=260, height=34).pack(anchor="w", padx=8)

        self._section(f, "Categories")
        grid = ctk.CTkFrame(f, fg_color=SURFACE, corner_radius=16)
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
            f, values=["20", "40"], command=self.on_keep, height=34, font=self.f_body,
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
        self.root.withdraw()

    # ------------------------------------------------------------ actions -- #
    def say(self, msg):
        self.status.configure(text=str(msg)[:160])
        try:
            self.icon.title = ("4K Wallpaper: " + str(msg))[:60]
        except Exception:
            pass

    def hint_text(self):
        if self.cfg["mode"] == "auto":
            src = (f"{len(self.cfg['categories'])} categories" if self.cfg["source"] == "Selected categories"
                   else self.cfg["source"])
            return (f"New random wallpaper from {src}. Offline: loops your last "
                    f"{self.cfg['keep_recent']} saved ones.")
        n = sum(1 for l in load_likes() if l.get("selected"))
        return f"Loops {n} ticked liked wallpaper(s) in order. Saved on disk, works offline."

    def refresh_state_label(self):
        mode = "Liked loop" if self.cfg["mode"] == "liked" else "Auto"
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
        self.refresh_gallery("liked")
        self.refresh_home()

    def toggle_from_recent(self, entry, btn):
        liked = toggle_like(entry)
        btn.configure(text="♥" if liked else "♡", text_color=ACCENT if liked else TEXT)
        self.refresh_home()

    def on_select(self, wid, val):
        set_selected(wid, val)
        n = sum(1 for l in load_likes() if l.get("selected"))
        self.liked_info.configure(text=f"{len(load_likes())} liked · {n} ticked for the loop "
                                       "· files are saved on disk, works offline")
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

    def on_source(self, value):
        self.cfg["source"] = value
        save_config(self.cfg)
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
        action = "advance" if not self.paused else None
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
                action = "advance" if not self.paused else None

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
                elif kind == "changed":
                    if self.current_page == "Home":
                        self.refresh_home()
                    elif self.current_page == "Recent":
                        self.refresh_gallery("recent")
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
