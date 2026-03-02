#!/usr/bin/env python3
"""
Daily News Digest Telegram Bot v5
Fixes:
  1. Correct 7 AM IST timing (UTC+5:30 = 01:30 UTC)
  2. Settings persisted to settings.json — survive restarts
  3. Single workflow: sends digest at startup + stays alive for interactive chat
  4. Default topics: GeoPolitics, Finance, AI Updates, Crypto
"""

import os, re, html, json, logging, tempfile, asyncio, feedparser, httpx
from datetime import datetime, time as dtime
from pathlib import Path
from gtts import gTTS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = int(os.environ["TELEGRAM_CHAT_ID"])
NEWS_API_KEY       = os.environ["NEWS_API_KEY"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
claude             = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def h(t): return html.escape(str(t))

SETTINGS_FILE = Path("settings.json")

# ══════════════════════════════════════════════════════════════════════════════
#  DEFAULT TOPICS  (4 built-in)
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_TOPICS = {
    "geopolitics": {
        "emoji": "🌍", "label": "GeoPolitics",
        "newsapi_q": "geopolitics international relations world affairs",
        "rss": [
            "https://feeds.reuters.com/reuters/worldNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
    },
    "finance": {
        "emoji": "💰", "label": "Finance",
        "newsapi_q": "finance markets economy stocks",
        "rss": [
            "https://feeds.reuters.com/reuters/businessNews",
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",
            "https://feeds.finance.yahoo.com/rss/2.0/headline",
        ],
    },
    "ai_updates": {
        "emoji": "🤖", "label": "AI Updates",
        "newsapi_q": "artificial intelligence machine learning OpenAI Google",
        "rss": [
            "https://techcrunch.com/category/artificial-intelligence/feed/",
            "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
            "https://feeds.feedburner.com/venturebeat/SZYF",
        ],
    },
    "crypto": {
        "emoji": "₿", "label": "Crypto",
        "newsapi_q": "cryptocurrency bitcoin ethereum blockchain",
        "rss": [
            "https://cointelegraph.com/rss",
            "https://coindesk.com/arc/outboundfeeds/rss/",
            "https://decrypt.co/feed",
        ],
    },
}

DEFAULT_SETTINGS = {
    "delivery_times": ["07:00"],
    "active_topics":  ["geopolitics", "finance", "ai_updates", "crypto"],
    "news_count":     5,
}

# ══════════════════════════════════════════════════════════════════════════════
#  PERSISTENT SETTINGS  — saved to settings.json, survives restarts
# ══════════════════════════════════════════════════════════════════════════════

def load_settings() -> tuple[dict, dict]:
    """Load topics + settings from file, falling back to defaults."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            loaded_topics   = data.get("topics",   DEFAULT_TOPICS.copy())
            loaded_settings = data.get("settings", DEFAULT_SETTINGS.copy())
            # Ensure all default topics exist (in case new defaults were added)
            for k, v in DEFAULT_TOPICS.items():
                if k not in loaded_topics:
                    loaded_topics[k] = v
            logger.info(f"✅ Settings loaded from {SETTINGS_FILE}")
            return loaded_topics, loaded_settings
        except Exception as ex:
            logger.warning(f"Settings load failed ({ex}), using defaults.")
    return DEFAULT_TOPICS.copy(), DEFAULT_SETTINGS.copy()


def save_settings():
    """Persist current topics + settings to file."""
    try:
        SETTINGS_FILE.write_text(json.dumps(
            {"topics": topics, "settings": settings}, indent=2, ensure_ascii=False
        ))
        logger.info("💾 Settings saved.")
    except Exception as ex:
        logger.error(f"Settings save failed: {ex}")


# Load on startup
topics, settings = load_settings()

# ── In-memory state ───────────────────────────────────────────────────────────
todays_digest:        dict            = {}
conversation_history: dict[int, list] = {}
last_reply:           dict[int, str]  = {}


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE: AUTO-GENERATE TOPIC CONFIG FROM FREE TEXT
# ══════════════════════════════════════════════════════════════════════════════

async def generate_topic_config(user_phrase: str) -> dict | None:
    prompt = f"""The user wants to add a news topic: "{user_phrase}"

Return a JSON object with ONLY these keys:
- key: short snake_case id (e.g. "cricket")
- label: clean display name (e.g. "Cricket")
- emoji: one relevant emoji
- newsapi_q: NewsAPI search query (5-8 words)
- rss: array of 2-3 reliable public RSS feed URLs

Reply with ONLY raw JSON, no markdown fences."""
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", resp.content[0].text.strip())
        cfg = json.loads(raw)
        for k in ("key", "label", "emoji", "newsapi_q", "rss"):
            if k not in cfg:
                raise ValueError(f"Missing: {k}")
        return cfg
    except Exception as ex:
        logger.error(f"Topic generation failed: {ex}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_rss(urls: list, max_total: int) -> list:
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
                    articles.append({"title": t, "link": l,
                                     "summary": e.get("summary","")[:400]})
        except Exception as ex:
            logger.warning(f"RSS {url}: {ex}")
    return articles


async def fetch_newsapi(query: str, max_items: int) -> list:
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


async def fetch_section(key: str) -> list:
    cfg   = topics[key]
    count = settings["news_count"]
    arts  = fetch_rss(cfg.get("rss", []), count)
    if len(arts) < 3:
        seen = {a["title"] for a in arts}
        for a in await fetch_newsapi(cfg["newsapi_q"], count):
            if a["title"] not in seen:
                arts.append(a); seen.add(a["title"])
    return arts[:count]


# ══════════════════════════════════════════════════════════════════════════════
#  TELUGU SECTION BUILDER
# ══════════════════════════════════════════════════════════════════════════════

async def build_telugu_section(key: str, articles: list) -> tuple:
    cfg     = topics[key]
    label   = cfg["label"].upper()
    divider = "―" * 22
    english = "\n".join(f"{i}. {a['title']}" for i, a in enumerate(articles, 1))
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=800,
            messages=[{"role": "user", "content":
                f"Translate these news headlines to Telugu. "
                f"Reply with ONLY numbered Telugu translations. "
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
        logger.error(f"Translation {label}: {ex}")
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
        if key not in topics:
            continue
        await app.bot.send_chat_action(chat_id=chat_id, action="typing")
        articles = await fetch_section(key)
        todays_digest[key] = articles
        if not articles:
            cfg = topics[key]
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

def settings_text() -> str:
    t          = settings["delivery_times"]
    times_str  = "  &  ".join(t) if t else "Not set"
    topics_str = "\n".join(
        f"  {topics[k]['emoji']} {topics[k]['label']}"
        for k in settings["active_topics"] if k in topics
    ) or "  None"
    return (
        f"⚙️ <b>Settings</b>\n\n"
        f"⏰ <b>Delivery Times (IST):</b> {h(times_str)}\n"
        f"📋 <b>Active Topics:</b>\n{topics_str}\n"
        f"🔢 <b>News per Topic:</b> {settings['news_count']}\n"
    )


def settings_main_kb() -> InlineKeyboardMarkup:
    t1         = settings["delivery_times"][0] if settings["delivery_times"] else "—"
    t2         = settings["delivery_times"][1] if len(settings["delivery_times"]) > 1 else "—"
    topics_str = ", ".join(topics[k]["label"] for k in settings["active_topics"] if k in topics) or "None"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏰ Delivery Times: {t1}  {t2}", callback_data="set_times_menu")],
        [InlineKeyboardButton(f"📋 Topics: {topics_str}",        callback_data="set_topics_menu")],
        [InlineKeyboardButton(f"🔢 News per Topic: {settings['news_count']}", callback_data="set_count_menu")],
        [InlineKeyboardButton("❌ Close",                         callback_data="set_close")],
    ])


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb())


def times_menu_kb() -> InlineKeyboardMarkup:
    t  = settings["delivery_times"]
    t1 = t[0] if len(t) > 0 else "Not set"
    t2 = t[1] if len(t) > 1 else "Not set"
    rows = [[InlineKeyboardButton(f"✏️ Edit Time 1: {t1}", callback_data="edit_time|0")]]
    if len(t) < 2:
        rows.append([InlineKeyboardButton("➕ Add 2nd delivery time", callback_data="add_time")])
    else:
        rows.append([InlineKeyboardButton(f"✏️ Edit Time 2: {t2}", callback_data="edit_time|1")])
        rows.append([InlineKeyboardButton("🗑 Remove 2nd delivery time", callback_data="remove_time")])
    rows.append([InlineKeyboardButton("« Back", callback_data="set_back")])
    return InlineKeyboardMarkup(rows)


def topics_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, cfg in topics.items():
        active = key in settings["active_topics"]
        icon   = "✅" if active else "⬜"
        rows.append([InlineKeyboardButton(f"{icon} {cfg['emoji']} {cfg['label']}", callback_data=f"topic_toggle|{key}")])
        if key not in DEFAULT_TOPICS:   # only allow deleting user-added topics
            rows.append([InlineKeyboardButton(f"🗑 Remove {cfg['label']}", callback_data=f"topic_delete|{key}")])
    rows.append([InlineKeyboardButton("➕ Add new topic...", callback_data="topic_add_new")])
    rows.append([InlineKeyboardButton("« Back", callback_data="set_back")])
    return InlineKeyboardMarkup(rows)


def count_menu_kb() -> InlineKeyboardMarkup:
    counts = [3, 4, 5, 6, 7, 8, 10]
    cur    = settings["news_count"]
    row    = [InlineKeyboardButton(f"{'✅ ' if c == cur else ''}{c}", callback_data=f"set_count|{c}") for c in counts]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("« Back", callback_data="set_back")]])


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "set_close":
        await q.message.delete(); return

    if data == "set_back":
        await q.message.edit_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb()); return

    # Times
    if data == "set_times_menu":
        await q.message.edit_text("⏰ <b>Delivery Times</b>\n\nIST, 24h format:", parse_mode="HTML", reply_markup=times_menu_kb()); return

    if data == "add_time":
        if len(settings["delivery_times"]) < 2:
            settings["delivery_times"].append("18:00"); save_settings()
        await q.message.edit_text("⏰ <b>Delivery Times</b>", parse_mode="HTML", reply_markup=times_menu_kb()); return

    if data == "remove_time":
        if len(settings["delivery_times"]) > 1:
            settings["delivery_times"].pop(1); save_settings()
        await q.message.edit_text("⏰ <b>Delivery Times</b>", parse_mode="HTML", reply_markup=times_menu_kb()); return

    if data.startswith("edit_time|"):
        idx = int(data.split("|")[1])
        context.user_data["editing_time_idx"] = idx
        cur = settings["delivery_times"][idx] if idx < len(settings["delivery_times"]) else "07:00"
        await q.message.reply_text(
            f"⏰ Time {idx+1} కి కొత్త సమయం టైప్ చేయండి (<b>HH:MM</b>, 24h IST)\n"
            f"ఉదా: <code>06:00</code> లేదా <code>18:30</code>\n\nప్రస్తుతం: <b>{cur}</b>",
            parse_mode="HTML"); return

    # Topics
    if data == "set_topics_menu":
        await q.message.edit_text(
            "📋 <b>Topics</b>\n\n✅ = active  |  ⬜ = inactive (tap to toggle)",
            parse_mode="HTML", reply_markup=topics_menu_kb()); return

    if data.startswith("topic_toggle|"):
        key = data.split("|")[1]
        if key in settings["active_topics"]:
            if len(settings["active_topics"]) <= 1:
                await q.answer("కనీసం ఒక topic ఉండాలి!", show_alert=True); return
            settings["active_topics"].remove(key)
        else:
            settings["active_topics"].append(key)
        save_settings()
        await q.message.edit_text("📋 <b>Topics</b>\n\n✅ = active  |  ⬜ = inactive",
                                  parse_mode="HTML", reply_markup=topics_menu_kb()); return

    if data.startswith("topic_delete|"):
        key = data.split("|")[1]
        if key in DEFAULT_TOPICS:
            await q.answer("Default topic తొలగించలేరు.", show_alert=True); return
        if len(settings["active_topics"]) <= 1 and key in settings["active_topics"]:
            await q.answer("కనీసం ఒక topic ఉండాలి!", show_alert=True); return
        topics.pop(key, None)
        settings["active_topics"] = [k for k in settings["active_topics"] if k != key]
        save_settings()
        await q.message.edit_text("📋 <b>Topics</b>", parse_mode="HTML", reply_markup=topics_menu_kb()); return

    if data == "topic_add_new":
        context.user_data["adding_topic"] = True
        await q.message.reply_text(
            "📋 <b>కొత్త Topic జోడించండి</b>\n\n"
            "మీకు కావలసిన topic పేరు టైప్ చేయండి.\n\n"
            "ఉదాహరణలు:\n"
            "• <code>Cricket news</code>\n"
            "• <code>Indian stock market</code>\n"
            "• <code>Bollywood</code>\n"
            "• <code>Climate change</code>\n\n"
            "AI అన్నీ automatically సెటప్ చేస్తుంది! 🤖",
            parse_mode="HTML"); return

    # Count
    if data == "set_count_menu":
        await q.message.edit_text(
            f"🔢 <b>News per Topic</b>\n\nప్రస్తుతం: <b>{settings['news_count']}</b>",
            parse_mode="HTML", reply_markup=count_menu_kb()); return

    if data.startswith("set_count|"):
        settings["news_count"] = int(data.split("|")[1])
        save_settings()
        await q.message.edit_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb()); return


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULE  — 7 AM IST = 01:30 UTC
# ══════════════════════════════════════════════════════════════════════════════

def ist_to_utc(ist_str: str) -> dtime:
    hh, mm = map(int, ist_str.split(":"))
    total  = (hh * 60 + mm - 330) % (24 * 60)   # IST = UTC + 5h30m
    return dtime(hour=total // 60, minute=total % 60)


def reschedule_jobs(app: Application):
    for job in app.job_queue.get_jobs_by_name("daily_digest"):
        job.schedule_removal()
    for ist_time in settings["delivery_times"]:
        try:
            utc_t = ist_to_utc(ist_time)
            app.job_queue.run_daily(scheduled_digest, time=utc_t, name="daily_digest")
            logger.info(f"⏰ Scheduled digest: {ist_time} IST = {utc_t} UTC")
        except Exception as ex:
            logger.error(f"Schedule error {ist_time}: {ex}")


async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE):
    logger.info("📰 Sending scheduled digest...")
    await send_digest(context.application, TELEGRAM_CHAT_ID)


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE CHAT
# ══════════════════════════════════════════════════════════════════════════════

def build_system_prompt() -> str:
    ctx = ""
    for key, arts in todays_digest.items():
        cfg = topics.get(key, {})
        ctx += f"\n## {cfg.get('emoji','')} {cfg.get('label','')}\n"
        for i, a in enumerate(arts, 1):
            ctx += f"{i}. {a['title']}\n   {a['summary']}\n"
    return (
        "మీరు ఒక తెలివైన AI వార్తల విశ్లేషకుడు. "
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
    chat_id    = update.effective_chat.id
    times      = "  &  ".join(settings["delivery_times"])
    topic_list = ", ".join(topics[k]["label"] for k in settings["active_topics"] if k in topics)
    await update.message.reply_text(
        f"👋 <b>వార్తల Bot కి స్వాగతం!</b>\n\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
        f"⏰ Delivery: <b>{h(times)} IST</b>\n"
        f"📋 Topics: <b>{h(topic_list)}</b>\n"
        f"🔢 Stories each: <b>{settings['news_count']}</b>\n\n"
        f"<b>Commands:</b>\n"
        f"/digest — ఇప్పుడే వార్తలు చూడండి\n"
        f"/settings — సెట్టింగ్స్ మార్చండి\n"
        f"/clear — chat history క్లియర్ చేయండి",
        parse_mode="HTML")


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
        await q.message.reply_text("మళ్ళీ ప్రశ్న అడగండి, తర్వాత 🔊 నొక్కండి."); return
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
    user_text = update.message.text.strip()
    pending   = context.user_data.get("pending_story")
    editing   = context.user_data.get("editing_time_idx")
    adding    = context.user_data.get("adding_topic")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Adding new topic
    if adding:
        context.user_data.pop("adding_topic")
        await update.message.reply_text(
            f"🤖 <b>\"{h(user_text)}\"</b> కోసం topic తయారు చేస్తున్నాను...\n10-15 సెకన్లు పట్టవచ్చు.",
            parse_mode="HTML")
        cfg = await generate_topic_config(user_text)
        if not cfg:
            await update.message.reply_text("❌ Topic తయారు చేయడంలో సమస్య. మళ్ళీ ప్రయత్నించండి."); return
        key = cfg["key"]
        if key in topics:
            key = key + "_2"
        topics[key] = {"emoji": cfg["emoji"], "label": cfg["label"],
                       "newsapi_q": cfg["newsapi_q"], "rss": cfg.get("rss", [])}
        settings["active_topics"].append(key)
        save_settings()
        await update.message.reply_text(
            f"✅ <b>{cfg['emoji']} {h(cfg['label'])}</b> topic జోడించబడింది!\n"
            f"తదుపరి digest నుండి వస్తాయి. ఇప్పుడే చూడాలంటే /digest పంపండి.",
            parse_mode="HTML")
        await update.message.reply_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb()); return

    # Editing delivery time
    if editing is not None:
        context.user_data.pop("editing_time_idx")
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", user_text)
        if not m:
            await update.message.reply_text(
                "❌ Format తప్పు. <code>HH:MM</code> format లో ఇవ్వండి\nఉదా: <code>06:00</code>",
                parse_mode="HTML"); return
        while len(settings["delivery_times"]) <= editing:
            settings["delivery_times"].append("07:00")
        settings["delivery_times"][editing] = user_text
        save_settings()
        reschedule_jobs(context.application)
        await update.message.reply_text(
            f"✅ Delivery time {editing+1} → <b>{user_text} IST</b> గా మార్చబడింది!",
            parse_mode="HTML")
        await update.message.reply_text(settings_text(), parse_mode="HTML", reply_markup=settings_main_kb()); return

    # Story follow-up
    if pending:
        section_key = pending["section_key"]
        idx         = pending["idx"]
        context.user_data.pop("pending_story")
        article = todays_digest.get(section_key, [])[idx] if todays_digest else None
        cfg     = topics.get(section_key, {})
        if article:
            prompt = (
                f"User is asking about: {cfg.get('label','')} story #{idx+1}\n"
                f"Title: {article['title']}\nSummary: {article['summary']}\n\n"
                f"Question: {user_text}\n\nAnswer in Telugu, 3-5 sentences. Keep names in English."
            )
            reply = await ask_claude(chat_id, prompt)
        else:
            reply = await ask_claude(chat_id, user_text)
        await send_reply_with_audio_btn(update, chat_id, reply); return

    # Normal question
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
        await q.message.reply_text("వార్త కనుగొనలేదు. /digest తో మళ్ళీ ప్రయత్నించండి."); return
    cfg = topics.get(section_key, {})
    context.user_data["pending_story"] = {"section_key": section_key, "idx": idx}
    await q.message.reply_text(
        f"📌 <b>{h(cfg.get('emoji',''))} {h(cfg.get('label',''))} #{idx+1}</b>\n"
        f"<i>{h(article['title'])}</i>\n\n"
        f"❓ ఈ వార్త గురించి మీ ప్రశ్న అడగండి — నేను తెలుగులో జవాబిస్తాను.",
        parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN — single workflow, sends digest at startup then stays alive for chat
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    await app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Webhook cleared.")
    reschedule_jobs(app)
    # Send digest immediately on startup (GitHub Action triggers at 7 AM IST)
    logger.info("📰 Sending startup digest...")
    await send_digest(app, TELEGRAM_CHAT_ID)


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
    app.add_handler(CallbackQueryHandler(handle_tts,               pattern="^tts$"))
    app.add_handler(CallbackQueryHandler(handle_story_callback,    pattern="^ask\\|"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback,
                    pattern="^(set_|topic_|edit_time|add_time|remove_time|set_count)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot running — polling for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
