# Husbandos & Waifus Bot 🎌

Telegram anime-character bot — inspired by public "cheat-sheet" bots, but built from scratch.
Single-file `bot.py` + `requirements.txt` only. **No external database server** — a Telegram
Channel is the database & backup, JSON files are the working cache. Works on Termux, VPS or PC.

## ✨ Features

- 🔍 **Character search** — name, alias, case-insensitive, partial match, inline chooser for duplicates
- ⌨️ **Prefix AND slash commands** — `.wa name`, `.op name`, `/wa name`, `/op name` (same engine)
- 🖼️ **Image search** — send a photo, or reply to any photo with `.wa` / `/op` → dHash visual matching
- 📺 **Auto channel search** — an optional "assistant account" (your own Telegram user account, phone login) scans channels it joined; captions & hashtags supply character names (never invented from pixels alone)
- 💾 **Telegram Channel as database** — records posted as messages, compact pinned index, `/recover` rebuilds the local cache from the channel (duplicate-safe, logged)
- 💗 Collections, ⭐ favorites, 👤 profile, 🏆 global & group leaderboards, 📊 stats
- 🛡️ Rate limiting, owner-only admin checks (user ID, never username), token-scrubbed logs, no code execution from user input
- 📱 Termux-friendly: Pillow-only dHash (no numpy/opencv/torch), no MongoDB, no external services

## 📦 Files

```
bot.py            the entire bot (single file)
requirements.txt  dependencies
.env              YOUR secrets (token, owner id, api id/hash) — never share/commit
.env.example      template for .env
run.txt           step-by-step run guide (Burmese)
data/             created automatically: JSON cache, image index, session, recovery log
test_bot.py       offline unit tests (python3 test_bot.py)
```

## 🚀 Quick start

```bash
pip install -r requirements.txt
# edit .env — put your token & owner id
python bot.py
```

Full setup (BotFather, channel admin, API_ID/API_HASH, Termux troubleshooting) → **run.txt** (Burmese).

## 🤖 Commands

**User** — `/start` `/help` `/search` `.wa` `/wa` `.op` `/op` `/harem` `/fav` `/profile` `/top` `/ctop` `/stats`

**Owner** — `/setdb` `/addchar` `/editchar` `/delchar` `/listchar` `/dbcheck` `/recover` `/broadcast`

**Assistant account (owner only)** — `/addass` `/myass` `/removeass` `/assstatus` `/cancel` `/scan` `/channels` `/searchstatus`

Reply to any photo with `.wa` or `.op` → image search. Send a photo in DM → image search.

## 🔐 Security notes

- Revoke any token you ever pasted in a chat: @BotFather → /mybots → API Token → **Revoke**
- Assistant session is stored in `data/ass_session.txt` (chmod 600) — never upload `data/` anywhere
- Login codes / passwords are only requested in **private chat** with the bot, never in groups
- The assistant account never sends messages on its own; it only reads channels it actually joined
- Admin rights are checked against `OWNER_ID` (numeric), never against usernames

## ⚠️ Honest limitations

- Image similarity (dHash) finds *similar images*, not names. A character name is shown
  only when the matching channel post has a readable caption/hashtag. Otherwise the bot
  says "name unknown" — it will not invent one.
- Telegram's Bot API cannot read channel history. `/recover` uses the bot's own pinned
  index + record posts. To rebuild from arbitrary channel history, link an assistant
  account and use `/scan`.
- Bot must stay running (polling). On Termux use `nohup python bot.py &`.
