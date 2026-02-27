#!/usr/bin/env python3
"""
Daily News Digest Telegram Bot v3
- Settings: delivery times, topics, news count — all editable from chat
- Only GeoPolitics by default (user can add more)
- Telugu translation + audio playback
"""

import os, re, html, json, logging, tempfile, feedparser, httpx
from datetime import datetime, time as dtime
from gtts import gTTS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Env ───────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = int(os.environ["TELEGRAM_CHAT_ID"])
NEWS_API_KEY       = os.environ["NEWS_API_KEY"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
claude             = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def h(t): return html.escape(str(t))

# ══════════════════════════════════════════════════════════════════════════════
#  TOPIC LIBRARY  — all available topics the user can choose from
# ══════════════════════════════════════════════════════════════════════════════
TOPIC_LIBRARY = {
    "geopolitics": {
        "emoji": "🌍", "label": "GeoPolitics",
        "rss": [
            "https://feeds.reuters.com/reuters/worldNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
        "newsapi_q": "geopolitics international relations world affairs",
    },
    "ai_tech": {
        "emoji": "🤖", "label": "AI Tech",
        "rss": [
            "https://techcrunch.com/category/artificial-intelligence/feed/",
            "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
            "https://feeds.feedburner.com/venturebeat/SZYF",
        ],
        "newsapi_q": "artificial intelligence technology",
    },
    "finance": {
        "emoji": "💰", "label": "Finance",
        "rss": [
            "https://feeds.reuters.com/reuters/businessNews",
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",
            "https://feeds.finance.yahoo.com/rss/2.0/headline",
        ],
        "newsapi_q": "finance markets economy",
    },
    "crypto": {
        "emoji": "₿", "label": "Crypto",
        "rss": [
            "https://cointelegraph.com/rss",
            "https://coindesk.com/arc/outboundfeeds/rss/",
            "https://decrypt.co/feed",
        ],
        "newsapi_q": "cryptocurrency bitcoin ethereum blockchain",
    },
    "india": {
        "emoji": "🇮🇳", "label": "India News",
        "rss": [
            "https://feeds.feedburner.com/ndtvnews-top-stories",
            "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        ],
        "newsapi_q": "India news today",
    },
    "sports": {
        "emoji": "🏆", "label": "Sports",
        "rss": [
            "https://feeds.bbci.co.uk/sport/rss.xml",
            "https://www.espn.com/espn/rss/news",
        ],
        "newsapi_q": "sports news today",
    },
    "startups": {
        "emoji": "🚀", "label": "Startups",
        "rss": [
            "https://techcrunch.com/category/startups/feed/",
            "https://feeds.feedburner.com/venturebeat/SZYF",
        ],
        "newsapi_q": "startup funding venture capital",
    },
}

# ══════════════════════════════════════════════════════════════════════════════
#  USER SETTINGS  (defaults — only GeoPolitics, 7 AM IST, 5 stories)
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_SETTINGS = {
    "delivery_times": ["07:00"],       # IST times, max 2
    "active_topics":  ["geopolitics"], # keys from TOPIC_LIBRARY
    "news_count":     5,               # stories per topic (1-10)
}

# Runtime settings (shared across all chats for this single-user bot)
settings: dict = dict(DEFAULT_SETTINGS)

# ── In-memory state ───────────────────────────────────────────────────────────
todays_digest:        dict            = {}
conversation_history: dict[int, list] = {}
last_reply:           dict[int, str]  = {}


# ══════════════════════════════════════════════════════════════════════════════
#  FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_rss(urls: list[str], max_total: int) -> list[dict]:
    articles = []
    for url in urls:
        if len(articles) >= max_total:
            break
        try:
            for e in feedparser.parse(url).entries:
                if len(articles) >= max_total:
                    break
                t, l = e.get("title","").strip(), e.get("link","").strip()
                if t and l:
                    articles.append({"title": t, "link": l, "summary": e.get("summary","")[:400]})
        except Exception as ex:
            logger.warning(f"RSS {url}: {ex}")
    return articles


async def fetch_newsapi(query: str, max_items: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://newsapi.org/v2/everything", params={
                "q": query, "apiKey": NEWS_API_KEY,
                "pageSize": max_items, "language": "en", "sortBy": "publishedAt",
            })
        return [
            {"title": a["title"].strip(), "link": a["url"],
             "summary": (a.get("description") or "")[:400]}
            for a in r.json().get("articles", [])[:max_items]
            if a.get("title") and "[Removed]" not in a.get("title","")
        ]
    except Exception as ex:
        logger.warning(f"NewsAPI '{query}': {ex}")
        return []


async def fetch_section(key: str) -> list[dict]:
    cfg   = TOPIC_LIBRARY[key]
    count = settings["news_count"]
    arts  = fetch_rss(cfg["rss"], count)
    if len(arts) < 3:
        seen = {a["title"] for a in arts}
        for a in await fetch_newsapi(cfg["newsapi_q"], count):
            if a["title"] not in seen:
                arts.append(a); seen.add(a["title"])
    return arts[:count]


# ══════════════════════════════════════════════════════════════════════════════
#  TELUGU TRANSLATION + SECTION BUILDER
# ══════════════════════════════════════════════════════════════════════════════

async def build_telugu_section(key: str, articles: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    cfg     = TOPIC_LIBRARY[key]
    label   = cfg["label"].upper()
    divider = "―" * 22
    english = "\n".join(f"{i}. {a['title']}" for i, a in enumerate(articles, 1))

    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=800,
            messages=[{"role": "user", "content":
                f"Translate these news headlines to Telugu. "
                f"Reply with ONLY the numbered Telugu translations. "
                f"Keep company names and people names in English.\n\n{english}"
            }],
        )
        raw    = resp.content[0].text.strip()
        titles = [re.sub(r"^\d+[\.\)\:\-]\s*","", l).strip()
                  for l in raw.split("\n") if l.strip()]
        while len(titles) < len(articles):
            titles.append(articles[len(titles)]["title"])
        titles = titles[:len(articles)]
    except Exception as ex:
        logger.error(f"Translation error {label}: {ex}")
        titles = [a["title"] for a in articles]

    lines    = "\n".join(f"{i}. {h(t)}" for i, t in enumerate(titles, 1))
    text     = f"{divider}\n{cfg['emoji']} <b>{h(label)}</b>\n{divider}\n\n{lines}"
    ask_row  = [InlineKeyboardButton(f"💬 {i}", callback_data=f"ask|{key}|{i-1}") for i in range(1, len(articles)+1)]
    link_row = [InlineKeyboardButton(f"🔗 {i}", url=articles[i-1]["link"])         for i in range(1, len(articles)+1)]
    return text, InlineKeyboardMarkup([ask_row, link_row])


# ══════════════════════════════════════════════════════════════════════════════
#  DIGEST SENDER
# ══════════════════════════════════════════════════════════════════════════════

async def send_digest(app: Application, chat_id: int):
    global todays_digest
    date_str = datetime.utcnow().strftime("%A, %d %B %Y")
    await app.bot.send_message(chat_id=chat_id, parse_mode="HTML", text=(
        f"🌅 <b>శుభోదయం!</b>\n📅 {h(date_str)}\n\n"
        f"ఈరోజు మీ ముఖ్యమైన వార్తలు ఇక్కడ ఉన్నాయి.\n"
        f"💬 వార్త గురించి అడగాలంటే నొక్కండి  |  🔗 పూర్తి వ్యాసం చదవాలంటే నొక్కండి"
    ))
    todays_digest = {}
    for key in settings["active_topics"]:
        if key not in TOPIC_LIBRARY:
            continue
        await app.bot.send_chat_action(chat_id=chat_id, action="typing")
        articles = await fetch_section(key)
        todays_digest[key] = articles
        if not articles:
            cfg = TOPIC_LIBRARY[key]
            await app.bot.send_message(chat_id=chat_id, parse_mode="HTML",
                text=f"{cfg['emoji']} <b>{h(cfg['label'].upper())}</b>\n\n<i>నేడు వార్తలు అందుబాటులో లేవు.</i>")
            continue
        text, kb = await build_telugu_section(key, articles)
        await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                   reply_markup=kb, disable_web_page_preview=True)
    await app.bot.send_message(chat_id=chat_id, parse_mode="HTML",
        text="✅ <b>ఈరోజు వార్తలు పూర్తయ్యాయి!</b>\n\nఏదైనా ప్రశ్న అడగాలంటే నేరుగా టైప్ చేయండి.")


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS UI
# ══════════════════════════════════════════════════════════════════════════════

def settings_main_kb() -> InlineKeyboardMarkup:
    t1 = settings["delivery_times"][0] if len(settings["delivery_times"]) > 0 else "—"
    t2 = settings["delivery_times"][1] if len(settings["delivery_times"]) > 1 else "—"
    topics_str = ", ".join(TOPIC_LIBRARY[k]["label"] for k in settings["active_topics"] if k in TOPIC_LIBRARY) or "None"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏰ Delivery Times: {t1}  {t2}", callback_data="set_times_menu")],
        [InlineKeyboardButton(f"📋 Topics: {topics_str}",        callback_data="set_topics_menu")],
        [InlineKeyboardButton(f"🔢 News per Topic: {settings['news_count']}", callback_data="set_count_menu")],
        [InlineKeyboardButton("❌ Close",                         callback_data="set_close")],
    ])


def settings_text() -> str:
    t = settings["delivery_times"]
    times_str = "  &  ".join(t) if t else "Not set"
    topics_str = "\n".join(
        f"  {TOPIC_LIBRARY[k]['emoji']} {TOPIC_LIBRARY[k]['label']}"
        for k in settings["active_topics"] if k in TOPIC_LIBRARY
    ) or "  None"
    return (
        f"⚙️ <b>Settings</b>\n\n"
        f"⏰ <b>Delivery Times (IST):</b> {h(times_str)}\n"
        f"📋 <b>Active Topics:</b>\n{topics_str}\n"
        f"🔢 <b>News per Topic:</b> {settings['news_count']}\n"
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb())


# ── Times menu ────────────────────────────────────────────────────────────────

def times_menu_kb() -> InlineKeyboardMarkup:
    t = settings["delivery_times"]
    t1 = t[0] if len(t) > 0 else "Not set"
    t2 = t[1] if len(t) > 1 else "Not set"
    rows = [
        [InlineKeyboardButton(f"✏️ Edit Time 1: {t1}", callback_data="edit_time|0")],
    ]
    if len(t) < 2:
        rows.append([InlineKeyboardButton("➕ Add 2nd delivery time", callback_data="add_time")])
    else:
        rows.append([InlineKeyboardButton(f"✏️ Edit Time 2: {t2}", callback_data="edit_time|1")])
        rows.append([InlineKeyboardButton("🗑 Remove 2nd delivery time",  callback_data="remove_time")])
    rows.append([InlineKeyboardButton("« Back", callback_data="set_back")])
    return InlineKeyboardMarkup(rows)


# ── Topics menu ───────────────────────────────────────────────────────────────

def topics_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, cfg in TOPIC_LIBRARY.items():
        active = key in settings["active_topics"]
        icon   = "✅" if active else "➕"
        action = f"topic_remove|{key}" if active else f"topic_add|{key}"
        rows.append([InlineKeyboardButton(f"{icon} {cfg['emoji']} {cfg['label']}", callback_data=action)])
    rows.append([InlineKeyboardButton("« Back", callback_data="set_back")])
    return InlineKeyboardMarkup(rows)


# ── Count menu ────────────────────────────────────────────────────────────────

def count_menu_kb() -> InlineKeyboardMarkup:
    counts = [3, 4, 5, 6, 7, 8, 10]
    cur    = settings["news_count"]
    row    = [InlineKeyboardButton(f"{'✅ ' if c == cur else ''}{c}", callback_data=f"set_count|{c}") for c in counts]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("« Back", callback_data="set_back")]])


# ── Master callback handler for settings ──────────────────────────────────────

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    # ── Close ──
    if data == "set_close":
        await q.message.delete()
        return

    # ── Back to main settings ──
    if data == "set_back":
        await q.message.edit_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb())
        return

    # ── Times menu ──
    if data == "set_times_menu":
        await q.message.edit_text(
            "⏰ <b>Delivery Times</b>\n\nChoose which time to edit (IST, 24h format):",
            parse_mode="HTML", reply_markup=times_menu_kb())
        return

    if data == "add_time":
        if len(settings["delivery_times"]) < 2:
            settings["delivery_times"].append("18:00")
        await q.message.edit_text(
            "⏰ <b>Delivery Times</b>\n\nChoose which time to edit (IST, 24h format):",
            parse_mode="HTML", reply_markup=times_menu_kb())
        return

    if data == "remove_time":
        if len(settings["delivery_times"]) > 1:
            settings["delivery_times"].pop(1)
        await q.message.edit_text(
            "⏰ <b>Delivery Times</b>\n\nChoose which time to edit (IST, 24h format):",
            parse_mode="HTML", reply_markup=times_menu_kb())
        return

    if data.startswith("edit_time|"):
        idx = int(data.split("|")[1])
        context.user_data["editing_time_idx"] = idx
        cur = settings["delivery_times"][idx] if idx < len(settings["delivery_times"]) else "07:00"
        await q.message.reply_text(
            f"⏰ Enter new time for delivery {idx+1} in <b>HH:MM</b> format (IST, 24h).\n"
            f"Example: <code>06:00</code> for 6 AM, <code>18:30</code> for 6:30 PM\n\n"
            f"Current: <b>{cur}</b>",
            parse_mode="HTML")
        return

    # ── Topics menu ──
    if data == "set_topics_menu":
        await q.message.edit_text(
            "📋 <b>Topics</b>\n\n✅ = active (tap to remove)  |  ➕ = inactive (tap to add)",
            parse_mode="HTML", reply_markup=topics_menu_kb())
        return

    if data.startswith("topic_add|"):
        key = data.split("|")[1]
        if key not in settings["active_topics"]:
            settings["active_topics"].append(key)
        await q.message.edit_text(
            "📋 <b>Topics</b>\n\n✅ = active (tap to remove)  |  ➕ = inactive (tap to add)",
            parse_mode="HTML", reply_markup=topics_menu_kb())
        return

    if data.startswith("topic_remove|"):
        key = data.split("|")[1]
        if key in settings["active_topics"] and len(settings["active_topics"]) > 1:
            settings["active_topics"].remove(key)
        elif len(settings["active_topics"]) <= 1:
            await q.answer("కనీసం ఒక topic ఉండాలి!", show_alert=True)
        await q.message.edit_text(
            "📋 <b>Topics</b>\n\n✅ = active (tap to remove)  |  ➕ = inactive (tap to add)",
            parse_mode="HTML", reply_markup=topics_menu_kb())
        return

    # ── Count menu ──
    if data == "set_count_menu":
        await q.message.edit_text(
            f"🔢 <b>News per Topic</b>\n\nCurrently: <b>{settings['news_count']}</b>\nSelect new count:",
            parse_mode="HTML", reply_markup=count_menu_kb())
        return

    if data.startswith("set_count|"):
        settings["news_count"] = int(data.split("|")[1])
        await q.message.edit_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb())
        return


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def ist_to_utc(ist_str: str) -> dtime:
    """Convert 'HH:MM' IST to UTC time object (IST = UTC+5:30)."""
    hh, mm = map(int, ist_str.split(":"))
    total  = hh * 60 + mm - 330        # subtract 5h30m
    total  = total % (24 * 60)          # wrap around midnight
    return dtime(hour=total // 60, minute=total % 60)


def reschedule_jobs(app: Application):
    """Remove existing digest jobs and recreate from current settings."""
    jq = app.job_queue
    for job in jq.get_jobs_by_name("daily_digest"):
        job.schedule_removal()
    for ist_time in settings["delivery_times"]:
        try:
            utc_t = ist_to_utc(ist_time)
            jq.run_daily(scheduled_digest, time=utc_t, name="daily_digest")
            logger.info(f"Scheduled digest at {ist_time} IST ({utc_t} UTC)")
        except Exception as ex:
            logger.error(f"Schedule error for {ist_time}: {ex}")


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE CHAT
# ══════════════════════════════════════════════════════════════════════════════

def build_system_prompt() -> str:
    ctx = ""
    for key, arts in todays_digest.items():
        cfg = TOPIC_LIBRARY.get(key, {})
        ctx += f"\n## {cfg.get('emoji','')} {cfg.get('label','')}\n"
        for i, a in enumerate(arts, 1):
            ctx += f"{i}. {a['title']}\n   {a['summary']}\n"
    return (
        "మీరు ఒక తెలివైన, సంక్షిప్త AI వార్తల విశ్లేషకుడు. "
        "వినియోగదారుడు నేటి వార్తల సారాంశం చదివారు. "
        "తెలుగులో 3-5 వాక్యాల విశ్లేషణ ఇవ్వండి. "
        "సంస్థల పేర్లు, వ్యక్తుల పేర్లు ఆంగ్లంలోనే ఉంచండి.\n\n"
        f"నేటి వార్తలు:\n{ctx}"
    )


async def ask_claude(chat_id: int, message: str) -> str:
    conversation_history.setdefault(chat_id, [])
    conversation_history[chat_id].append({"role": "user", "content": message})
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=400,
            system=build_system_prompt(),
            messages=conversation_history[chat_id][-20:],
        )
        reply = resp.content[0].text.strip()
        conversation_history[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as ex:
        logger.error(f"Claude: {ex}")
        return "క్షమించండి, మళ్ళీ ప్రయత్నించండి."


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    times   = "  &  ".join(settings["delivery_times"])
    topics  = ", ".join(TOPIC_LIBRARY[k]["label"] for k in settings["active_topics"] if k in TOPIC_LIBRARY)
    await update.message.reply_text(
        f"👋 <b>వార్తల Bot కి స్వాగతం!</b>\n\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
        f"⏰ Delivery: <b>{h(times)} IST</b>\n"
        f"📋 Topics: <b>{h(topics)}</b>\n"
        f"🔢 Stories each: <b>{settings['news_count']}</b>\n\n"
        f"<b>Commands:</b>\n"
        f"/digest — ఇప్పుడే వార్తలు చూడండి\n"
        f"/settings — అన్ని సెట్టింగ్స్ మార్చండి\n"
        f"/clear — chat history క్లియర్ చేయండి\n"
        f"/help — ఈ సందేశం చూడండి",
        parse_mode="HTML"
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ వార్తలు తీసుకొస్తున్నాను... సుమారు 30 సెకన్లు!")
    await send_digest(context.application, update.effective_chat.id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_history[update.effective_chat.id] = []
    await update.message.reply_text("🧹 Chat history క్లియర్ అయింది!")


async def send_reply_with_audio_btn(update: Update, chat_id: int, reply: str):
    last_reply[chat_id] = reply
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔊 తెలుగులో వినండి", callback_data="tts")]])
    await update.message.reply_text(reply, reply_markup=kb)


async def handle_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    chat_id = update.effective_chat.id
    await q.answer("🔊 ఆడియో తయారవుతోంది...")
    text = last_reply.get(chat_id)
    if not text:
        await q.message.reply_text("మళ్ళీ ప్రశ్న అడగండి, తర్వాత 🔊 నొక్కండి.")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
    try:
        tts = gTTS(text=text, lang="te", slow=False)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tts.save(f.name); tmp = f.name
        with open(tmp, "rb") as audio:
            await context.bot.send_voice(chat_id=chat_id, voice=audio)
        os.unlink(tmp)
    except Exception as ex:
        logger.error(f"TTS: {ex}")
        await q.message.reply_text("ఆడియో తయారు చేయడంలో సమస్య వచ్చింది.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_chat.id
    user_text = update.message.text
    pending   = context.user_data.get("pending_story")
    editing   = context.user_data.get("editing_time_idx")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # ── Editing a delivery time ──
    if editing is not None:
        context.user_data.pop("editing_time_idx")
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", user_text.strip())
        if not m:
            await update.message.reply_text(
                "❌ Format తప్పు. <code>HH:MM</code> format లో ఇవ్వండి, ఉదా: <code>06:00</code>",
                parse_mode="HTML")
            return
        new_time = user_text.strip()
        while len(settings["delivery_times"]) <= editing:
            settings["delivery_times"].append("07:00")
        settings["delivery_times"][editing] = new_time
        reschedule_jobs(context.application)
        await update.message.reply_text(
            f"✅ Delivery time {editing+1} → <b>{new_time} IST</b> గా మార్చబడింది!\n"
            f"రేపటి నుండి అమలు అవుతుంది.",
            parse_mode="HTML")
        await update.message.reply_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb())
        return

    # ── Follow-up on a story ──
    if pending:
        section_key = pending["section_key"]
        idx         = pending["idx"]
        context.user_data.pop("pending_story")
        article = todays_digest.get(section_key, [])[idx] if todays_digest else None
        cfg     = TOPIC_LIBRARY.get(section_key, {})
        if article:
            prompt = (
                f"The user is asking about this news story:\n"
                f"Section: {cfg.get('label','')}\n"
                f"Title: {article['title']}\n"
                f"Summary: {article['summary']}\n\n"
                f"User's question: {user_text}\n\n"
                f"Answer in Telugu in 3-5 sentences. Keep company/people names in English."
            )
            reply = await ask_claude(chat_id, prompt)
        else:
            reply = await ask_claude(chat_id, user_text)
        await send_reply_with_audio_btn(update, chat_id, reply)
        return

    # ── Normal question ──
    reply = await ask_claude(chat_id, user_text)
    await send_reply_with_audio_btn(update, chat_id, reply)


async def handle_story_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, section_key, idx_str = q.data.split("|")
        idx     = int(idx_str)
        article = todays_digest.get(section_key, [])[idx]
    except (ValueError, IndexError):
        await q.message.reply_text("వార్త కనుగొనలేదు. /digest తో మళ్ళీ ప్రయత్నించండి.")
        return
    cfg = TOPIC_LIBRARY.get(section_key, {})
    context.user_data["pending_story"] = {"section_key": section_key, "idx": idx}
    await q.message.reply_text(
        f"📌 <b>{h(cfg.get('emoji',''))} {h(cfg.get('label',''))} #{idx+1}</b>\n"
        f"<i>{h(article['title'])}</i>\n\n"
        f"❓ ఈ వార్త గురించి మీ ప్రశ్న అడగండి — నేను తెలుగులో జవాబిస్తాను.",
        parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER + MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE):
    await send_digest(context.application, TELEGRAM_CHAT_ID)


async def post_init(app: Application):
    await app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Webhook cleared.")
    reschedule_jobs(app)


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_start))
    app.add_handler(CommandHandler("digest",   cmd_digest))
    app.add_handler(CommandHandler("clear",    cmd_clear))
    app.add_handler(CommandHandler("settings", cmd_settings))

    # Callback handlers — order matters: most specific patterns first
    app.add_handler(CallbackQueryHandler(handle_tts,               pattern="^tts$"))
    app.add_handler(CallbackQueryHandler(handle_story_callback,    pattern="^ask\\|"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern="^set_|^topic_|^edit_time|^add_time|^remove_time|^set_count"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 News Digest Bot v3 running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
