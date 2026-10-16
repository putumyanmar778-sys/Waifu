# Offline tests for bot.py helpers (no network, no Telegram calls)
import asyncio
import io
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Stub telegram/telethon so we can import bot.py without those packages if needed
for mod in ("telegram", "telegram.constants", "telegram.error", "telegram.ext"):
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            sys.modules[mod] = types.ModuleType(mod)
try:
    import telegram.ext  # noqa
except Exception:
    pass

# Ensure fake modules have needed attributes before bot import
import importlib

def ensure(modpath, **attrs):
    if modpath in sys.modules:
        for k, v in attrs.items():
            setattr(sys.modules[modpath], k, v, )
    else:
        m = types.ModuleType(modpath)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[modpath] = m

try:
    import telegram
    telegram.InlineKeyboardButton = getattr(telegram, "InlineKeyboardButton", object)
    telegram.InlineKeyboardMarkup = getattr(telegram, "InlineKeyboardMarkup", object)
    telegram.Update = getattr(telegram, "Update", object)
    telegram.constants.ChatType = getattr(telegram.constants, "ChatType", types.SimpleNamespace(PRIVATE="private", GROUP="group", SUPERGROUP="supergroup"))
    telegram.constants.ParseMode = getattr(telegram.constants, "ParseMode", types.SimpleNamespace(HTML="html"))
    telegram.error.BadRequest = getattr(telegram.error, "BadRequest", Exception)
    telegram.error.Forbidden = getattr(telegram.error, "Forbidden", Exception)
    telegram.error.RetryAfter = getattr(telegram.error, "RetryAfter", Exception)
    telegram.error.TimedOut = getattr(telegram.error, "TimedOut", Exception)
    telegram.ext.Application = getattr(telegram.ext, "Application", object)
    telegram.ext.ApplicationBuilder = getattr(telegram.ext, "ApplicationBuilder", object)
    telegram.ext.CallbackQueryHandler = getattr(telegram.ext, "CallbackQueryHandler", object)
    telegram.ext.ContextTypes = getattr(telegram.ext, "ContextTypes", types.SimpleNamespace(DEFAULT_TYPE=object))
    telegram.ext.MessageHandler = getattr(telegram.ext, "MessageHandler", object)
    telegram.ext.filters = getattr(telegram.ext, "filters", types.SimpleNamespace())
except ImportError:
    ensure("telegram", InlineKeyboardButton=object, InlineKeyboardMarkup=object, Update=object)
    ensure("telegram.constants", ChatType=types.SimpleNamespace(PRIVATE="private"), ParseMode=types.SimpleNamespace(HTML="html"))
    ensure("telegram.error", BadRequest=Exception, Forbidden=Exception, RetryAfter=Exception, TimedOut=Exception)
    ensure("telegram.ext", Application=object, ApplicationBuilder=object,
           CallbackQueryHandler=object, ContextTypes=types.SimpleNamespace(DEFAULT_TYPE=object),
           CommandHandler=object, MessageHandler=object, filters=types.SimpleNamespace())

import bot  # noqa: E402

fails = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)

# --- 1. owner check -----------------------------------------------------------
bot.OWNER_ID = 7738726467
check("owner id accepted", bot.is_owner(7738726467))
check("owner id rejected (other)", not bot.is_owner(12345))
check("owner id rejected (string junk)", not bot.is_owner("abc"))

# --- 2. prefix parser (text_router internals -> extract_query equivalent) -----
# emulate: ".wa zani" split
text = ".wa zani"
parts = text.split(" ", 1)
check("prefix cmd detected", parts[0].lower() in bot.PREFIX_ALIASES)
check("prefix arg extracted", parts[1] == "zani")
text2 = ".op  Minami   Kotori "
p2 = text2.split(" ", 1)
check("prefix arg with spaces", p2[1].strip() == "Minami   Kotori")

# --- 3. normalize + find_characters (case-insensitive, alias aware) -----------
bot.CHARS = {
    "C00001": {"char_id": "C00001", "name": "Zani", "anime": "Wuthering Waves",
               "aliases": ["zani-ww", "Zani Chan"], "category": "waifu", "rarity": "SSR"},
    "C00002": {"char_id": "C00002", "name": "Minami Kotori", "anime": "Love Live!",
               "aliases": ["kotori"], "category": "waifu", "rarity": "SR"},
}
exact, partial = bot.find_characters("ZANI")
check("case-insensitive exact match", len(exact) == 1 and exact[0]["char_id"] == "C00001")
exact2, _ = bot.find_characters("kotori")
check("alias match", len(exact2) == 1 and exact2[0]["char_id"] == "C00002")
exact3, partial3 = bot.find_characters("zani ch")
check("partial match", exact3 == [] and len(partial3) == 1)
exact4, _ = bot.find_characters("zzzz")
check("no match", exact4 == [])

# --- 4. addchar parser --------------------------------------------------------
rec = bot.parse_addchar_args("Zani | Wuthering Waves | zani-ww, zz | waifu | SSR")
check("addchar parse name", rec["name"] == "Zani")
check("addchar parse anime", rec["anime"] == "Wuthering Waves")
check("addchar parse aliases", rec["aliases"] == ["zani-ww", "zz"])
check("addchar parse rarity", rec["rarity"] == "SSR")
check("addchar rejects empty", bot.parse_addchar_args("") is None)

# --- 5. dHash round-trip + similarity -----------------------------------------
from PIL import Image
if bot.PIL_OK:
    # Deterministic images: horizontal white->black gradient, and a vertical one
    img1 = Image.new("L", (100, 100))
    for x in range(100):
        v = 255 - int(255 * x / 99)
        for y in range(100):
            img1.putpixel((x, y), v)
    h1 = bot.dhash(img1)
    check("dhash stable", h1 == bot.dhash(img1))
    h2 = bot.dhash(img1.transpose(Image.FLIP_LEFT_RIGHT))
    check("dhash inverted for mirror (expected)", bot.hamming(h1, h2) == 64)
    # similar image = same gradient with slight noise
    img2 = img1.copy()
    for i in range(30):
        img2.putpixel((i, 0), max(0, img1.getpixel((i, 0)) - 3))
    h2b = bot.dhash(img2)
    check("dhash distance small for similar", bot.hamming(h1, h2b) <= bot.DHASH_THRESHOLD)
    img3 = Image.new("L", (100, 100))
    for y in range(100):
        v = 255 - int(255 * y / 99)
        for x in range(100):
            img3.putpixel((x, y), v)
    h3 = bot.dhash(img3)
    check("dhash distance big for different", bot.hamming(h1, h3) > 20)

    # index round-trip through find_by_image (async)
    bot.IMG_INDEX = {"entries": [{"c": 1, "m": 10, "h": format(h1, "016x"), "cap": "Zani | Wuthering Waves #zani", "t": ""}],
                     "last_id": {"1": 10}}
    m = asyncio.run(bot.find_by_image(img1))
    check("find_by_image hit", len(m) >= 1 and m[0][0] <= bot.DHASH_THRESHOLD)
    m2 = asyncio.run(bot.find_by_image(img3))
    check("find_by_image miss on different image", m2 == [])
    check("caption name extracted", bot.name_from_caption("Zani | Wuthering Waves #zani") == "Zani")
    check("caption first line wins", bot.name_from_caption("cute girl #Rem") == "cute girl")
    check("caption tag fallback", bot.name_from_caption("#Rem") == "Rem")
    check("caption none when empty", bot.name_from_caption("") is None)
    check("caption skips hashtags only", bot.name_from_caption("#HWDATA #anime") is None)
else:
    print("SKIP Pillow not installed")

# --- 6. recovery dedupe logic --------------------------------------------------
bot.CHARS = {}
existing = {"char_id": "C00001", "name": "Zani"}
rec_new = {"char_id": "C00002", "name": "Kotori"}
for r in (existing, existing, rec_new, rec_new):  # duplicates incoming
    if "char_id" in r and r["char_id"] not in bot.CHARS:
        bot.CHARS[r["char_id"]] = r
check("recovery dedupe", len(bot.CHARS) == 2)

# --- 7. .env loader -------------------------------------------------------------
os.makedirs("/tmp/hwtest", exist_ok=True)
with open("/tmp/hwtest/.env", "w") as fh:
    fh.write("BOT_TOKEN=123:abc\nOWNER_ID=7738726467\nAPI_ID=12345\nAPI_HASH=deadbeef\nDB_CHANNEL_ID=-1001234567890\n")
env = bot.load_env("/tmp/hwtest/.env")
check("env token", env["BOT_TOKEN"] == "123:abc")
check("env owner", env["OWNER_ID"] == "7738726467")
check("env db channel int", bot._to_int(env["DB_CHANNEL_ID"]) == -1001234567890)
check("env junk -> None", bot._to_int("abc") is None)

# --- 8. token never logged ------------------------------------------------------
class R:
    def __init__(self, msg):
        self.msg = msg
        self.args = ()
    def getMessage(self):
        return self.msg

bot.TOKEN = "123:secret"
r = R("connect failed with 123:secret")
bot.NoTokenFilter().filter(r)
check("token scrubbed from logs", r.msg == "connect failed with ***TOKEN***")

print()
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL TESTS PASSED")
