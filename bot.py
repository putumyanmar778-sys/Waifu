#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Husbandos & Waifus Bot (single-file, Termux-friendly)
=====================================================
A Telegram anime-character bot inspired by public cheat-sheet bots.

- Character database stored in a Telegram Channel (posts + pinned index)
  with a local JSON cache (data/). No external database server needed.
- Prefix commands (.wa, .op) AND slash commands (/wa, /op) share one
  dispatcher.
- Optional "Assistant Account": a regular Telegram USER account logged in
  with a phone number (Telethon / MTProto). It is used ONLY to scan
  channels the account itself joined and to enable channel-history
  recovery. It never sends messages on its own.
- Image matching: perceptual difference-hash (dHash) implemented with
  Pillow only (no numpy / opencv / torch -> Termux-safe).
- Image similarity alone can NEVER prove a character name. A name is
  shown only when a matching channel post also has a readable caption.

Run:  python bot.py
"""

import asyncio
import base64
import hashlib
import hmac
import inspect
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    from PIL import Image
    PIL_OK = True
except ImportError:  # pragma: no cover
    PIL_OK = False

try:  # optional: better perceptual matching when the package is present
    import imagehash
    IMAGEHASH_OK = True
except ImportError:  # pragma: no cover
    IMAGEHASH_OK = False

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

try:  # Bot API 7.11 copy-to-clipboard buttons (python-telegram-bot >= 21.4)
    from telegram import CopyTextButton
    _COPY_TEXT_CLASS = True
except ImportError:  # pragma: no cover
    CopyTextButton = None
    _COPY_TEXT_CLASS = False

try:
    COPY_TEXT_OK = _COPY_TEXT_CLASS and "copy_text" in inspect.signature(
        InlineKeyboardButton.__init__).parameters
except Exception:  # pragma: no cover
    COPY_TEXT_OK = False
from telegram.ext import (
    CommandHandler,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon import errors as tl_errors
    TELETHON_OK = True
except ImportError:  # pragma: no cover
    TELETHON_OK = False

# ----------------------------------------------------------------------------
# 1. CONFIG (.env)  --  BOT_TOKEN / OWNER_ID / API_ID / API_HASH / DB_CHANNEL_ID
# ----------------------------------------------------------------------------

def load_env(path: str = ".env") -> dict:
    """Tiny .env parser (no external dependency). Real env vars win."""
    env = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key:
                        env[key] = val
        except OSError:
            pass
    for key in ("BOT_TOKEN", "OWNER_ID", "API_ID", "API_HASH", "DB_CHANNEL_ID"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


ENV = load_env()
TOKEN = ENV.get("BOT_TOKEN", "")
OWNER_ID = int(ENV["OWNER_ID"]) if ENV.get("OWNER_ID", "").strip().lstrip("-").isdigit() else 0
API_ID = int(ENV["API_ID"]) if ENV.get("API_ID", "").strip().isdigit() else 0
API_HASH = ENV.get("API_HASH", "")
def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


ENV_DB_CHANNEL = _to_int(ENV.get("DB_CHANNEL_ID"))

# ----------------------------------------------------------------------------
# 2. PATHS + LOCAL CACHE (the working database; the channel is the backup)
# ----------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

F_CHARS = os.path.join(DATA_DIR, "chars.json")          # char_id -> record
F_USERS = os.path.join(DATA_DIR, "users.json")          # uid -> profile
F_HAREM = os.path.join(DATA_DIR, "harem.json")          # uid -> [char_id]
F_FAVS = os.path.join(DATA_DIR, "favs.json")            # uid -> [char_id]
F_CHATS = os.path.join(DATA_DIR, "chats.json")          # chat_id -> members (for /ctop)
F_CFG = os.path.join(DATA_DIR, "config.json")           # {"db_channel_id": ...}
F_IMGIDX = os.path.join(DATA_DIR, "image_index.json")   # visual index from channels
F_SESSION = os.path.join(DATA_DIR, "ass_session.txt")   # Telethon StringSession (SECRET)
F_RECOVERY_LOG = os.path.join(DATA_DIR, "recovery_log.txt")
F_CHANNELS = os.path.join(DATA_DIR, "channels.json")    # search-scope channel list
F_COPYCACHE = os.path.join(DATA_DIR, "copy_cache.json")     # fallback copy-button payloads
F_BACKUP_STATE = os.path.join(DATA_DIR, "backup_state.json")  # last backup metadata
F_BACKUP_KEY = os.path.join(DATA_DIR, "backup.key")        # key for session encryption
F_BACKUP_DIR = os.path.join(DATA_DIR, "backups")           # local backup files
F_SNAPSHOT = os.path.join(DATA_DIR, "pre_restore_snapshot.json")
os.makedirs(F_BACKUP_DIR, exist_ok=True)
BACKUP_VERSION = 2


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        LOG.error("Failed to save %s: %s", path, exc)


# ---- in-memory state --------------------------------------------------------
CHARS = {}        # char_id -> record dict
CHAR_MSGID = {}   # char_id -> channel message id (of its #HWDATA post)
INDEX_MSGID = None
USERS = {}        # str(uid) -> {"name","first_seen","count"}
HAREM = {}        # str(uid) -> [char_id]
FAVS = {}
CHATS = {}        # str(chat_id) -> {"title", "members": {str(uid): name}}
RUN_CFG = {}      # loaded from F_CFG
IMG_INDEX = {"entries": [], "last_id": {}}   # entries: {c,m,h,cap,t}
SEARCH_CHANNELS = []  # [{"chat_id" or "username", "title"}]
ASS_CLIENT = None       # Telethon client (assistant user account) or None
ASS_PHONE = None
COPY_CACHE = {}         # token -> text (fallback copy buttons)
BACKUP_STATE = {}       # last backup metadata
_COPY_SEQ = 0


def load_all() -> None:
    global CHARS, CHAR_MSGID, INDEX_MSGID, USERS, HAREM, FAVS, CHATS, RUN_CFG, IMG_INDEX, SEARCH_CHANNELS, COPY_CACHE, BACKUP_STATE
    CHARS = load_json(F_CHARS, {})
    users_db = load_json(F_USERS, {})
    USERS = users_db.get("users", {})
    INDEX_MSGID = users_db.get("index_msgid")
    CHAR_MSGID = users_db.get("char_msgid", {})
    HAREM = load_json(F_HAREM, {})
    FAVS = load_json(F_FAVS, {})
    CHATS = load_json(F_CHATS, {})
    RUN_CFG = load_json(F_CFG, {})
    IMG_INDEX = load_json(F_IMGIDX, {"entries": [], "last_id": {}})
    if "entries" not in IMG_INDEX or "last_id" not in IMG_INDEX:
        IMG_INDEX = {"entries": [], "last_id": {}}
    SEARCH_CHANNELS = load_json(F_CHANNELS, [])
    COPY_CACHE = load_json(F_COPYCACHE, {})
    BACKUP_STATE = load_json(F_BACKUP_STATE, {})


def save_users_db() -> None:
    save_json(F_USERS, {"users": USERS, "index_msgid": INDEX_MSGID, "char_msgid": CHAR_MSGID})


def save_chars() -> None:
    save_json(F_CHARS, CHARS)


# ----------------------------------------------------------------------------
# 3. SMALL HELPERS
# ----------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
LOG = logging.getLogger("huswabot")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


class NoTokenFilter(logging.Filter):
    """Never let the bot token leak into logs."""

    def filter(self, record):
        try:
            msg = record.getMessage()
            if TOKEN and TOKEN in msg:
                record.msg = msg.replace(TOKEN, "***TOKEN***")
                record.args = ()
        except Exception:
            pass
        return True


LOG.addFilter(NoTokenFilter())


def is_owner(uid) -> bool:
    try:
        return int(uid) == OWNER_ID and OWNER_ID != 0
    except (TypeError, ValueError):
        return False


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def get_db_channel(context: ContextTypes.DEFAULT_TYPE):
    """DB channel id: data/config.json overrides .env."""
    cid = RUN_CFG.get("db_channel_id") or ENV_DB_CHANNEL
    if cid:
        try:
            return int(cid)
        except (TypeError, ValueError):
            return None
    return None


def safe_text(value, limit=64):
    """Escape HTML specials for user-provided strings."""
    text = str(value or "").strip()
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text[:limit]


# ---- rate limiting ----------------------------------------------------------
_LAST_CMD = {}
_LAST_WARN = {}


def rate_ok(uid, min_gap=1.2) -> bool:
    uid = str(uid)
    now = time.time()
    if now - _LAST_CMD.get(uid, 0) < min_gap:
        if now - _LAST_WARN.get(uid, 0) > 5.0:
            _LAST_WARN[uid] = now
            return False
        return False
    _LAST_CMD[uid] = now
    return True


# ----------------------------------------------------------------------------
# 4. CHARACTER DATABASE LOGIC
# ----------------------------------------------------------------------------

def new_char_id() -> str:
    n = 1
    for cid in CHARS:
        m = re.match(r"^C(\d+)$", cid)
        if m:
            n = max(n, int(m.group(1)) + 1)
    return f"C{n:05d}"


def char_names(rec) -> list:
    names = [rec.get("name", "")]
    names.extend(rec.get("aliases", []))
    return [n for n in names if n]


def find_characters(query: str):
    """Returns (exact_matches, partial_matches). Case-insensitive, alias-aware."""
    q = normalize(query)
    if not q:
        return [], []
    exact, partial = [], []
    for rec in CHARS.values():
        norms = [normalize(n) for n in char_names(rec)]
        if q in norms:
            exact.append(rec)
        elif any(q in n for n in norms):
            partial.append(rec)
    exact.sort(key=lambda r: r.get("name", "").lower())
    partial.sort(key=lambda r: r.get("name", "").lower())
    return exact, partial


def parse_addchar_args(args_text: str):
    """Expected: Name | Anime | alias,alias | category | rarity"""
    parts = [p.strip() for p in args_text.split("|")]
    if not parts or not parts[0]:
        return None
    rec = {
        "name": parts[0][:100],
        "anime": parts[1][:100] if len(parts) > 1 and parts[1] else "",
        "aliases": [],
        "category": parts[3][:40] if len(parts) > 3 and parts[3] else "",
        "rarity": parts[4][:20] if len(parts) > 4 and parts[4] else "",
        "file_id": "",
        "added_at": now_iso(),
    }
    if len(parts) > 2 and parts[2]:
        rec["aliases"] = [a.strip()[:60] for a in parts[2].split(",") if a.strip()][:10]
    return rec


def record_text(rec) -> str:
    """Serialization used for the channel post (backup record)."""
    return "#HWDATA\n" + json.dumps(rec, ensure_ascii=False)


# ---- channel persistence (Bot API side) --------------------------------------

async def db_post_record(context, rec: dict, file_id: str = "") -> bool:
    """Post a character record into the DB channel and refresh the pinned index."""
    db = get_db_channel(context)
    if not db:
        return False
    if file_id:
        rec["file_id"] = file_id
    try:
        if rec.get("file_id"):
            msg = await context.bot.send_photo(db, rec["file_id"], caption=record_text(rec))
        else:
            msg = await context.bot.send_message(db, record_text(rec))
    except (BadRequest, Forbidden, TimedOut) as exc:
        LOG.error("DB post failed: %s", exc)
        return False
    CHAR_MSGID[rec["char_id"]] = msg.message_id
    await db_update_index(context)
    save_chars()
    save_users_db()
    return True


async def db_delete_record(context, char_id: str) -> bool:
    db = get_db_channel(context)
    mid = CHAR_MSGID.pop(char_id, None)
    if db and mid:
        try:
            await context.bot.delete_message(db, mid)
        except (BadRequest, Forbidden, TimedOut):
            pass  # already gone / no rights; index update below still fixes state
    save_users_db()
    if db:
        await db_update_index(context)
    return True


async def db_update_index(context) -> None:
    """Pinned message = compact index {char_id: message_id}. Recovery map."""
    global INDEX_MSGID
    db = get_db_channel(context)
    if not db:
        return
    payload = json.dumps({"chars": CHAR_MSGID}, ensure_ascii=False)
    if len(payload) > 3500:
        LOG.warning("DB index too large for one message (%d chars); index not updated.", len(payload))
        return
    text = "#HWINDEX\n" + payload
    try:
        if INDEX_MSGID:
            await context.bot.edit_message_text(text, chat_id=db, message_id=INDEX_MSGID)
        else:
            msg = await context.bot.send_message(db, text)
            INDEX_MSGID = msg.message_id
            await context.bot.pin_chat_message(db, INDEX_MSGID, disable_notification=True)
        save_users_db()
    except (BadRequest, Forbidden, TimedOut) as exc:
        LOG.error("Index update failed: %s", exc)


# ----------------------------------------------------------------------------
# 5. IMAGE HASHING (dHash, Pillow only -- Termux-safe, no numpy/opencv)
# ----------------------------------------------------------------------------

def dhash(img, hash_size: int = 8) -> int:
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    img = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


DHASH_THRESHOLD = 8        # max hamming distance to call it "same image"
SEARCH_MAX_DISTANCE = 12   # looser: still worth showing as a candidate
STRONG_MATCH_DISTANCE = 4  # tight: reported as a high-confidence visual match


def name_from_caption(caption):
    """Best-effort character name from a channel caption. Returns None if unknown.
    A name is NEVER invented from the image itself."""
    if not caption:
        return None
    for line in caption.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("|", "—", "–", " - ", " • "):
            if sep in line:
                line = line.split(sep)[0]
                break
        line = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", line)
        line = re.sub(r"#\w+", "", line)  # drop hashtags from the name line
        line = line.strip(" -—|:.•*~")
        if line and len(line) <= 60:
            return line
    tags = [t for t in re.findall(r"#(\w{2,30})", caption)
            if t.lower() not in ("hwdata", "hwindex", "hwbackup", "anime", "waifu", "husbando")]
    if tags:
        return tags[0].replace("_", " ").title()
    return None


def build_entry_excerpt(entry) -> str:
    cap = entry.get("cap") or ""
    return cap[:120].replace("\n", " / ")

# ----------------------------------------------------------------------------
# 6. UI TEXT (English) + KEYBOARD HELPERS
# ----------------------------------------------------------------------------

EMOJI = {"star": "⭐", "heart": "💗", "fire": "🔥", "magnify": "🔍", "crown": "👑",
         "book": "📖", "robot": "🤖", "chart": "📊", "trophy": "🏆", "no": "❌",
         "ok": "✅", "wait": "⏳", "img": "🖼️", "ch": "📺", "lock": "🔒"}


def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search", callback_data="menu:search"),
         InlineKeyboardButton("💗 My Harem", callback_data="menu:harem")],
        [InlineKeyboardButton("⭐ Favorites", callback_data="menu:favs"),
         InlineKeyboardButton("👤 Profile", callback_data="menu:profile")],
        [InlineKeyboardButton("🏆 Top", callback_data="menu:top"),
         InlineKeyboardButton("📖 Help", callback_data="menu:help")],
    ])


WELCOME_TEXT = (
    f"{EMOJI['fire']} <b>Husbandos &amp; Waifus Bot</b>\n\n"
    "Welcome! Search anime characters by name or by photo.\n\n"
    f"{EMOJI['magnify']} <code>.wa &lt;name&gt;</code> or <code>/wa &lt;name&gt;</code> — search a waifu\n"
    f"{EMOJI['magnify']} <code>.op &lt;name&gt;</code> or <code>/op &lt;name&gt;</code> — search a husbando\n"
    f"{EMOJI['img']} Send a photo (or reply to one with <code>.wa</code>) to search by image\n\n"
    f"{EMOJI['book']} Use /help to see all commands."
)

HELP_TEXT = (
    f"{EMOJI['book']} <b>Commands</b>\n\n"
    "<b>User</b>\n"
    "/start — main menu\n"
    "/search &lt;name&gt; — search characters\n"
    ".wa &lt;name&gt; / .op &lt;name&gt; — quick search (also /wa, /op)\n"
    "/harem — your collected characters\n"
    "/fav — your favorites\n"
    "/profile — your stats\n"
    "/top — global leaderboard\n"
    "/ctop — this group's leaderboard\n"
    "/stats — bot statistics\n\n"
    "<b>Assistant account (owner only)</b>\n"
    "/addass — link a Telegram user account (phone login)\n"
    "/myass — show linked account\n"
    "/removeass — unlink account\n"
    "/assstatus — account status\n"
    "/scan — index media from allowed channels\n"
    "/channels — list channels in search scope\n"
    "/searchstatus — image-search system status\n\n"
    "<b>Database channel</b> (owner only)\n"
    "/getdb — show the current database channel\n"
    "/removedb — unset the database channel\n"
    "/dbstatus — database + assistant account status\n"
    "/dbscan — index images from the database channel\n\n"
    "<b>Backup / Restore</b> (owner only)\n"
    "/setbackup &lt;channel_id&gt; — set the Telegram backup channel\n"
    "/getbackup — show the backup channel\n"
    "/backup — create a backup (local file + channel)\n"
    "/restore — restore (reply to a backup file, or the newest one)\n"
    "/backupstatus — backup status and checksum\n"
    "/asscheck — verify the saved assistant session\n\n"
    "<i>Tip: reply to any photo with <code>.wa</code> to search that image.</i>"
)

ERR_NO_QUERY = f"{EMOJI['no']} Please provide a name. Example: <code>.wa zani</code>"
ERR_NOT_FOUND = f"{EMOJI['no']} No character found. Try another name or spelling."
ERR_RATE = f"{EMOJI['wait']} Slow down a little, you are sending commands too fast."
ERR_PERM = f"{EMOJI['lock']} This command is for the bot owner only."
ERR_GENERIC = f"{EMOJI['no']} Something went wrong. Please try again later."


def char_card(rec) -> str:
    lines = [f"{EMOJI['star']} <b>{safe_text(rec.get('name'), 100)}</b>"]
    if rec.get("anime"):
        lines.append(f"{EMOJI['book']} Anime: <b>{safe_text(rec['anime'])}</b>")
    if rec.get("category"):
        lines.append(f"{EMOJI['ch']} Category: {safe_text(rec['category'], 40)}")
    if rec.get("rarity"):
        lines.append(f"{EMOJI['fire']} Rarity: {safe_text(rec['rarity'], 20)}")
    if rec.get("aliases"):
        lines.append(f"{EMOJI['magnify']} Also known as: {safe_text(', '.join(rec['aliases']), 200)}")
    lines.append(f"<i>ID: {safe_text(rec.get('char_id'), 12)}</i>")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 7. PAGINATION (generic callback-data driven)
# ----------------------------------------------------------------------------

PAGE_SIZE = 8


def chunk_list(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def list_view_kb(view: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"pg:{view}:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{max(total_pages, 1)}", callback_data="pg:noop:0"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"pg:{view}:{page + 1}"))
    return InlineKeyboardMarkup([row])


async def safe_send(context, chat_id, text, **kwargs):
    """send_message with FloodWait handling."""
    try:
        return await context.bot.send_message(chat_id, text, **kwargs)
    except RetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        try:
            return await context.bot.send_message(chat_id, text, **kwargs)
        except Exception:
            return None
    except (BadRequest, Forbidden, TimedOut) as exc:
        LOG.warning("send_message to %s failed: %s", chat_id, exc)
        return None


# ----------------------------------------------------------------------------
# 8. VIEWS (data builders for list commands)
# ----------------------------------------------------------------------------

def build_harem_page(uid: str, page: int):
    ids = HAREM.get(uid, [])
    pages = chunk_list(ids, PAGE_SIZE)
    lines = [f"{EMOJI['heart']} <b>My Harem</b> — {len(ids)} character(s)\n"]
    for cid in (pages[page] if pages else []):
        rec = CHARS.get(cid)
        if rec:
            lines.append(f"• <b>{safe_text(rec.get('name'), 60)}</b> — {safe_text(rec.get('anime') or 'Unknown', 40)} <code>{cid}</code>")
        else:
            lines.append(f"• <i>deleted character</i> <code>{cid}</code>")
    kb = list_view_kb(f"harem:{uid}", page, max(len(pages), 1)) if len(pages) > 1 else None
    return "\n".join(lines), kb


def build_fav_page(uid: str, page: int):
    ids = FAVS.get(uid, [])
    pages = chunk_list(ids, PAGE_SIZE)
    lines = [f"{EMOJI['star']} <b>My Favorites</b> — {len(ids)} character(s)\n"]
    for cid in (pages[page] if pages else []):
        rec = CHARS.get(cid)
        if rec:
            lines.append(f"• <b>{safe_text(rec.get('name'), 60)}</b> — {safe_text(rec.get('anime') or 'Unknown', 40)} <code>{cid}</code>")
    kb = list_view_kb(f"favs:{uid}", page, max(len(pages), 1)) if len(pages) > 1 else None
    return "\n".join(lines), kb


def build_top_page(scope_uids, scope_label, page: int):
    entries = []
    for uid in scope_uids:
        u = USERS.get(uid, {})
        if u:
            entries.append((u.get("name") or f"User {uid}", len(HAREM.get(uid, []))))
    entries.sort(key=lambda x: x[1], reverse=True)
    pages = chunk_list(entries, 10)
    lines = [f"{EMOJI['trophy']} <b>Leaderboard</b> — {safe_text(scope_label, 40)}\n"]
    medals = ["🥇", "🥈", "🥉"]
    start = page * 10
    for i, (name, cnt) in enumerate(pages[page] if pages else [], start=start):
        medal = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{medal} {safe_text(name, 40)} — {cnt}")
    if not entries:
        lines.append("<i>Nobody has collected anything yet.</i>")
    kb = list_view_kb("top", page, max(len(pages), 1)) if len(pages) > 1 else None
    return "\n".join(lines), kb


# ----------------------------------------------------------------------------
# 9. SEARCH RESULT PRESENTATION
# ----------------------------------------------------------------------------

async def present_search_results(context, chat_id, exact, partial, reply_to=None):
    if len(exact) == 1:
        rec = exact[0]
        cap = record_result_text(rec)
        kb = char_action_kb(rec, chat_id)
        try:
            if rec.get("file_id"):
                await context.bot.send_photo(chat_id, rec["file_id"], caption=cap,
                                             parse_mode=ParseMode.HTML, reply_markup=kb,
                                             reply_to_message_id=reply_to or None)
            else:
                await context.bot.send_message(chat_id, cap, parse_mode=ParseMode.HTML,
                                               reply_markup=kb, reply_to_message_id=reply_to or None)
        except (BadRequest, Forbidden, TimedOut):
            await safe_send(context, chat_id, cap, parse_mode=ParseMode.HTML)
        return
    if not exact and len(partial) == 1:
        await present_search_results(context, chat_id, partial[:1], [], reply_to)
        return
    # multiple candidates -> inline chooser
    cands = exact or partial
    buttons = []
    for rec in cands[:8]:
        label = rec.get("name", "?")
        if rec.get("anime"):
            label += f" ({rec['anime']})"
        buttons.append([InlineKeyboardButton(safe_text(label, 50), callback_data=f"pick:{rec['char_id']}:{chat_id if isinstance(chat_id, int) else 0}")])
    text = (f"{EMOJI['magnify']} <b>Multiple matches found.</b> Please choose:"
            if len(cands) > 1 else ERR_NOT_FOUND)
    await safe_send(context, chat_id, text, parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons), reply_to_message_id=reply_to or None)


def char_action_kb(rec, chat_id=None):
    """Collect / Favorite row plus the copy buttons for a stored record."""
    rows = [[
        InlineKeyboardButton(f"{EMOJI['heart']} Collect", callback_data=f"collect:{rec['char_id']}"),
        InlineKeyboardButton(f"{EMOJI['star']} Favorite", callback_data=f"fav:{rec['char_id']}"),
    ]]
    return result_buttons(rec.get("name"), source_url=rec.get("source_url", ""), extra_rows=rows)

# ----------------------------------------------------------------------------
# 10. USER COMMAND HANDLERS
# ----------------------------------------------------------------------------

async def register_user(update: Update) -> str:
    u = update.effective_user
    uid = str(u.id)
    if uid not in USERS:
        USERS[uid] = {"name": u.first_name or "User", "first_seen": now_iso(), "count": 0}
        save_users_db()
    elif USERS[uid].get("name") != (u.first_name or "User"):
        USERS[uid]["name"] = u.first_name or "User"
        save_users_db()
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        cid = str(chat.id)
        if cid not in CHATS:
            CHATS[cid] = {"title": chat.title or "Group", "members": {}}
        if uid not in CHATS[cid]["members"]:
            CHATS[cid]["members"][uid] = u.first_name or "User"
            save_json(F_CHATS, CHATS)
    return uid


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await safe_send(context, update.effective_chat.id, WELCOME_TEXT,
                    parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await safe_send(context, update.effective_chat.id, HELP_TEXT, parse_mode=ParseMode.HTML)


def extract_query(update, context) -> str:
    """Command argument text: from context.args, or after the prefix word."""
    if context.args:
        return " ".join(context.args)
    msg = update.effective_message if update else None
    text = (msg.text or "") if msg else ""
    parts = text.split(" ", 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def handle_wa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/wa .wa /op .op and /search -- shared dispatcher."""
    if not rate_ok(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_RATE)
        return
    await register_user(update)
    uid = str(update.effective_user.id)

    # Reply-to-image search: `.wa` as a bare reply to a photo
    query = extract_query(update, context)
    msg = update.effective_message
    # Bare `.wa` (or /wa) as a reply to a photo -> image search instead
    if not query and msg and msg.reply_to_message and (
            msg.reply_to_message.photo
            or (msg.reply_to_message.document and
                str(msg.reply_to_message.document.mime_type or "").startswith("image/"))):
        await handle_image_search(update, context, reply_msg=msg.reply_to_message)
        return
    if not query:
        await safe_send(context, update.effective_chat.id, ERR_NO_QUERY)
        return

    exact, partial = find_characters(query)
    if not exact and not partial:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} No character named <b>{safe_text(query, 60)}</b> found.",
                        parse_mode=ParseMode.HTML)
        return
    await present_search_results(context, update.effective_chat.id, exact, partial)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_wa(update, context)


async def collect_character(uid: str, char_id: str) -> bool:
    rec = CHARS.get(char_id)
    if not rec:
        return False
    lst = HAREM.setdefault(uid, [])
    if char_id in lst:
        return False
    lst.append(char_id)
    if uid in USERS:
        USERS[uid]["count"] = len(lst)
    save_json(F_HAREM, HAREM)
    save_users_db()
    return True


async def on_pick_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User chose one character among several same-name matches."""
    q = update.callback_query
    char_id = (q.data or "").split(":")[1]
    rec = CHARS.get(char_id)
    await q.answer()
    if not rec:
        await q.edit_message_text(ERR_NOT_FOUND)
        return
    cap = record_result_text(rec)
    try:
        if rec.get("file_id"):
            await context.bot.send_photo(q.message.chat_id, rec["file_id"], caption=cap,
                                         parse_mode=ParseMode.HTML, reply_markup=char_action_kb(rec, q.message.chat_id))
        else:
            await q.edit_message_text(cap, parse_mode=ParseMode.HTML,
                                      reply_markup=char_action_kb(rec, q.message.chat_id))
    except (BadRequest, Forbidden, TimedOut):
        await safe_send(context, q.message.chat_id, cap, parse_mode=ParseMode.HTML)


async def on_action_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = str(q.from_user.id)
    data = q.data or ""
    try:
        if data.startswith("collect:"):
            _, char_id = data.split(":", 1)
            ok = await collect_character(uid, char_id)
            await q.answer(f"Added to your harem! {EMOJI['heart']}" if ok
                           else "Already in your harem.")
            if ok:
                rec = CHARS.get(char_id, {})
                await safe_send(context, q.message.chat_id,
                                f"{EMOJI['heart']} <b>{safe_text(q.from_user.first_name, 40)}</b> collected "
                                f"<b>{safe_text(rec.get('name'), 60)}</b>!", parse_mode=ParseMode.HTML)
        elif data.startswith("fav:"):
            _, char_id = data.split(":", 1)
            if char_id not in CHARS:
                await q.answer("Character not found.")
                return
            lst = FAVS.setdefault(uid, [])
            if char_id in lst:
                lst.remove(char_id)
                await q.answer("Removed from favorites.")
            else:
                lst.append(char_id)
                await q.answer(f"Added to favorites! {EMOJI['star']}")
            save_json(F_FAVS, FAVS)
        else:
            await q.answer()
    except Exception as exc:  # never crash on a button
        LOG.error("button error: %s", exc)
        try:
            await q.answer(ERR_GENERIC)
        except Exception:
            pass


async def on_page_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, view, page_s = q.data.split(":", 2)
        page = int(page_s)
    except ValueError:
        await q.answer()
        return
    if view == "noop":
        await q.answer()
        return
    if view.startswith("harem:"):
        owner = view.split(":", 1)[1]
        if str(q.from_user.id) != owner:
            await q.answer("You can only view your own collection.", show_alert=True)
            return
        text, kb = build_harem_page(owner, page)
    elif view.startswith("favs:"):
        owner = view.split(":", 1)[1]
        if str(q.from_user.id) != owner:
            await q.answer("You can only view your own favorites.", show_alert=True)
            return
        text, kb = build_fav_page(owner, page)
    elif view == "top":
        chat = q.message.chat
        if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            entry = CHATS.get(str(chat.id), {})
            scope = list(entry.get("members", {}).keys())
            label = entry.get("title", "This group")
        else:
            scope = list(USERS.keys())
            label = "Global"
        text, kb = build_top_page(scope, label, page)
    else:
        await q.answer()
        return
    await q.answer()
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except (BadRequest, TimedOut):
        pass


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = str(q.from_user.id)
    target = q.data.split(":", 1)[1]
    await q.answer()
    if target == "search":
        await safe_send(context, q.message.chat_id,
                        f"{EMOJI['magnify']} Use <code>.wa name</code>, <code>.op name</code> or /search to find characters.",
                        parse_mode=ParseMode.HTML)
    elif target == "harem":
        text, kb = build_harem_page(uid, 0)
        await safe_send(context, q.message.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif target == "favs":
        text, kb = build_fav_page(uid, 0)
        await safe_send(context, q.message.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif target == "profile":
        text = build_profile_text(uid)
        await safe_send(context, q.message.chat_id, text, parse_mode=ParseMode.HTML)
    elif target == "top":
        text, kb = build_top_page(list(USERS.keys()), "Global", 0)
        await safe_send(context, q.message.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif target == "help":
        await safe_send(context, q.message.chat_id, HELP_TEXT, parse_mode=ParseMode.HTML)


def build_profile_text(uid: str) -> str:
    u = USERS.get(uid, {})
    harem = HAREM.get(uid, [])
    favs = FAVS.get(uid, [])
    animes = {CHARS[c]["anime"] for c in harem if c in CHARS and CHARS[c].get("anime")}
    lines = [
        f"{EMOJI['crown']} <b>{safe_text(u.get('name', 'User'), 40)}</b>'s Profile",
        f"ID: <code>{uid}</code>",
        f"Joined: {safe_text(u.get('first_seen', '—'), 24)}",
        "",
        f"{EMOJI['heart']} Collected: <b>{len(harem)}</b>",
        f"{EMOJI['star']} Favorites: <b>{len(favs)}</b>",
        f"{EMOJI['book']} Series: <b>{len(animes)}</b>",
    ]
    return "\n".join(lines)


async def cmd_harem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    uid = str(update.effective_user.id)
    text, kb = build_harem_page(uid, 0)
    await safe_send(context, update.effective_chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_fav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    uid = str(update.effective_user.id)
    text, kb = build_fav_page(uid, 0)
    await safe_send(context, update.effective_chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    uid = str(update.effective_user.id)
    await safe_send(context, update.effective_chat.id, build_profile_text(uid), parse_mode=ParseMode.HTML)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    text, kb = build_top_page(list(USERS.keys()), "Global", 0)
    await safe_send(context, update.effective_chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_ctop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_send(context, chat.id, f"{EMOJI['no']} /ctop works in groups only.")
        return
    entry = CHATS.get(str(chat.id), {})
    scope = list(entry.get("members", {}).keys())
    text, kb = build_top_page(scope, entry.get("title", chat.title or "This group"), 0)
    await safe_send(context, chat.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    total_chars = len(CHARS)
    total_users = len(USERS)
    total_collected = sum(len(v) for v in HAREM.values())
    groups = len(CHATS)
    indexed = len(IMG_INDEX.get("entries", []))
    text = (
        f"{EMOJI['chart']} <b>Bot Statistics</b>\n\n"
        f"{EMOJI['star']} Characters in database: <b>{total_chars}</b>\n"
        f"{EMOJI['robot']} Registered users: <b>{total_users}</b>\n"
        f"{EMOJI['heart']} Total collections: <b>{total_collected}</b>\n"
        f"{EMOJI['ch']} Groups served: <b>{groups}</b>\n"
        f"{EMOJI['img']} Channel images indexed: <b>{indexed}</b>"
    )
    await safe_send(context, update.effective_chat.id, text, parse_mode=ParseMode.HTML)

# ----------------------------------------------------------------------------
# 11. ASSISTANT ACCOUNT (Telethon user login) -- owner only, private chat only
# ----------------------------------------------------------------------------

ASS_LOGIN_STATE = {}  # uid -> {"step": "phone"|"code"|"password", "phone":..., "client":...}


def ass_session_active() -> bool:
    return bool(ASS_CLIENT) and os.path.exists(F_SESSION)


async def ass_start_client() -> bool:
    """Start (or restart) the Telethon client from the saved session."""
    global ASS_CLIENT
    if not TELETHON_OK:
        return False
    if not (API_ID and API_HASH):
        return False
    session = ""
    if os.path.exists(F_SESSION):
        try:
            with open(F_SESSION, "r", encoding="utf-8") as fh:
                session = fh.read().strip()
        except OSError:
            pass
    if not session:
        return False
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return False
        ASS_CLIENT = client
        return True
    except Exception as exc:
        LOG.error("Assistant client start failed: %s", exc)
        return False


def ass_stop_client() -> None:
    global ASS_CLIENT
    if ASS_CLIENT:
        try:
            ASS_CLIENT.loop.create_task(ASS_CLIENT.disconnect())
        except Exception:
            pass
        ASS_CLIENT = None


async def cmd_addass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    uid = update.effective_user.id
    if not is_owner(uid):
        await safe_send(context, chat.id, ERR_PERM)
        return
    if not TELETHON_OK:
        await safe_send(context, chat.id, f"{EMOJI['no']} Telethon is not installed. Run: <code>pip install telethon</code>")
        return
    if not (API_ID and API_HASH):
        await safe_send(context, chat.id,
                        f"{EMOJI['no']} Missing <code>API_ID</code> / <code>API_HASH</code>. "
                        "Get them at https://my.telegram.org and put them in your .env file.",
                        parse_mode=ParseMode.HTML)
        return
    if chat.type != ChatType.PRIVATE:
        await safe_send(context, chat.id,
                        f"{EMOJI['lock']} Login codes must stay private. Please DM me and use /addass there.")
        return
    if ass_session_active():
        await safe_send(context, chat.id, f"{EMOJI['ok']} An assistant account is already linked. Use /removeass first.")
        return
    ASS_LOGIN_STATE[uid] = {"step": "phone"}
    await safe_send(context, chat.id,
                    f"{EMOJI['robot']} <b>Assistant account linking</b>\n\n"
                    "Send the <b>phone number</b> of the Telegram user account you own "
                    "(with country code), for example: <code>+95912345678</code>\n\n"
                    f"{EMOJI['no']} Send /cancel to abort. "
                    "Only proceed if this account belongs to you.", parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    state = ASS_LOGIN_STATE.pop(uid, None)
    if state and state.get("client"):
        try:
            await state["client"].disconnect()
        except Exception:
            pass
    await safe_send(context, update.effective_chat.id, f"{EMOJI['ok']} Cancelled.")


async def ass_login_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle phone / code / password steps of the interactive login."""
    chat = update.effective_chat
    uid = update.effective_user.id
    state = ASS_LOGIN_STATE.get(uid)
    if not state or chat.type != ChatType.PRIVATE:
        return
    text = (update.effective_message.text or "").strip()
    if text.startswith(("/", ".")):
        return  # let commands pass through
    step = state["step"]

    try:
        if step == "phone":
            if not re.match(r"^\+?\d{6,16}$", text):
                await safe_send(context, chat.id, f"{EMOJI['no']} That does not look like a valid phone number. Try again or /cancel")
                return
            state["phone"] = text
            state["client"] = TelegramClient(StringSession(), API_ID, API_HASH)
            await state["client"].connect()
            await state["client"].send_code_request(text)
            state["step"] = "code"
            await safe_send(context, chat.id,
                            f"{EMOJI['wait']} Telegram sent a <b>login code</b> to that account "
                            "(check the Telegram app / SMS).\nSend the code here (numbers only).",
                            parse_mode=ParseMode.HTML)
        elif step == "code":
            code = re.sub(r"\D", "", text)
            state["code"] = code
            try:
                await state["client"].sign_in(phone=state["phone"], code=code)
            except tl_errors.SessionPasswordNeededError:
                state["step"] = "password"
                await safe_send(context, chat.id,
                                f"{EMOJI['lock']} Two-step verification is enabled.\n"
                                "Send your <b>Telegram password</b> here (private chat only).",
                                parse_mode=ParseMode.HTML)
                return
            await ass_finish_login(update, context, uid, state)
        elif step == "password":
            try:
                await state["client"].sign_in(password=text)
            except tl_errors.PasswordHashInvalidError:
                await safe_send(context, chat.id, f"{EMOJI['no']} Wrong password. Try again or /cancel")
                return
            await ass_finish_login(update, context, uid, state)
    except tl_errors.PhoneCodeInvalidError:
        await safe_send(context, chat.id, f"{EMOJI['no']} Invalid code. Check it and send again.")
    except tl_errors.PhoneCodeExpiredError:
        state["step"] = "phone"
        await safe_send(context, chat.id,
                        f"{EMOJI['no']} The code expired. Send your phone number again to restart.")
    except tl_errors.FloodWaitError as exc:
        await safe_send(context, chat.id,
                        f"{EMOJI['wait']} Telegram rate limit: please wait <b>{exc.seconds} s</b> and try again.",
                        parse_mode=ParseMode.HTML)
    except Exception as exc:
        LOG.error("ass login error: %s", exc)
        await safe_send(context, chat.id, ERR_GENERIC)


async def ass_finish_login(update: Update, context: ContextTypes.DEFAULT_TYPE, uid, state) -> None:
    global ASS_CLIENT, ASS_PHONE
    client = state.pop("client")
    ASS_CLIENT = client
    ASS_PHONE = state.get("phone", "")
    session_str = client.session.save()
    tmp = F_SESSION + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(session_str)
    try:
        os.chmod(tmp, 0o600)
        os.replace(tmp, F_SESSION)
    except OSError:
        pass
    ASS_LOGIN_STATE.pop(uid, None)
    me = await client.get_me()
    name = f"{getattr(me, 'first_name', '')} (@{getattr(me, 'username', None) or 'no-username'})"
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} <b>Assistant account linked!</b>\n"
                    f"Account: {safe_text(name, 80)}\n"
                    f"Session saved securely under <code>data/</code>. Use /scan to index channels.",
                    parse_mode=ParseMode.HTML)


async def cmd_removeass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not ass_session_active():
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} No assistant account linked.")
        return
    ass_stop_client()
    try:
        os.remove(F_SESSION)
    except OSError:
        pass
    await safe_send(context, update.effective_chat.id, f"{EMOJI['ok']} Assistant account removed.")


async def cmd_myass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not ass_session_active():
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} No assistant account linked. Use /addass.")
        return
    me = await ASS_CLIENT.get_me()
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['robot']} <b>Assistant account</b>\n"
                    f"Name: {safe_text(getattr(me, 'first_name', ''), 60)}\n"
                    f"Username: @{safe_text(getattr(me, 'username', None) or 'none', 40)}",
                    parse_mode=ParseMode.HTML)


async def cmd_assstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    ok = ass_session_active()
    text = (f"{EMOJI['ok']} Assistant account: <b>connected</b>" if ok
            else f"{EMOJI['no']} Assistant account: <b>not connected</b>")
    text += f"\nTelethon installed: {'yes' if TELETHON_OK else 'no (pip install telethon)'}"
    text += f"\nAPI_ID/API_HASH set: {'yes' if API_ID and API_HASH else 'no'}"
    await safe_send(context, update.effective_chat.id, text, parse_mode=ParseMode.HTML)

# ----------------------------------------------------------------------------
# 12. ADMIN COMMANDS (owner only, verified by numeric Telegram user ID)
# ----------------------------------------------------------------------------

async def cmd_setdb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setdb -100xxxxxxxxxx  -- set the database channel."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not context.args:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Usage: <code>/setdb -1001234567890</code> (the channel ID, bot must be admin there).",
                        parse_mode=ParseMode.HTML)
        return
    cid = _to_int(context.args[0])
    if cid is None:
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} Invalid channel ID.")
        return
    RUN_CFG["db_channel_id"] = cid
    save_json(F_CFG, RUN_CFG)
    global INDEX_MSGID
    INDEX_MSGID = None  # new channel -> new index
    save_users_db()
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} Database channel set to <code>{cid}</code>.", parse_mode=ParseMode.HTML)


async def cmd_addchar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addchar Name | Anime | alias,alias | category | rarity  (reply to a photo optional)"""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not get_db_channel(context):
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Set a database channel first with /setdb.", parse_mode=ParseMode.HTML)
        return
    args_text = " ".join(context.args) if context.args else ""
    msg = update.effective_message
    rec = parse_addchar_args(args_text)
    if not rec:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Usage:\n<code>/addchar Zani | Wuthering Waves | zani-ww | waifu | SSR</code>\n"
                        "(reply to a photo to attach an image)",
                        parse_mode=ParseMode.HTML)
        return
    rec["char_id"] = new_char_id()
    file_id = ""
    if msg.reply_to_message and msg.reply_to_message.photo:
        file_id = msg.reply_to_message.photo[-1].file_id
    CHARS[rec["char_id"]] = rec
    ok = await db_post_record(context, rec, file_id)
    if ok:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['ok']} Added <b>{safe_text(rec['name'], 60)}</b> "
                        f"<code>{rec['char_id']}</code> (saved to channel + cache).",
                        parse_mode=ParseMode.HTML)
    else:
        # keep local copy; channel backup failed
        save_chars()
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['wait']} Added locally as <code>{rec['char_id']}</code>, but the channel backup failed. "
                        "Check that the bot is admin in the DB channel.",
                        parse_mode=ParseMode.HTML)


async def cmd_editchar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/editchar C00001 field value...  fields: name, anime, category, rarity, aliases"""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if len(context.args or []) < 3:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Usage: <code>/editchar C00001 anime New Anime Name</code>\n"
                        "Fields: name, anime, category, rarity, aliases(comma list)",
                        parse_mode=ParseMode.HTML)
        return
    cid, field = context.args[0].upper(), context.args[1].lower()
    value = " ".join(context.args[2:])
    rec = CHARS.get(cid)
    if not rec:
        await safe_send(context, update.effective_chat.id, ERR_NOT_FOUND)
        return
    if field not in ("name", "anime", "category", "rarity", "aliases"):
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} Unknown field: <code>{safe_text(field, 20)}</code>")
        return
    if field == "aliases":
        rec["aliases"] = [a.strip()[:60] for a in value.split(",") if a.strip()][:10]
    else:
        rec[field] = value[:100]
    ok = await db_post_record(context, rec, rec.get("file_id", ""))
    save_chars()
    if ok and cid in CHAR_MSGID:
        try:  # try to remove the old record post so the channel stays clean
            await context.bot.delete_message(get_db_channel(context), CHAR_MSGID[cid])
        except (BadRequest, Forbidden, TimedOut):
            pass
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} Updated <code>{cid}</code>: {safe_text(field, 20)} set.",
                    parse_mode=ParseMode.HTML)


async def cmd_delchar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not context.args:
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} Usage: <code>/delchar C00001</code>")
        return
    cid = context.args[0].upper()
    if cid not in CHARS:
        await safe_send(context, update.effective_chat.id, ERR_NOT_FOUND)
        return
    rec = CHARS.pop(cid)
    await db_delete_record(context, cid)
    save_chars()
    for store in (HAREM, FAVS):
        for uid in list(store):
            store[uid] = [c for c in store[uid] if c != cid]
    save_json(F_HAREM, HAREM)
    save_json(F_FAVS, FAVS)
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} Removed <b>{safe_text(rec.get('name'), 60)}</b> <code>{cid}</code>.",
                    parse_mode=ParseMode.HTML)


async def cmd_listchar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    total = len(CHARS)
    recs = sorted(CHARS.values(), key=lambda r: r.get("char_id", ""))
    show = recs[:20]
    lines = [f"{EMOJI['book']} <b>Characters</b> ({total} total, showing {len(show)}):\n"]
    for rec in show:
        lines.append(f"• <code>{rec.get('char_id')}</code> {safe_text(rec.get('name'), 50)}"
                     + (f" — {safe_text(rec.get('anime'), 40)}" if rec.get("anime") else ""))
    if total > 20:
        lines.append(f"\n<i>...and {total - 20} more. Use /dbcheck for totals.</i>")
    if not show:
        lines.append("<i>Database is empty. Add with /addchar.</i>")
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_dbcheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    db = get_db_channel(context)
    lines = [f"{EMOJI['chart']} <b>Database status</b>",
             f"DB channel: <code>{db if db else 'NOT SET (/setdb)'}</code>",
             f"Local characters: <b>{len(CHARS)}</b>",
             f"Index mapping (char→post): <b>{len(CHAR_MSGID)}</b>",
             f"Channel index message: {'yes' if INDEX_MSGID else 'no'}",
             f"Image index entries: <b>{len(IMG_INDEX.get('entries', []))}</b>",
             f"Assistant account: {'connected' if ass_session_active() else 'not connected'}",
             f"Recovery log entries: see <code>data/recovery_log.txt</code>"]
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_recover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recover -- rebuild the local DB from the pinned index + record posts in the DB channel."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    db = get_db_channel(context)
    if not db:
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} No DB channel set. Use /setdb first.")
        return
    await safe_send(context, update.effective_chat.id, f"{EMOJI['wait']} Recovering from channel... (this reads the pinned index, then each record post)")
    restored, skipped = 0, 0
    # The Bot API cannot read channel history. Recovery therefore uses the pinned
    # index + individually fetched posts (index → message_id → record).
    try:
        idx_msg = await context.bot.forward_message(
            chat_id=update.effective_chat.id, from_chat_id=db, message_id=INDEX_MSGID)
        text = idx_msg.caption or idx_msg.text or ""
        await idx_msg.delete()
        payload = json.loads(text.split("\n", 1)[1]) if "\n" in text else {}
        mapping = payload.get("chars", {})
    except Exception as exc:
        LOG.error("recover failed: %s", exc)
        mapping = {}
    log_lines = [f"[{now_iso()}] recovery started, index entries={len(mapping)}"]
    for cid, mid in mapping.items():
        try:
            m = await context.bot.forward_message(chat_id=update.effective_chat.id,
                                                  from_chat_id=db, message_id=mid)
            body = m.caption or m.text or ""
            await m.delete()
            if "#HWDATA" not in body:
                skipped += 1
                continue
            rec = json.loads(body.split("#HWDATA", 1)[1].strip())
            if "char_id" in rec and rec["char_id"] not in CHARS:
                CHARS[rec["char_id"]] = rec
                CHAR_MSGID[rec["char_id"]] = mid
                restored += 1
            elif rec.get("char_id") in CHARS:
                skipped += 1  # dedupe: never create duplicate characters
        except (BadRequest, Forbidden, TimedOut) as exc:
            log_lines.append(f"  miss {cid} (msg {mid}): {exc}")
            skipped += 1
        await asyncio.sleep(0.4)  # stay under rate limits
    save_chars()
    save_users_db()
    log_lines.append(f"[{now_iso()}] restored={restored} skipped={skipped}")
    try:
        with open(F_RECOVERY_LOG, "a", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines) + "\n")
    except OSError:
        pass
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} Recovery finished. Restored: <b>{restored}</b>, skipped/duplicates: <b>{skipped}</b>.",
                    parse_mode=ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast <text>  (owner only; to all known users/groups)"""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    text = " ".join(context.args or [])
    if not text:
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} Usage: <code>/broadcast Hello everyone!</code>")
        return
    targets = set(USERS.keys()) | set(CHATS.keys())
    sent = 0
    await safe_send(context, update.effective_chat.id, f"{EMOJI['wait']} Broadcasting to {len(targets)} chats...")
    for tid in targets:
        ok = await safe_send(context, int(tid), f"📢 <b>Announcement</b>\n\n{safe_text(text, 2000)}",
                             parse_mode=ParseMode.HTML)
        if ok:
            sent += 1
        await asyncio.sleep(0.15)
    await safe_send(context, update.effective_chat.id, f"{EMOJI['ok']} Broadcast delivered to {sent}/{len(targets)} chats.")


# ----------------------------------------------------------------------------
# 13. AUTO CHANNEL SEARCH (Telethon user account) + IMAGE MATCHING
# ----------------------------------------------------------------------------

SCAN_LIMIT_PER_CHAT = 300      # messages per channel per /scan
IMG_INDEX_MAX = 5000           # keep the index bounded


def search_scope_allowed() -> list:
    return SEARCH_CHANNELS


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not search_scope_allowed():
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['ch']} No channels in the search scope yet.\n"
                        "Link an assistant account (/addass), have that account join the channels you want, "
                        "then run /scan.",
                        parse_mode=ParseMode.HTML)
        return
    lines = [f"{EMOJI['ch']} <b>Channels in search scope</b> ({len(SEARCH_CHANNELS)}):\n"]
    for ch in SEARCH_CHANNELS[:30]:
        lines.append(f"• {safe_text(ch.get('title') or ch.get('chat_id') or ch.get('username'), 60)}")
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/scan -- index media + captions from channels the assistant account joined."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not ass_session_active():
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Link an assistant account first: /addass", parse_mode=ParseMode.HTML)
        return
    status = await safe_send(context, update.effective_chat.id,
                             f"{EMOJI['wait']} Scanning channels... this can take a while.")
    added = 0
    chats_scanned = 0
    global SEARCH_CHANNELS
    async for dialog in ASS_CLIENT.iter_dialogs():
        if not dialog.is_channel:
            continue
        entity = dialog.entity
        title = getattr(entity, "title", None) or dialog.name
        SEARCH_CHANNELS = [c for c in SEARCH_CHANNELS if c.get("chat_id") != dialog.id]
        SEARCH_CHANNELS.append({"chat_id": dialog.id, "title": title})
        chats_scanned += 1
        last_id = IMG_INDEX["last_id"].get(str(dialog.id), 0)
        count = 0
        try:
            async for message in ASS_CLIENT.iter_messages(dialog.entity, limit=SCAN_LIMIT_PER_CHAT,
                                                          min_id=last_id, reverse=True):
                count += 1
                if message.photo or (message.document and getattr(message.document, "mime_type", "").startswith("image/")):
                    blob = await message.download_media(file=io.BytesIO(), thumb=-1)
                    if blob and PIL_OK:
                        try:
                            img = Image.open(blob)
                            h = dhash(img)
                            caption = message.message or ""
                            entry = {
                                "c": dialog.id,            # chat id
                                "m": message.id,           # message id
                                "h": format(h, "016x"),    # dHash hex
                                "cap": caption[:200],
                                "t": message.date.isoformat() if message.date else "",
                            }
                            # dedupe identical hashes within same chat
                            if not any(e["c"] == dialog.id and e["h"] == entry["h"]
                                       for e in IMG_INDEX["entries"][-200:]):
                                IMG_INDEX["entries"].append(entry)
                                added += 1
                        except Exception as exc:
                            LOG.debug("hash failed: %s", exc)
                if len(IMG_INDEX["entries"]) > IMG_INDEX_MAX:
                    IMG_INDEX["entries"] = IMG_INDEX["entries"][-IMG_INDEX_MAX:]
            IMG_INDEX["last_id"][str(dialog.id)] = max(last_id, (message.id if count else last_id))
        except tl_errors.FloodWaitError as exc:
            await safe_send(context, update.effective_chat.id,
                            f"{EMOJI['wait']} Telegram flood limit hit; waiting {exc.seconds}s.")
            await asyncio.sleep(exc.seconds + 1)
        except (tl_errors.ChannelPrivateError, tl_errors.ChatAdminRequiredError):
            LOG.info("Skipping restricted channel %s", title)
        except Exception as exc:
            LOG.warning("scan error in %s: %s", title, exc)
    save_json(F_IMGIDX, IMG_INDEX)
    save_json(F_CHANNELS, SEARCH_CHANNELS)
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} Scan finished: {chats_scanned} channel(s), {added} new image(s) indexed.\n"
                    f"Total indexed: {len(IMG_INDEX['entries'])}",
                    parse_mode=ParseMode.HTML)
    if status:
        try:
            await status.delete()
        except Exception:
            pass


async def cmd_searchstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    entries = len(IMG_INDEX.get("entries", []))
    chats = len(IMG_INDEX.get("last_id", {}))
    text = (f"{EMOJI['chart']} <b>Image search status</b>\n\n"
            f"Assistant account: {'connected' if ass_session_active() else 'not connected'}\n"
            f"Channels in scope: <b>{len(SEARCH_CHANNELS)}</b>\n"
            f"Indexed images: <b>{entries}</b>\n"
            f"Scan position saved for: {chats} channel(s)\n"
            f"Hash method: dHash (Pillow) — names come from captions only.")
    await safe_send(context, update.effective_chat.id, text, parse_mode=ParseMode.HTML)


def find_by_image(img) -> list:
    """Return (distance, entry) pairs, closest first.

    dHash is always used; aHash is used as a second opinion and pHash is added
    when the optional `imagehash` package is installed. Similarity only says
    'same/similar picture' -- it can never prove a character name."""
    target = compute_hashes(img)
    scored = []
    for entry in IMG_INDEX.get("entries", []):
        hashes = entry_hashes(entry)
        dist = hash_distance(target["d"], hashes.get("d"))
        if dist is None:
            continue
        dists = [dist]
        alt = hash_distance(target["a"], hashes.get("a"))
        if alt is not None:
            dists.append(alt)
        pdist = hash_distance(target["p"], hashes.get("p"))
        if pdist is not None:
            dists.append(pdist)
        # worst-case across the available hashes keeps false positives out
        worst = max(dists)
        best = min(dists)
        if best > SEARCH_MAX_DISTANCE:
            continue
        scored.append((worst, best, entry))
    scored.sort(key=lambda x: (x[1], x[0]))
    return [(item[0], item[2]) for item in scored[:5]]


def tme_link(chat_id: int, msg_id: int, channels) -> str:
    for ch in channels:
        if ch.get("chat_id") == chat_id and ch.get("username"):
            return f"https://t.me/{ch['username']}/{msg_id}"
    return ""


async def handle_image_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              reply_msg=None) -> None:
    """Search by photo: user sends a photo, or replies to one with .wa/.op."""
    if not PIL_OK:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Pillow is required for image search: <code>pip install pillow</code>")
        return
    msg = update.effective_message
    photo_msg = reply_msg or msg
    if not photo_msg:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Send a photo, or reply to a photo with <code>.wa</code>.",
                        parse_mode=ParseMode.HTML)
        return
    doc = getattr(photo_msg, "document", None)
    is_image_doc = bool(doc and str(getattr(doc, "mime_type", "") or "").startswith("image/"))
    if not photo_msg.photo and not is_image_doc:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Send a photo, or reply to a photo with <code>.wa</code>.",
                        parse_mode=ParseMode.HTML)
        return
    if not IMG_INDEX.get("entries"):
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['wait']} The image index is empty. Set the database channel "
                        "with <code>/setdb</code> and ask the owner to run <code>/dbscan</code>.",
                        parse_mode=ParseMode.HTML)
        return
    wait = await safe_send(context, update.effective_chat.id,
                           f"{EMOJI['wait']} Searching image database...")
    try:
        tg_file = await (doc.get_file() if is_image_doc else photo_msg.photo[-1].get_file())
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        img = Image.open(buf)
    except Exception as exc:
        LOG.error("image download failed: %s", exc)
        if wait:
            try:
                await wait.delete()
            except Exception:
                pass
        await safe_send(context, update.effective_chat.id, ERR_GENERIC)
        return
    matches = find_by_image(img)
    if wait:
        try:
            await wait.delete()
        except Exception:
            pass
    if not matches:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} No matching image found in the indexed channels. "
                        "The owner can index more channels with <code>/scan</code> or "
                        "<code>/dbscan</code>.",
                        parse_mode=ParseMode.HTML)
        return
    reply_to = msg.message_id if (reply_msg is not None and msg) else None
    await present_image_result(context, update.effective_chat.id, matches, reply_to=reply_to)


async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo sent directly to the bot in private chat -> image search."""
    if not rate_ok(update.effective_user.id, min_gap=2.0):
        await safe_send(context, update.effective_chat.id, ERR_RATE)
        return
    await register_user(update)
    await handle_image_search(update, context)

# ----------------------------------------------------------------------------
# 14. PREFIX-COMMAND ROUTER (.wa / .op etc.) + LOGIN TEXT ROUTER
# ----------------------------------------------------------------------------

PREFIX_ALIASES = {".wa": "wa", ".op": "op", ".find": "find",
                  ".search": "search"}
PREFIX_HELP = ".wa / .op / .find"


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single entry point for non-slash text: assistant login steps + prefix commands."""
    if not update.effective_message or not update.effective_user:
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    chat = update.effective_chat
    uid = update.effective_user.id

    # 1) interactive assistant-account login (private chat only, never a command)
    state = ASS_LOGIN_STATE.get(uid)
    if state and chat.type == ChatType.PRIVATE and not text.startswith("."):
        await ass_login_text_handler(update, context)
        return

    # 2) prefix commands (.wa / .op / .find)
    if text.startswith("."):
        parts = text.split(" ", 1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in PREFIX_ALIASES:
            context.args = rest.split() if rest else []
            reply = update.effective_message.reply_to_message
            has_photo = bool(reply and (reply.photo or (
                reply.document and str(reply.document.mime_type or "").startswith("image/"))))
            if not rest and has_photo:
                # bare `.wa` / `.op` replying to a photo -> search by image
                await handle_image_search(update, context, reply_msg=reply)
                return
            if not rest:
                await safe_send(context, chat.id, ERR_NO_QUERY, parse_mode=ParseMode.HTML)
                return
            await handle_wa(update, context)
            return
        # unknown prefix command: short hint (avoid spam)
        await safe_send(context, chat.id,
                        f"{EMOJI['no']} Unknown command. Try <code>.wa name</code>, "
                        "<code>.op name</code> or /help.",
                        parse_mode=ParseMode.HTML)

# ----------------------------------------------------------------------------
# 15.5  COPY BUTTONS + SEARCH RESULT CARD (reference layout, own commands only)
# ----------------------------------------------------------------------------

HINT_STOPWORDS = {"the", "and", "of", "a", "an", "de", "van", "von", "san", "no"}
COPY_TEXT_LIMIT = 256      # Telegram copy_text limit
COPY_CACHE_MAX = 800


def make_hint(name: str) -> str:
    """Short re-searchable hint for a character name (first meaningful word)."""
    n = normalize(name)
    n = re.sub(r"[^\w\s\-']", " ", n)
    words = [w for w in n.split() if len(w) >= 3 and w not in HINT_STOPWORDS]
    if words:
        return words[0]
    words = [w for w in n.split() if w]
    return words[0] if words else (name or "").strip().lower()


def copy_token(text: str) -> str:
    """Remember a copy payload and return a short token (fallback buttons)."""
    global _COPY_SEQ
    _COPY_SEQ += 1
    tok = format(int(time.time() * 1000), "x") + format(_COPY_SEQ, "x")
    COPY_CACHE[tok] = (text or "")[:1024]
    if len(COPY_CACHE) > COPY_CACHE_MAX:
        for key in list(COPY_CACHE)[:200]:
            COPY_CACHE.pop(key, None)
    save_json(F_COPYCACHE, COPY_CACHE)
    return tok


def copy_button(label: str, payload: str):
    """Clipboard button when the Bot API supports copy_text, otherwise a button
    that resends the text as a tappable <code> block (works on every client)."""
    payload = (payload or "")[:COPY_TEXT_LIMIT]
    if COPY_TEXT_OK:
        try:
            return InlineKeyboardButton(label, copy_text=CopyTextButton(text=payload))
        except Exception as exc:  # pragma: no cover
            LOG.debug("copy_text button unavailable: %s", exc)
    return InlineKeyboardButton(label, callback_data=f"cp:{copy_token(payload)}")


def result_buttons(name, hint=None, source_url="", extra_rows=None):
    """Full / NAME / Hint copy buttons + an optional Source link for one character."""
    name = (name or "").strip()
    hint = hint or make_hint(name)
    block = f"NAME : {name}\n━━━━━━━━━━━━━━━━\n🔹 Hint : /wa {hint}\n🔸 Full : {name}"
    rows = [
        [copy_button("📋 Full", block), copy_button("📋 NAME", name)],
        [copy_button("📋 Hint", hint)],
    ]
    if source_url and str(source_url).startswith("http"):
        rows[1].append(InlineKeyboardButton("🔗 Source", url=str(source_url)))
    for row in (extra_rows or []):
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(rows)


def result_card(name, hint=None, anime="", confidence="", source_url="",
                note="", extra_lines=None):
    """Reference-style result block. Only commands this bot really has are shown."""
    name = (name or "").strip()
    hint = hint or make_hint(name)
    lines = [
        f"<b>NAME : {safe_text(name, 100)}</b>",
        "━━━━━━━━━━━━━━━━",
        f"🔹 <b>Hint :</b> <code>/wa {safe_text(hint, 40)}</code>",
        f"🔸 <b>Full :</b> <code>{safe_text(name, 100)}</code>",
    ]
    if anime:
        lines.append(f"📖 Series: {safe_text(anime, 80)}")
    if confidence:
        lines.append(f"🎯 Match: {safe_text(confidence, 40)}")
    for extra in (extra_lines or []):
        if extra:
            lines.append(extra)
    if source_url and str(source_url).startswith("http"):
        lines.append(f"🔗 Source: {source_url}")
    if note:
        lines.append("")
        lines.append(f"<i>{note}</i>")
    return "\n".join(lines)


def record_result_text(rec, confidence="", source_url="", note=""):
    """Result card for a locally stored character record."""
    extra = []
    if rec.get("category"):
        extra.append(f"📺 Category: {safe_text(rec['category'], 40)}")
    if rec.get("rarity"):
        extra.append(f"🔥 Rarity: {safe_text(rec['rarity'], 20)}")
    if rec.get("aliases"):
        extra.append(f"🔍 Also known as: {safe_text(', '.join(rec['aliases']), 200)}")
    extra.append(f"<i>ID: {safe_text(rec.get('char_id'), 12)}</i>")
    return result_card(rec.get("name"), anime=rec.get("anime", ""), confidence=confidence,
                       source_url=source_url or rec.get("source_url", ""),
                       note=note, extra_lines=extra)


async def on_copy_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback for clients / bot-API versions without copy_text buttons."""
    q = update.callback_query
    data = q.data or ""
    tok = data.split(":", 1)[1] if ":" in data else ""
    text = COPY_CACHE.get(tok)
    if not text:
        await q.answer("This copy button expired — search again.", show_alert=True)
        return
    await q.answer("Text sent below — tap it to copy 📋")
    await safe_send(context, q.message.chat_id, f"<code>{safe_text(text, 900)}</code>",
                    parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------------------
# 15.6  MULTI-HASH IMAGE MATCHING (dHash + aHash + optional pHash)
# ----------------------------------------------------------------------------

def ahash(img, hash_size: int = 8) -> int:
    """Average hash — cheap second opinion next to dHash."""
    img = img.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / float(len(pixels) or 1)
    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= avg else 0)
    return bits


def phash_hex(img) -> str:
    """Perceptual hash through the optional `imagehash` package ('' if missing)."""
    if not IMAGEHASH_OK:
        return ""
    try:
        return str(imagehash.phash(img))
    except Exception:
        return ""


def compute_hashes(img) -> dict:
    return {"d": format(dhash(img), "016x"),
            "a": format(ahash(img), "016x"),
            "p": phash_hex(img)}


def entry_hashes(entry) -> dict:
    out = {}
    if entry.get("h"):
        out["d"] = entry["h"]
    if entry.get("a"):
        out["a"] = entry["a"]
    if entry.get("p"):
        out["p"] = entry["p"]
    return out


def hash_distance(hex_a, hex_b):
    try:
        return hamming(int(str(hex_a), 16), int(str(hex_b), 16))
    except (TypeError, ValueError):
        return None


def confidence_label(dist: int) -> str:
    if dist <= STRONG_MATCH_DISTANCE:
        return "high"
    if dist <= 8:
        return "medium"
    return "low"


def entry_name_from_caption(caption: str) -> str:
    """Name from a channel caption — #HWDATA records are parsed as JSON."""
    cap = caption or ""
    if cap.startswith("#HWDATA"):
        try:
            rec = json.loads(cap.split("#HWDATA", 1)[1].strip())
            return str(rec.get("name", "")).strip()
        except (ValueError, IndexError):
            return ""
    return name_from_caption(cap) or ""


def entry_name(entry) -> str:
    """Stored name, else parsed from the caption. Never invented from the image."""
    name = (entry.get("name") or "").strip()
    if name:
        return name
    return entry_name_from_caption(entry.get("cap") or "")


async def present_image_result(context, chat_id, matches, reply_to=None) -> None:
    """Show the closest visual match. A name appears only when a caption has one."""
    dist, entry = matches[0]
    name = entry_name(entry)
    conf = confidence_label(dist)
    source = tme_link(entry.get("c", 0), entry.get("m", 0), SEARCH_CHANNELS)

    if name:
        exact, _ = find_characters(name)
        if len(exact) == 1:
            rec = exact[0]
            text = record_result_text(
                rec, confidence=conf, source_url=source,
                note=("The name comes from the matched channel caption, not from the "
                      f"image itself. Visual distance {dist}/64 — confidence {conf}."))
            extra = [[InlineKeyboardButton(f"{EMOJI['heart']} Collect",
                                           callback_data=f"collect:{rec['char_id']}"),
                      InlineKeyboardButton(f"{EMOJI['star']} Favorite",
                                           callback_data=f"fav:{rec['char_id']}")]]
            await safe_send(context, chat_id, text, parse_mode=ParseMode.HTML,
                            reply_markup=result_buttons(rec.get("name"), source_url=source,
                                                        extra_rows=extra),
                            reply_to_message_id=reply_to or None,
                            disable_web_page_preview=True)
            return
        await safe_send(context, chat_id,
                        result_card(name, confidence=conf, source_url=source,
                                    note=("The name comes from the matched channel caption "
                                          f"(visual distance {dist}/64, confidence {conf}).")),
                        parse_mode=ParseMode.HTML,
                        reply_markup=result_buttons(name, source_url=source),
                        reply_to_message_id=reply_to or None,
                        disable_web_page_preview=True)
        return

    lines = [f"{EMOJI['img']} <b>Visual match found</b> (distance {dist}/64, confidence {conf})",
             "",
             f"{EMOJI['no']} The matched post has no readable caption, so the "
             "<b>character name cannot be determined</b> from the image alone."]
    excerpt = build_entry_excerpt(entry)
    if excerpt:
        lines.append(f"{EMOJI['book']} Caption: <i>{safe_text(excerpt, 120)}</i>")
    if source:
        lines.append(f"{EMOJI['ch']} Source: {source}")
    if len(matches) > 1:
        lines.append("")
        lines.append(f"<i>{len(matches)} similar post(s) found. Other candidates:</i>")
        for other_dist, other in matches[1:4]:
            link = tme_link(other.get("c", 0), other.get("m", 0), SEARCH_CHANNELS)
            label = entry_name(other) or build_entry_excerpt(other) or "no caption"
            lines.append(f"• {safe_text(label, 50)} (d={other_dist})" + (f" — {link}" if link else ""))
    await safe_send(context, chat_id, "\n".join(lines), parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to or None, disable_web_page_preview=True)


# ----------------------------------------------------------------------------
# 15.7  DATABASE CHANNEL  (/getdb /removedb /dbstatus /dbscan)
# ----------------------------------------------------------------------------

async def ass_resolve_entity(chat_id):
    """Resolve a channel id with the assistant account (direct, then via dialogs)."""
    if not ASS_CLIENT:
        return None
    try:
        return await ASS_CLIENT.get_entity(int(chat_id))
    except Exception:
        pass
    try:
        async for dialog in ASS_CLIENT.iter_dialogs():
            if dialog.id == int(chat_id):
                return dialog.entity
    except Exception as exc:
        LOG.debug("dialog scan failed: %s", exc)
    return None


async def ass_channel_readable(chat_id):
    """(ok, message) — can the assistant account read that channel's history?"""
    if not ass_session_active():
        return False, "assistant account not linked — use /addass"
    entity = await ass_resolve_entity(chat_id)
    if entity is None:
        return False, "the assistant account is not a member of that channel"
    try:
        await ASS_CLIENT.get_messages(entity, limit=1)
    except Exception as exc:
        return False, f"cannot read history: {exc}"
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)
    return True, f"readable — {title}"


async def index_channel(chat_id, limit: int = SCAN_LIMIT_PER_CHAT) -> int:
    """Incrementally index images + captions of one channel (assistant account)."""
    if not ass_session_active() or not PIL_OK:
        return 0
    entity = await ass_resolve_entity(chat_id)
    if entity is None:
        raise RuntimeError("assistant account cannot resolve that channel")
    last_id = int(IMG_INDEX["last_id"].get(str(chat_id), 0) or 0)
    newest = last_id
    added = 0
    try:
        async for message in ASS_CLIENT.iter_messages(entity, limit=limit,
                                                      min_id=last_id, reverse=True):
            newest = max(newest, message.id or 0)
            is_image = bool(message.photo) or bool(
                message.document and
                str(getattr(message.document, "mime_type", "") or "").startswith("image/"))
            if not is_image:
                continue
            try:
                blob = await message.download_media(file=io.BytesIO(), thumb=-1)
            except tl_errors.FloodWaitError as exc:
                await asyncio.sleep(exc.seconds + 1)
                blob = None
            except Exception as exc:
                LOG.debug("download failed (%s): %s", message.id, exc)
                blob = None
            if not blob:
                continue
            try:
                hashes = compute_hashes(Image.open(blob))
            except Exception as exc:
                LOG.debug("hash failed (%s): %s", message.id, exc)
                continue
            caption = message.message or ""
            entry = {
                "c": chat_id,
                "m": message.id,
                "h": hashes["d"],
                "a": hashes["a"],
                "p": hashes["p"],
                "cap": caption[:400],
                "t": message.date.isoformat() if message.date else "",
                "name": entry_name_from_caption(caption),
            }
            if any(e.get("c") == chat_id and e.get("h") == entry["h"]
                   for e in IMG_INDEX["entries"][-500:]):
                continue
            IMG_INDEX["entries"].append(entry)
            added += 1
            if len(IMG_INDEX["entries"]) > IMG_INDEX_MAX:
                IMG_INDEX["entries"] = IMG_INDEX["entries"][-IMG_INDEX_MAX:]
    except tl_errors.FloodWaitError as exc:
        await asyncio.sleep(exc.seconds + 1)
    except Exception as exc:
        LOG.warning("index_channel(%s) stopped early: %s", chat_id, exc)
    IMG_INDEX["last_id"][str(chat_id)] = newest
    save_json(F_IMGIDX, IMG_INDEX)
    return added


async def cmd_getdb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/getdb -- show the configured database channel + assistant read access."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    db = get_db_channel(context)
    if not db:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} No database channel set. Use <code>/setdb -100…</code>",
                        parse_mode=ParseMode.HTML)
        return
    ok, msg = await ass_channel_readable(db)
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ch']} Database channel: <code>{db}</code>\n"
                    f"{EMOJI['ok'] if ok else EMOJI['wait']} Assistant read access: "
                    f"{safe_text(msg, 160)}\n"
                    f"{EMOJI['img']} Indexed images: <b>{len(IMG_INDEX.get('entries', []))}</b>",
                    parse_mode=ParseMode.HTML)


async def cmd_removedb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removedb -- unset the database channel (local data is kept)."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    global INDEX_MSGID
    old = RUN_CFG.pop("db_channel_id", None)
    save_json(F_CFG, RUN_CFG)
    INDEX_MSGID = None
    save_users_db()
    if old:
        IMG_INDEX["last_id"].pop(str(old), None)
        save_json(F_IMGIDX, IMG_INDEX)
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} Database channel unset"
                    + (f" (was <code>{old}</code>). Local data was kept." if old else "."),
                    parse_mode=ParseMode.HTML)


async def cmd_dbstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dbstatus -- database channel, index and assistant-account status."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    db = get_db_channel(context)
    entries = IMG_INDEX.get("entries", [])
    from_db = len([e for e in entries if db and e.get("c") == db])
    named = len([e for e in entries if e.get("name")])
    lines = [
        f"{EMOJI['chart']} <b>Database channel status</b>",
        f"Database channel: <code>{db if db else 'NOT SET (/setdb)'}</code>",
        f"Assistant account: {'connected' if ass_session_active() else 'not connected'}",
        f"Telethon installed: {'yes' if TELETHON_OK else 'no (pip install telethon)'}",
        f"Local characters: <b>{len(CHARS)}</b>",
        f"Indexed images (total): <b>{len(entries)}</b>",
        f"Indexed from the DB channel: <b>{from_db}</b>",
        f"Indexed posts with a readable name: <b>{named}</b>",
        f"Scan position saved for: {len(IMG_INDEX.get('last_id', {}))} channel(s)",
        f"Search channels in scope: <b>{len(SEARCH_CHANNELS)}</b>",
    ]
    if db:
        ok, msg = await ass_channel_readable(db)
        lines.append(f"{EMOJI['ok'] if ok else EMOJI['no']} Read access: {safe_text(msg, 160)}")
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_dbscan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dbscan -- index images + captions of the configured DB channel."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    db = get_db_channel(context)
    if not db:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Set the channel first with /setdb.", parse_mode=ParseMode.HTML)
        return
    if not ass_session_active():
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Link the assistant account first: /addass", parse_mode=ParseMode.HTML)
        return
    if not PIL_OK:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Pillow is required: <code>pip install pillow</code>")
        return
    status = await safe_send(context, update.effective_chat.id,
                             f"{EMOJI['wait']} Indexing the database channel… this can take a while.")
    try:
        added = await index_channel(db)
        total = len(IMG_INDEX.get("entries", []))
        text = (f"{EMOJI['ok']} Indexed <b>{added}</b> new image(s) from the DB channel.\n"
                f"Total indexed images: <b>{total}</b>")
    except Exception as exc:
        LOG.error("dbscan failed: %s", exc)
        text = f"{EMOJI['no']} Indexing failed: {safe_text(exc, 160)}"
    if status:
        try:
            await status.delete()
        except Exception:
            pass
    await safe_send(context, update.effective_chat.id, text, parse_mode=ParseMode.HTML)

# ----------------------------------------------------------------------------
# 15.8  BACKUP & RESTORE  (local files + optional Telegram backup channel)
# ----------------------------------------------------------------------------

def backup_key() -> bytes:
    """Session-encryption key: BACKUP_KEY from config, else data/backup.key."""
    raw = (ENV.get("BACKUP_KEY") or os.environ.get("BACKUP_KEY") or "").strip()
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).digest()
    if os.path.exists(F_BACKUP_KEY):
        try:
            with open(F_BACKUP_KEY, "rb") as fh:
                stored = fh.read().strip()
            if stored:
                return hashlib.sha256(stored).digest()
        except OSError:
            pass
    stored = base64.b64encode(os.urandom(32))
    try:
        with open(F_BACKUP_KEY, "wb") as fh:
            fh.write(stored)
        os.chmod(F_BACKUP_KEY, 0o600)
    except OSError:
        pass
    return hashlib.sha256(stored).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def encrypt_blob(plaintext: str) -> str:
    """Dependency-free symmetric encryption (HMAC-SHA256 keystream + tag).
    Used for the assistant session stored inside a backup file."""
    data = (plaintext or "").encode("utf-8")
    key = backup_key()
    nonce = os.urandom(16)
    stream = _keystream(key, nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return base64.b64encode(nonce + cipher + tag).decode("ascii")


def decrypt_blob(token: str) -> str:
    raw = base64.b64decode((token or "").encode("ascii"))
    if len(raw) < 33:
        raise ValueError("backup blob too short")
    nonce, cipher, tag = raw[:16], raw[16:-16], raw[-16:]
    key = backup_key()
    expect = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expect):
        raise ValueError("integrity check failed (wrong key or corrupted data)")
    stream = _keystream(key, nonce, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")


def payload_checksum(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_backup_payload(context) -> dict:
    """Everything needed to rebuild the bot: session, channel, config, index, data."""
    session_blob = ""
    if os.path.exists(F_SESSION):
        try:
            with open(F_SESSION, "r", encoding="utf-8") as fh:
                session_blob = encrypt_blob(fh.read().strip())
        except (OSError, ValueError) as exc:
            LOG.error("session encryption failed: %s", exc)
    payload = {
        "app": "huswabot",
        "version": BACKUP_VERSION,
        "created_at": now_iso(),
        "db_channel_id": get_db_channel(context),
        "backup_channel_id": RUN_CFG.get("backup_channel_id"),
        "ass_phone": ASS_PHONE or "",
        "ass_session_enc": session_blob,
        "chars": CHARS,
        "char_msgid": CHAR_MSGID,
        "index_msgid": INDEX_MSGID,
        "users": USERS,
        "harem": HAREM,
        "favs": FAVS,
        "chats": CHATS,
        "search_channels": SEARCH_CHANNELS,
        "image_index": IMG_INDEX,
        "config": {k: v for k, v in RUN_CFG.items() if k != "backup_channel_id"},
    }
    payload["checksum"] = payload_checksum(payload)
    return payload


def verify_payload(payload) -> bool:
    """Integrity check: recompute the sha256 over everything except the checksum."""
    if not isinstance(payload, dict):
        return False
    stored = payload.get("checksum")
    if not stored:
        return False
    body = {k: v for k, v in payload.items() if k != "checksum"}
    return hmac.compare_digest(str(stored), payload_checksum(body))


def snapshot_files(tag: str = "pre_restore") -> str:
    """Save the current data so a failed restore can roll back to it."""
    try:
        payload = build_backup_payload(None)
    except Exception as exc:
        LOG.error("snapshot build failed: %s", exc)
        return ""
    payload["snapshot_tag"] = tag
    name = f"{tag}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    path = os.path.join(F_BACKUP_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        with open(F_SNAPSHOT, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except OSError as exc:
        LOG.error("snapshot failed: %s", exc)
        return ""
    return path


def apply_payload(payload: dict) -> dict:
    """Write a verified payload back to the local data files. Returns stats."""
    global CHARS, CHAR_MSGID, INDEX_MSGID, USERS, HAREM, FAVS, CHATS, RUN_CFG, IMG_INDEX, SEARCH_CHANNELS
    stats = {}
    if isinstance(payload.get("chars"), dict):
        CHARS = payload["chars"]
        save_chars()
        stats["characters"] = len(CHARS)
    if isinstance(payload.get("char_msgid"), dict):
        CHAR_MSGID = payload["char_msgid"]
    if payload.get("index_msgid"):
        INDEX_MSGID = payload["index_msgid"]
    if isinstance(payload.get("users"), dict):
        USERS = payload["users"]
        stats["users"] = len(USERS)
    if isinstance(payload.get("harem"), dict):
        HAREM = payload["harem"]
        save_json(F_HAREM, HAREM)
        stats["harem_rows"] = len(HAREM)
    if isinstance(payload.get("favs"), dict):
        FAVS = payload["favs"]
        save_json(F_FAVS, FAVS)
    if isinstance(payload.get("chats"), dict):
        CHATS = payload["chats"]
        save_json(F_CHATS, CHATS)
    if isinstance(payload.get("search_channels"), list):
        SEARCH_CHANNELS = payload["search_channels"]
        save_json(F_CHANNELS, SEARCH_CHANNELS)
    if isinstance(payload.get("image_index"), dict):
        IMG_INDEX = payload["image_index"]
        if "entries" not in IMG_INDEX or "last_id" not in IMG_INDEX:
            IMG_INDEX = {"entries": [], "last_id": {}}
        save_json(F_IMGIDX, IMG_INDEX)
        stats["indexed_images"] = len(IMG_INDEX.get("entries", []))
    if isinstance(payload.get("config"), dict):
        merged = dict(payload["config"])
        if RUN_CFG.get("backup_channel_id"):
            merged["backup_channel_id"] = RUN_CFG["backup_channel_id"]
        RUN_CFG = merged
        save_json(F_CFG, RUN_CFG)
    save_users_db()
    if payload.get("ass_session_enc"):
        try:
            with open(F_SESSION, "w", encoding="utf-8") as fh:
                fh.write(decrypt_blob(payload["ass_session_enc"]))
            os.chmod(F_SESSION, 0o600)
            stats["session"] = "restored"
        except Exception as exc:
            stats["session"] = f"not restored ({exc})"
    return stats


def save_backup_file(payload: dict) -> str:
    name = "huswabot-backup-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".json"
    path = os.path.join(F_BACKUP_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except OSError as exc:
        LOG.error("backup write failed: %s", exc)
        return ""
    return path


def local_backups() -> list:
    try:
        files = [os.path.join(F_BACKUP_DIR, f) for f in os.listdir(F_BACKUP_DIR)
                 if f.startswith("huswabot-backup-") and f.endswith(".json")]
    except OSError:
        return []
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


async def fetch_channel_backup() -> dict:
    """Newest backup document from the configured Telegram backup channel."""
    channel = RUN_CFG.get("backup_channel_id")
    if not channel:
        raise RuntimeError("no backup channel set — use /setbackup")
    if not ass_session_active():
        raise RuntimeError("the assistant account is needed to read the backup channel")
    entity = await ass_resolve_entity(channel)
    if entity is None:
        raise RuntimeError("the assistant account cannot access the backup channel")
    async for message in ASS_CLIENT.iter_messages(entity, limit=200):
        doc = getattr(message, "document", None)
        if not doc:
            continue
        filename = ""
        for attr in getattr(doc, "attributes", []) or []:
            if getattr(attr, "file_name", None):
                filename = attr.file_name
        if filename.endswith(".json") and "huswabot-backup" in filename:
            raw = await message.download_media(file=bytes)
            return json.loads(raw.decode("utf-8"))
    raise RuntimeError("no backup file found in the backup channel")


async def cmd_setbackup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setbackup -100xxxxxxxxxx -- channel that stores backup files."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    if not context.args:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Usage: <code>/setbackup -1001234567890</code>",
                        parse_mode=ParseMode.HTML)
        return
    cid = _to_int(context.args[0])
    if cid is None:
        await safe_send(context, update.effective_chat.id, f"{EMOJI['no']} Invalid channel ID.")
        return
    RUN_CFG["backup_channel_id"] = cid
    save_json(F_CFG, RUN_CFG)
    try:
        chat = await context.bot.get_chat(cid)
        note = f"\n{EMOJI['ok']} Bot access confirmed: {safe_text(chat.title or cid, 80)}"
    except Exception as exc:
        note = f"\n{EMOJI['wait']} Access not verified yet: {safe_text(exc, 120)}"
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ok']} Backup channel set to <code>{cid}</code>.{note}",
                    parse_mode=ParseMode.HTML)


async def cmd_getbackup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/getbackup -- show the current backup channel."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    ch = RUN_CFG.get("backup_channel_id")
    if not ch:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} No backup channel set. Use <code>/setbackup -100…</code>",
                        parse_mode=ParseMode.HTML)
        return
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['ch']} Backup channel: <code>{ch}</code>\n"
                    f"Local backup files: <b>{len(local_backups())}</b>",
                    parse_mode=ParseMode.HTML)


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup -- create a backup (local file + upload to the backup channel)."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    await safe_send(context, update.effective_chat.id, f"{EMOJI['wait']} Creating backup…")
    payload = build_backup_payload(context)
    path = save_backup_file(payload)
    size = _file_size(path)
    checksum = payload.get("checksum", "")
    channel = RUN_CFG.get("backup_channel_id")
    delivery = "local file only (no backup channel set)"
    if channel and path:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            await context.bot.send_document(
                channel, document=io.BytesIO(data), filename=os.path.basename(path),
                caption=(f"#HWBACKUP v{BACKUP_VERSION}\ncreated: {payload['created_at']}\n"
                         f"sha256: {checksum}"))
            delivery = "uploaded to the backup channel"
        except Exception as exc:
            LOG.error("channel backup failed: %s", exc)
            delivery = f"channel upload failed ({safe_text(exc, 100)})"
    BACKUP_STATE.update({"last_backup_at": payload["created_at"], "last_path": path,
                         "last_size": size, "last_checksum": checksum,
                         "last_delivery": delivery})
    save_json(F_BACKUP_STATE, BACKUP_STATE)
    lines = [
        f"{EMOJI['ok']} <b>Backup created</b>",
        f"File: <code>{safe_text(os.path.basename(path) if path else 'not written', 70)}</code>",
        f"Size: <b>{size}</b> bytes",
        f"Version: <b>{BACKUP_VERSION}</b>",
        f"Checksum (sha256): <code>{safe_text(checksum, 70)}</code>",
        f"Delivery: {safe_text(delivery, 120)}",
        "",
        "<i>Restore with /restore (reply to this file, or no argument for the newest backup).</i>",
    ]
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/restore -- from a replied backup file, a named local file, the newest local
    backup, or the Telegram backup channel. Verifies the checksum and can roll back."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    msg = update.effective_message
    payload = None
    source_label = ""

    if msg.reply_to_message and msg.reply_to_message.document:
        doc = msg.reply_to_message.document
        try:
            tg_file = await doc.get_file()
            buf = io.BytesIO()
            await tg_file.download_to_memory(buf)
            payload = json.loads(buf.getvalue().decode("utf-8"))
            source_label = doc.file_name or "replied file"
        except Exception as exc:
            await safe_send(context, update.effective_chat.id,
                            f"{EMOJI['no']} Could not read that file: {safe_text(exc, 140)}")
            return
    elif context.args:
        target = context.args[0]
        for path in local_backups():
            base = os.path.basename(path)
            if base == target or target in base:
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        payload = json.load(fh)
                    source_label = base
                except (OSError, ValueError) as exc:
                    await safe_send(context, update.effective_chat.id,
                                    f"{EMOJI['no']} Could not read {safe_text(base, 60)}: {exc}")
                    return
                break
        if payload is None:
            await safe_send(context, update.effective_chat.id,
                            f"{EMOJI['no']} No local backup matches that name. "
                            "Use /backupstatus to list them.")
            return
    else:
        files = local_backups()
        if files:
            try:
                with open(files[0], "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                source_label = os.path.basename(files[0]) + " (newest local)"
            except (OSError, ValueError) as exc:
                await safe_send(context, update.effective_chat.id,
                                f"{EMOJI['no']} Could not read the newest local backup: {exc}")
                return
        else:
            try:
                payload = await fetch_channel_backup()
                source_label = "Telegram backup channel"
            except Exception as exc:
                await safe_send(context, update.effective_chat.id,
                                f"{EMOJI['no']} No local backup, and channel restore failed: "
                                f"{safe_text(exc, 160)}")
                return

    if not verify_payload(payload):
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} <b>Integrity check FAILED</b> — nothing was changed. "
                        "The file is corrupted or was made with a different backup key.",
                        parse_mode=ParseMode.HTML)
        return

    snapshot = snapshot_files("pre_restore")
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['wait']} Integrity OK. Current data snapshotted, restoring…")
    try:
        stats = apply_payload(payload)
    except Exception as exc:
        LOG.error("restore failed: %s", exc)
        rolled = "no snapshot available"
        try:
            with open(F_SNAPSHOT, "r", encoding="utf-8") as fh:
                rollback = json.load(fh)
            if verify_payload(rollback):
                apply_payload(rollback)
                rolled = "rolled back to the pre-restore snapshot"
            else:
                rolled = "rollback snapshot failed its checksum"
        except Exception as rexc:
            LOG.error("rollback failed: %s", rexc)
            rolled = f"rollback FAILED ({safe_text(rexc, 100)})"
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Restore failed: {safe_text(exc, 140)}\n{rolled}")
        return

    session_note = stats.get("session", "no session in this backup")
    if session_note == "restored" and TELETHON_OK:
        ok = await ass_start_client()
        session_note = ("restored and reloaded" if ok
                        else "restored (restart the bot to activate)")
    summary = [
        f"{EMOJI['ok']} <b>Restore finished</b>",
        f"Source: <code>{safe_text(source_label, 70)}</code>",
        f"Backup version: <b>{payload.get('version')}</b> (created {safe_text(payload.get('created_at'), 30)})",
        f"Characters: <b>{stats.get('characters', 0)}</b>",
        f"Users: <b>{stats.get('users', 0)}</b>",
        f"Indexed images: <b>{stats.get('indexed_images', 0)}</b>",
        f"Assistant session: {safe_text(session_note, 80)}",
        f"Pre-restore snapshot: <code>{safe_text(os.path.basename(snapshot) if snapshot else 'none', 70)}</code>",
    ]
    await safe_send(context, update.effective_chat.id, "\n".join(summary), parse_mode=ParseMode.HTML)


async def cmd_backupstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backupstatus -- last backup, checksum, local files and backup channel."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    files = local_backups()
    ch = RUN_CFG.get("backup_channel_id")
    if (ENV.get("BACKUP_KEY") or "").strip():
        key_src = "BACKUP_KEY from config"
    elif os.path.exists(F_BACKUP_KEY):
        key_src = "data/backup.key (auto-generated, chmod 600)"
    else:
        key_src = "not created yet (created on the first backup)"
    lines = [
        f"{EMOJI['chart']} <b>Backup status</b>",
        f"Backup version: <b>{BACKUP_VERSION}</b>",
        f"Backup channel: <code>{ch if ch else 'NOT SET (/setbackup)'}</code>",
        f"Session encryption key: {key_src}",
        f"Last backup: {safe_text(BACKUP_STATE.get('last_backup_at') or 'never', 30)}",
        f"Last checksum: <code>{safe_text(BACKUP_STATE.get('last_checksum') or '—', 70)}</code>",
        f"Last delivery: {safe_text(BACKUP_STATE.get('last_delivery') or '—', 60)}",
        f"Local backup files: <b>{len(files)}</b>",
    ]
    if files:
        lines.append("")
        lines.append("<i>Newest local files:</i>")
        for path in files[:3]:
            lines.append(f"• <code>{safe_text(os.path.basename(path), 60)}</code> "
                         f"({_file_size(path)} B)")
    lines.append("")
    lines.append("<i>Restore: /restore (reply to a backup file) or /restore with no "
                 "argument for the newest one.</i>")
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------------------
# 15.9  ASSISTANT SESSION VALIDITY  (/asscheck)
# ----------------------------------------------------------------------------

async def ass_validate_session():
    """(ok, info) — is the saved assistant session still usable?"""
    if not TELETHON_OK:
        return False, "telethon is not installed (pip install telethon)"
    if not (API_ID and API_HASH):
        return False, "API_ID / API_HASH missing in the configuration"
    if not os.path.exists(F_SESSION):
        return False, "no saved session — use /addass"
    if not ASS_CLIENT:
        started = await ass_start_client()
        if not started:
            return False, "session could not be started (expired or revoked) — use /addass again"
    try:
        if not await ASS_CLIENT.is_user_authorized():
            return False, "session is no longer authorized — use /addass again"
        me = await ASS_CLIENT.get_me()
    except Exception as exc:
        return False, f"session check error: {safe_text(exc, 120)}"
    return True, f"{getattr(me, 'first_name', '?')} (@{getattr(me, 'username', None) or 'no-username'})"


async def cmd_asscheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/asscheck -- verify the stored assistant session is still valid."""
    if not is_owner(update.effective_user.id):
        await safe_send(context, update.effective_chat.id, ERR_PERM)
        return
    ok, info = await ass_validate_session()
    db = get_db_channel(context)
    lines = [f"{EMOJI['ok'] if ok else EMOJI['no']} <b>Assistant session</b>: "
             f"{'valid' if ok else 'NOT usable'}",
             f"Detail: {safe_text(info, 160)}"]
    if ok and db:
        read_ok, read_msg = await ass_channel_readable(db)
        lines.append(f"{EMOJI['ok'] if read_ok else EMOJI['no']} DB channel read access: "
                     f"{safe_text(read_msg, 160)}")
    lines.append(f"Session file: <code>data/ass_session.txt</code>")
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)

# ----------------------------------------------------------------------------
# 15. UNKNOWN-COMMAND FALLBACK
# ----------------------------------------------------------------------------

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await register_user(update)
    await safe_send(context, update.effective_chat.id,
                    f"{EMOJI['no']} Unknown command. Use /help to see what I can do.")


# ----------------------------------------------------------------------------
# 16. STARTUP CONFIG CHECK + MAIN
# ----------------------------------------------------------------------------

def check_startup_config() -> list:
    problems = []
    if not TOKEN or not TOKEN.count(":") == 1:
        problems.append("BOT_TOKEN missing or invalid in .env")
    if OWNER_ID == 0:
        problems.append("OWNER_ID missing in .env (your numeric Telegram user ID)")
    if API_ID == 0 or not API_HASH:
        LOG.info("API_ID/API_HASH not set: assistant-account features disabled "
                 "(user login + channel image search need them).")
    return problems


async def post_init(app: Application) -> None:
    # local persistent data is already loaded by load_all() -> nothing is lost
    # on a restart; the Telegram channel remains the off-device backup.
    snapshot_files("startup")
    LOG.info("Copy-to-clipboard buttons: %s", "native" if COPY_TEXT_OK else "fallback text")
    LOG.info("pHash (imagehash) available: %s", IMAGEHASH_OK)
    if API_ID and API_HASH and TELETHON_OK and os.path.exists(F_SESSION):
        ok = await ass_start_client()
        LOG.info("Assistant account restored from session: %s", ok)
        if ok:
            valid, info = await ass_validate_session()
            LOG.info("Assistant session check: %s (%s)", valid, info)
    db = get_db_channel(None)
    LOG.info("Bot started. DB channel: %s | Backup channel: %s | Characters: %d | "
             "Users: %d | Owner: %s",
             db, RUN_CFG.get("backup_channel_id"), len(CHARS), len(USERS), OWNER_ID)


def main() -> None:
    load_all()
    problems = check_startup_config()
    for p in problems:
        LOG.error("CONFIG PROBLEM: %s", p)
    if not TOKEN or TOKEN.count(":") != 1:
        print("FATAL: put BOT_TOKEN=your_token in the .env file next to bot.py")
        sys.exit(1)

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # --- user commands (slash) ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["wa", "op", "search", "find"], handle_wa))
    app.add_handler(CommandHandler("harem", cmd_harem))
    app.add_handler(CommandHandler("fav", cmd_fav))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("ctop", cmd_ctop))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # --- admin commands (owner only, enforced inside each handler) ---
    app.add_handler(CommandHandler("setdb", cmd_setdb))
    app.add_handler(CommandHandler("addchar", cmd_addchar))
    app.add_handler(CommandHandler("editchar", cmd_editchar))
    app.add_handler(CommandHandler("delchar", cmd_delchar))
    app.add_handler(CommandHandler("listchar", cmd_listchar))
    app.add_handler(CommandHandler("dbcheck", cmd_dbcheck))
    app.add_handler(CommandHandler("recover", cmd_recover))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # --- assistant account + channel search ---
    app.add_handler(CommandHandler("addass", cmd_addass))
    app.add_handler(CommandHandler("myass", cmd_myass))
    app.add_handler(CommandHandler("removeass", cmd_removeass))
    app.add_handler(CommandHandler("assstatus", cmd_assstatus))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("searchstatus", cmd_searchstatus))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # --- database channel management (owner only) ---
    app.add_handler(CommandHandler("getdb", cmd_getdb))
    app.add_handler(CommandHandler("removedb", cmd_removedb))
    app.add_handler(CommandHandler("dbstatus", cmd_dbstatus))
    app.add_handler(CommandHandler("dbscan", cmd_dbscan))

    # --- backup / restore (owner only) ---
    app.add_handler(CommandHandler("setbackup", cmd_setbackup))
    app.add_handler(CommandHandler("getbackup", cmd_getbackup))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("backupstatus", cmd_backupstatus))
    app.add_handler(CommandHandler("asscheck", cmd_asscheck))

    # --- callback buttons ---
    app.add_handler(CallbackQueryHandler(on_pick_button, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(on_action_button, pattern=r"^(collect|fav):"))
    app.add_handler(CallbackQueryHandler(on_page_button, pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_copy_button, pattern=r"^cp:"))

    # --- photos sent to the bot (private) -> image search ---
    private_media = filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE)
    app.add_handler(MessageHandler(private_media, photo_message_handler))

    # --- prefix commands + login text (must come after command handlers) ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # --- unknown slash commands ---
    app.add_handler(CommandHandler(["about", "settings"], unknown_command))

    LOG.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
