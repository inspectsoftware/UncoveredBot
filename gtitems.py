"""Growtopia item data: items.dat fetching/parsing + sprite textures.

Everything here is synchronous/blocking on purpose — bot.py always calls into
this module through asyncio.to_thread.

Data sources:
- items.dat comes from the community mirror (StileDevs/itemsdat-archive), which
  tracks the live game. The real client receives items.dat over ENet at runtime,
  so there is no official HTTP download for it.
- Sprite texture sheets (.rttex) are harvested, in order of preference, from:
  1. a local Growtopia client install (GT_LOCAL_CACHE),
  2. the game asset CDN when configured (GT_CDN_BASE),
  3. the official macOS client download (GT_CLIENT_URL) — auto-downloaded and
     extracted with 7-Zip, so headless hosts get sprites with zero setup
     beyond having a 7z binary available.
"""

import io
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from growtopia.rttex_converter import rttex_unpack
from PIL import Image

log = logging.getLogger("pricebot.gtitems")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "gtdata")
TEXTURES_DIR = os.path.join(DATA_DIR, "textures")
SPRITES_DIR = os.path.join(DATA_DIR, "sprites")
CLIENT_TMP_DIR = os.path.join(DATA_DIR, "client_tmp")
NEEDED_TEXTURES_FILE = os.path.join(DATA_DIR, "needed_textures.json")
HARVEST_MARKER_FILE = os.path.join(DATA_DIR, "client_harvest.json")

# Community mirror that tracks the live game's items.dat.
ITEMS_MIRROR_BASE = os.getenv(
    "ITEMS_MIRROR_BASE",
    "https://raw.githubusercontent.com/StileDevs/itemsdat-archive/main",
)
# Base URL of the game asset CDN, e.g. "https://ubistatic-a.akamaihd.net/0098/<build>/cache".
# The <build> slug changes per client release and is only embedded in the client,
# so this stays configurable instead of hardcoded.
GT_CDN_BASE = os.getenv("GT_CDN_BASE", "").rstrip("/")
# Root folder of an installed Growtopia client, the most reliable texture source.
# Base sheets live in game/, live-updated deltas in cache/game/; the recursive
# index prefers cache/ (walked first) so patched sheets win. Defaults to the
# standard Windows install location only when LOCALAPPDATA actually exists.
GT_LOCAL_CACHE = os.getenv("GT_LOCAL_CACHE") or (
    os.path.join(os.environ["LOCALAPPDATA"], "Growtopia") if os.getenv("LOCALAPPDATA") else ""
)
# Official client download used as the texture source of last resort. The macOS
# build is a plain disk image whose Resources/game folder holds every sheet,
# and 7-Zip can extract it on any platform.
GT_CLIENT_URL = os.getenv("GT_CLIENT_URL", "https://growtopiagame.com/Growtopia-mac.dmg")

HTTP_TIMEOUT = 30
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024  # sanity cap for any single small download
MAX_CLIENT_BYTES = 2 * 1024 * 1024 * 1024  # cap for the client disk image
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

XOR_KEY = "PBG892FXX982ABC*"

RARITY_NONE = 999
CLOTHING_TYPE = 20

ITEM_TYPE_NAMES = {
    0: "Fist", 1: "Wrench", 2: "Door", 3: "Lock", 4: "Gems", 5: "Treasure",
    6: "Deadly Block", 7: "Trampoline", 8: "Consumable", 9: "Entrance",
    10: "Sign", 11: "SFX Block", 12: "Toggleable Foreground", 13: "Main Door",
    14: "Platform", 15: "Bedrock", 16: "Pain Block (Lava)", 17: "Foreground Block",
    18: "Background Block", 19: "Seed", 20: "Clothing", 21: "Animated Foreground",
    22: "SFX Foreground", 23: "Portal", 24: "Checkpoint", 25: "Sheet Music",
    26: "Slippery Block", 28: "Water", 29: "Weather Machine", 30: "Ignitable",
    31: "Pain Block (Spike)", 32: "Chemical", 33: "Vending Machine", 34: "Untradable Item",
}

CLOTHING_SLOT_NAMES = {
    0: "Hat", 1: "Shirt", 2: "Pants", 3: "Feet", 4: "Face",
    5: "Hand", 6: "Back", 7: "Hair", 8: "Chest",
}


class SpriteUnavailable(Exception):
    """Raised when an item's sprite can't be produced (no texture, bad coords...)."""


def item_type_name(item_type: int) -> str:
    return ITEM_TYPE_NAMES.get(item_type, f"Type {item_type}")


def clothing_slot_name(clothing_type: int) -> str:
    return CLOTHING_SLOT_NAMES.get(clothing_type, f"Slot {clothing_type}")


# --- HTTP helpers ---

def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "UncoveredBot/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"Download exceeded {MAX_DOWNLOAD_BYTES} bytes: {url}")
    return data


def _download_to_file(url: str, dest: str, max_bytes: int) -> int:
    """Stream a large download to disk. Returns the byte count."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    total = 0
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Download exceeded {max_bytes} bytes: {url}")
            f.write(chunk)
    return total


# --- items.dat fetching ---

def fetch_latest_filename() -> str:
    """Name of the newest items.dat on the mirror, e.g. 'items-v5.47.dat'."""
    data = json.loads(_http_get(f"{ITEMS_MIRROR_BASE}/latest.json"))
    name = data.get("content")
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]+\.dat", name):
        raise ValueError(f"Mirror returned an unexpected latest.json: {data!r}")
    return name


def download_items_dat(filename: str) -> bytes:
    raw = _http_get(f"{ITEMS_MIRROR_BASE}/{urllib.parse.quote(filename)}")
    if len(raw) < 6:
        raise ValueError("Downloaded items.dat is implausibly small")
    log.info(f"Downloaded {filename} ({len(raw) / 1024:.0f} KB) from mirror")
    return raw


# --- items.dat parsing ---
# Field layout ported from gt-itemsdat-json (via growtopia-api). Each entry:
# (key, size, min_format_version). size -1 = length-prefixed string,
# size -2 = XOR-encrypted string. Keys starting with "_" are unknown padding.

_STRING = -1
_STRING_XOR = -2

_FIELDS: list[tuple[str, int, int]] = [
    ("id", 4, 0), ("properties", 2, 0), ("type", 1, 0), ("material", 1, 0),
    ("name", _STRING_XOR, 0), ("file_name", _STRING, 0), ("file_hash", 4, 0),
    ("visual_type", 1, 0), ("cook_time", 4, 0), ("tex_x", 1, 0), ("tex_y", 1, 0),
    ("storage_type", 1, 0), ("layer", 1, 0), ("collision_type", 1, 0),
    ("hardness", 1, 0), ("regen_time", 4, 0), ("clothing_type", 1, 0),
    ("rarity", 2, 0), ("max_hold", 1, 0), ("alt_file_path", _STRING, 0),
    ("alt_file_hash", 4, 0), ("anim_ms", 4, 0),
    ("pet_name", _STRING, 4), ("pet_prefix", _STRING, 4), ("pet_suffix", _STRING, 4),
    ("pet_ability", _STRING, 5),
    ("seed_base", 1, 0), ("seed_over", 1, 0), ("tree_base", 1, 0), ("tree_over", 1, 0),
    ("bg_col", 4, 0), ("fg_col", 4, 0), ("seed1", 2, 0), ("seed2", 2, 0),
    ("bloom_time", 4, 0),
    ("anim_type", 4, 7), ("anim_string", _STRING, 7),
    ("anim_tex", _STRING, 8), ("anim_string2", _STRING, 8),
    ("dlayer1", 4, 8), ("dlayer2", 4, 8),
    ("properties2", 2, 9), ("_unk", 62, 9),
    ("tile_range", 4, 10), ("pile_range", 4, 10),
    ("custom_punch", _STRING, 11),
    ("_unk2", 13, 12),
    ("clock_div", 4, 13),
    ("parent_id", 4, 14),
    ("_unk3", 25, 15), ("alt_sit_path", _STRING, 15),
    ("_unk4", _STRING, 16),
    ("_unk5", 4, 17), ("_unk6", 4, 18), ("_unk7", 9, 19),
    ("_unk8", 2, 21),
    # v22+ layout confirmed against StileDevs/grow-items (Items.ts).
    ("description", _STRING, 22),   # "info" — the in-game item description
    ("_recipe", 4, 23),             # 2x u16 recipe slots
    ("_unk9", 1, 24),
    ("_hit_sound", _STRING, 25), ("_hit_sound_hash", 4, 25),
    ("_unk10", 1, 26),
]
MAX_KNOWN_FORMAT = max(v for _, _, v in _FIELDS)


def _decrypt_name(raw: bytes, item_id: int) -> str:
    return "".join(
        chr(b ^ ord(XOR_KEY[(i + item_id) % len(XOR_KEY)])) for i, b in enumerate(raw)
    )


def parse_items_dat(raw: bytes) -> dict:
    """Parse items.dat into {'version', 'item_count', 'items': [dict, ...]}."""
    buf = io.BytesIO(raw)

    def num(size: int) -> int:
        return int.from_bytes(buf.read(size), "little")

    def string() -> bytes:
        return buf.read(num(2))

    version = num(2)
    item_count = num(4)
    if version > MAX_KNOWN_FORMAT:
        log.warning(
            f"items.dat format v{version} is newer than the parser knows "
            f"(v{MAX_KNOWN_FORMAT}) — attempting to parse anyway."
        )

    items = []
    for i in range(item_count):
        item: dict = {}
        for key, size, min_version in _FIELDS:
            if min_version > version:
                continue
            if key == "id":
                item["id"] = num(4)
                if item["id"] != i:
                    raise ValueError(
                        f"Item ID mismatch at #{i} (got {item['id']}) — the parser "
                        f"is out of date for items.dat format v{version}."
                    )
            elif size == _STRING_XOR:
                raw_name = string()
                item[key] = _decrypt_name(raw_name, item["id"]) if version >= 3 else raw_name.decode("utf-8", "replace")
            elif size == _STRING:
                value = string().decode("utf-8", "replace")
                if not key.startswith("_"):
                    item[key] = value
            else:
                value = num(size)
                if not key.startswith("_"):
                    item[key] = value
        items.append(item)

    leftover = len(raw) - buf.tell()
    if leftover:
        log.warning(f"items.dat parse finished with {leftover} unread bytes (format v{version}).")

    # Item descriptions embed Growtopia color codes (`2...`` etc.) — strip for display.
    for it in items:
        if it.get("description"):
            it["description"] = re.sub(r"`.?", "", it["description"]).strip()

    _save_needed_textures(items)
    return {"version": version, "item_count": item_count, "items": items}


# --- Sprite textures ---

_SAFE_TEXTURE_RE = re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9_\-./]*\.rttex")


def _safe_texture_name(file_name: str) -> str | None:
    """Validated CDN-relative texture path (no traversal), or None if unusable."""
    if not file_name or not _SAFE_TEXTURE_RE.fullmatch(file_name) or ".." in file_name:
        return None
    return file_name


def _texture_path(file_name: str) -> str:
    # Sheets are stored flat by basename — names are unique in practice.
    return os.path.join(TEXTURES_DIR, os.path.basename(file_name))


def _save_needed_textures(items: list[dict]) -> None:
    """Remember which sheets the current items.dat references, for ensure_textures()."""
    needed = sorted({
        name for it in items if (name := _safe_texture_name(it.get("file_name", "")))
    })
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NEEDED_TEXTURES_FILE, "w", encoding="utf-8") as f:
        json.dump(needed, f)


def _needed_textures() -> list[str]:
    try:
        with open(NEEDED_TEXTURES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _local_cache_index() -> dict[str, str]:
    """basename -> full path of every .rttex inside the installed client cache."""
    index: dict[str, str] = {}
    if GT_LOCAL_CACHE and os.path.isdir(GT_LOCAL_CACHE):
        for root, _, files in os.walk(GT_LOCAL_CACHE):
            for name in files:
                if name.endswith(".rttex"):
                    index.setdefault(name, os.path.join(root, name))
    return index


def _fetch_texture(file_name: str, local_index: dict[str, str] | None = None) -> bool:
    """Try to obtain one texture sheet (local client cache first, then CDN)."""
    safe = _safe_texture_name(file_name)
    if safe is None:
        return False
    dest = _texture_path(safe)
    os.makedirs(TEXTURES_DIR, exist_ok=True)

    # 1) Installed Growtopia client cache on this machine. items.dat references
    # bare names (tiles_page1.rttex) while the cache nests them (game\...), so
    # look the basename up in a recursive index.
    if local_index is None:
        local_index = _local_cache_index()
    local = local_index.get(os.path.basename(safe))
    if local:
        shutil.copyfile(local, dest)
        return True

    # 2) Game asset CDN, when configured. Sheets usually live under game/.
    if GT_CDN_BASE:
        candidates = [safe] if "/" in safe else [f"game/{safe}", safe]
        for rel in candidates:
            try:
                data = _http_get(f"{GT_CDN_BASE}/{rel}")
            except (urllib.error.URLError, ValueError) as e:
                log.info(f"CDN fetch failed for {rel}: {e}")
                continue
            if data[:6] in (b"RTPACK", b"RTTXTR"):
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            log.warning(f"CDN returned non-RTTEX data for {rel}")
    return False


# --- Texture source of last resort: the official client download ---

def _find_7z() -> str | None:
    for name in ("7z", "7zz", "7za"):
        path = shutil.which(name)
        if path:
            return path
    if os.name == "nt":
        # growtopia-api ships a bundled 7z.exe for its dataminer — reuse it.
        try:
            import growtopia
            bundled = os.path.join(os.path.dirname(growtopia.__file__), "dataminer", "bin", "7z.exe")
            if os.path.isfile(bundled):
                return bundled
        except ImportError:
            pass
    return None


def _harvest_already_attempted(missing: list[str]) -> bool:
    """True when a client harvest was already tried for exactly this missing set,
    so the (large) client download isn't repeated for permanently absent sheets."""
    try:
        with open(HARVEST_MARKER_FILE, encoding="utf-8") as f:
            return json.load(f).get("missing") == sorted(missing)
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def _record_harvest_attempt(missing: list[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HARVEST_MARKER_FILE, "w", encoding="utf-8") as f:
        json.dump({"missing": sorted(missing)}, f)


def _harvest_from_client(missing: list[str]) -> int:
    """Download the official client and pull every .rttex sheet out of it.

    Copies ALL sheets found (not just the currently missing ones) into
    TEXTURES_DIR so future items.dat updates don't need another download.
    Returns how many of the missing sheets were recovered.
    """
    exe = _find_7z()
    if exe is None:
        log.warning(
            "Can't harvest textures from the Growtopia client: no 7-Zip binary "
            "found. Install 7-Zip/p7zip (7z on PATH) to enable automatic "
            "client texture downloads."
        )
        return 0

    shutil.rmtree(CLIENT_TMP_DIR, ignore_errors=True)
    os.makedirs(CLIENT_TMP_DIR, exist_ok=True)
    dmg = os.path.join(CLIENT_TMP_DIR, "Growtopia.dmg")
    try:
        log.info(f"Downloading the Growtopia client to harvest {len(missing)} texture sheets…")
        size = _download_to_file(GT_CLIENT_URL, dmg, MAX_CLIENT_BYTES)
        log.info(f"Client downloaded ({size / 1024 / 1024:.0f} MB) — extracting textures…")

        out_dir = os.path.join(CLIENT_TMP_DIR, "extracted")
        proc = subprocess.run(
            [exe, "x", dmg, f"-o{out_dir}", "*.rttex", "-r", "-aoa", "-y"],
            capture_output=True, text=True, timeout=1800,
        )
        if proc.returncode not in (0, 1):  # 1 = extracted with warnings
            log.warning(f"7-Zip exited with code {proc.returncode}: {(proc.stderr or '')[-400:]}")

        os.makedirs(TEXTURES_DIR, exist_ok=True)
        harvested = 0
        seen: set[str] = set()
        for root, _, files in os.walk(out_dir):
            for name in files:
                if name.endswith(".rttex") and name not in seen:
                    seen.add(name)
                    shutil.copyfile(os.path.join(root, name), os.path.join(TEXTURES_DIR, name))
                    harvested += 1
        recovered = sum(1 for n in missing if os.path.isfile(_texture_path(n)))
        log.info(
            f"Client harvest done: {harvested} sheets extracted, "
            f"{recovered}/{len(missing)} missing sheets recovered."
        )
        return recovered
    finally:
        shutil.rmtree(CLIENT_TMP_DIR, ignore_errors=True)


def ensure_textures(force: bool = False) -> int:
    """Make sure every referenced texture sheet is present locally.

    Tries the local client install and the CDN per sheet; if sheets are still
    missing, downloads the official client once and extracts everything from it.
    Returns the number of sheets available afterwards. Missing sheets are not an
    error, sprites for them will simply be unavailable.
    """
    needed = _needed_textures()
    if not needed:
        return texture_count()

    local_index = _local_cache_index()
    missing = []
    for name in needed:
        if force or not os.path.isfile(_texture_path(name)):
            if not _fetch_texture(name, local_index):
                missing.append(name)

    if missing and (force or not _harvest_already_attempted(missing)):
        try:
            _harvest_from_client(missing)
        except Exception as e:
            log.warning(f"Client texture harvest failed: {e}")
        _record_harvest_attempt(missing)
        missing = [n for n in missing if not os.path.isfile(_texture_path(n))]

    if missing:
        log.warning(
            f"{len(missing)}/{len(needed)} texture sheets unavailable after trying "
            f"local install ('{GT_LOCAL_CACHE or 'not set'}'), "
            f"CDN ({GT_CDN_BASE or 'not set'}) and the client download. "
            "Sprites for those items will be missing."
        )
    return texture_count()


def textures_present() -> bool:
    return texture_count() > 0


def texture_count() -> int:
    try:
        return sum(1 for n in os.listdir(TEXTURES_DIR) if n.endswith(".rttex"))
    except FileNotFoundError:
        return 0


def sprite_cache_stats() -> tuple[int, int]:
    """(number of cached sprite PNGs, total size in bytes)."""
    try:
        entries = [os.path.join(SPRITES_DIR, n) for n in os.listdir(SPRITES_DIR) if n.endswith(".png")]
    except FileNotFoundError:
        return 0, 0
    return len(entries), sum(os.path.getsize(p) for p in entries)


def sprite_png(file_name: str, tex_x: int, tex_y: int, scale: int = 1) -> bytes:
    """Cropped (and optionally upscaled) 32x32 item sprite as PNG bytes."""
    safe = _safe_texture_name(file_name or "")
    if safe is None:
        raise SpriteUnavailable("Item has no usable texture reference")

    cache_key = f"{os.path.basename(safe).removesuffix('.rttex')}_{tex_x}_{tex_y}_{scale}.png"
    cache_path = os.path.join(SPRITES_DIR, cache_key)
    if os.path.isfile(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    sheet_path = _texture_path(safe)
    if not os.path.isfile(sheet_path) and not _fetch_texture(safe):
        raise SpriteUnavailable(f"Texture sheet {safe} is not available")

    with open(sheet_path, "rb") as f:
        sheet = f.read()
    unpacked = rttex_unpack(sheet, x=tex_x, y=tex_y)
    if unpacked is None:
        raise SpriteUnavailable(f"{safe} is not a valid RTTEX file")

    img = Image.open(unpacked)
    if img.getbbox() is None:
        raise SpriteUnavailable("Sprite tile is empty (coords outside the sheet?)")
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    out = io.BytesIO()
    img.save(out, format="PNG")
    png = out.getvalue()

    os.makedirs(SPRITES_DIR, exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(png)
    return png
