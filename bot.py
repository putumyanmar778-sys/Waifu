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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
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


def load_all() -> None:
    global CHARS, CHAR_MSGID, INDEX_MSGID, USERS, HAREM, FAVS, CHATS, RUN_CFG, IMG_INDEX, SEARCH_CHANNELS
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


DHASH_THRESHOLD = 8  # max hamming distance to call it "same image"


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
        cap = char_card(rec)
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


def char_action_kb(rec, chat_id):
    uid_key = str(chat_id) if not str(chat_id).startswith("-") else ""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{EMOJI['heart']} Collect", callback_data=f"collect:{rec['char_id']}"),
        InlineKeyboardButton(f"{EMOJI['star']} Favorite", callback_data=f"fav:{rec['char_id']}"),
    ]])

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


def extract_query(context) -> str:
    if context.args:
        return " ".join(context.args)
    text = (update.effective_message.text or "") if update.effective_message else ""
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
    query = extract_query(context)
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
    cap = char_card(rec)
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


async def find_by_image(img) -> list:
    """Return index entries visually closest to the image. dHash only tells
    'same/similar picture' -- it never proves a character name."""
    target = dhash(img)
    scored = []
    for e in IMG_INDEX.get("entries", []):
        try:
            dist = hamming(target, int(e["h"], 16))
        except (ValueError, KeyError):
            continue
        if dist <= DHASH_THRESHOLD:
            scored.append((dist, e))
    scored.sort(key=lambda x: x[0])
    return scored[:5]


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
    if not photo_msg or not photo_msg.photo:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} Send a photo, or reply to a photo with <code>.wa</code>.",
                        parse_mode=ParseMode.HTML)
        return
    wait = await safe_send(context, update.effective_chat.id,
                           f"{EMOJI['wait']} Searching image database...")
    try:
        tg_file = await photo_msg.photo[-1].get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        img = Image.open(buf)
    except Exception as exc:
        LOG.error("image download failed: %s", exc)
        await safe_send(context, update.effective_chat.id, ERR_GENERIC)
        return
    matches = await find_by_image(img)
    if wait:
        try:
            await wait.delete()
        except Exception:
            pass
    if not matches:
        await safe_send(context, update.effective_chat.id,
                        f"{EMOJI['no']} No matching image found in the indexed channels. "
                        "The owner can index more channels with /scan.",
                        parse_mode=ParseMode.HTML)
        return
    dist, entry = matches[0]
    name = name_from_caption(entry.get("cap", ""))
    lines = [f"{EMOJI['img']} <b>Visual match found</b> (distance {dist}/{64})\n"]
    if name:
        lines.append(f"{EMOJI['star']} Character (from caption): <b>{safe_text(name, 60)}</b>")
        rec_exact, _ = find_characters(name)
        if rec_exact:
            return await present_search_results(context, update.effective_chat.id, rec_exact, [])
    else:
        lines.append(f"{EMOJI['no']} The matching post has no readable caption, so the "
                     "<b>character name cannot be determined</b> from the image alone.")
    cap_excerpt = build_entry_excerpt(entry)
    if cap_excerpt:
        lines.append(f"{EMOJI['book']} Original caption: <i>{safe_text(cap_excerpt, 120)}</i>")
    link = tme_link(entry.get("c", 0), entry.get("m", 0), SEARCH_CHANNELS)
    if link:
        lines.append(f"{EMOJI['ch']} Source: {link}")
    if len(matches) > 1:
        lines.append(f"\n<i>{len(matches)} similar image(s) found; showing the closest.</i>")
    await safe_send(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True)


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

PREFIX_ALIASES = {".wa": "wa", ".op": "op"}


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

    # 2) prefix commands
    if text.startswith("."):
        parts = text.split(" ", 1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in (".wa", ".op"):
            context.args = rest.split() if rest else []
            # bare `.wa` replying to a photo -> image search
            if not rest and update.effective_message.reply_to_message:
                await handle_image_search(update, context,
                                          reply_msg=update.effective_message.reply_to_message)
                return
            await handle_wa(update, context)
            return
        # unknown prefix command: silent hint (avoid spam)
        await safe_send(context, chat.id,
                        f"{EMOJI['no']} Unknown command. Try <code>.wa name</code>, <code>.op name</code> or /help.",
                        parse_mode=ParseMode.HTML)


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
    if API_ID and API_HASH and TELETHON_OK and os.path.exists(F_SESSION):
        ok = await ass_start_client()
        LOG.info("Assistant account restored from session: %s", ok)
    db = RUN_CFG.get("db_channel_id") or ENV_DB_CHANNEL
    LOG.info("Bot started. DB channel: %s | Characters: %d | Owner: %s",
             db, len(CHARS), OWNER_ID)


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
    app.add_handler(CommandHandler(["wa", "op", "search"], handle_wa))
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

    # --- callback buttons ---
    app.add_handler(CallbackQueryHandler(on_pick_button, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(on_action_button, pattern=r"^(collect|fav):"))
    app.add_handler(CallbackQueryHandler(on_page_button, pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))

    # --- photos sent to the bot (private) -> image search ---
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_message_handler))

    # --- prefix commands + login text (must come after command handlers) ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # --- unknown slash commands ---
    app.add_handler(CommandHandler(["about", "settings"], unknown_command))

    LOG.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
