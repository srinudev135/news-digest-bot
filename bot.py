#!/usr/bin/env python3
"""
Daily News Digest Telegram Bot v2
- Clean UI: 4 sections, numbered stories, per-story follow-up buttons
- Sections: AI Tech | Finance | GeoPolitics | Crypto
- Powered by Claude AI for summaries and follow-up chat
- Uses HTML parse mode throughout (avoids MarkdownV2 escape headaches)
"""

import os
import re
import html
import logging
import tempfile
import feedparser
import httpx
from datetime import datetime
from gtts import gTTS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = int(os.environ["TELEGRAM_CHAT_ID"])
NEWS_API_KEY       = os.environ["NEWS_API_KEY"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── HTML helper ───────────────────────────────────────────────────────────────
def h(text: str) -> str:
    """Escape text for Telegram HTML mode."""
    return html.escape(str(text))

# ── Section definitions ───────────────────────────────────────────────────────
# Each section has an emoji, display label, RSS feeds, and a NewsAPI query
SECTIONS = {
    "ai_tech": {
        "emoji":  "🤖",
        "label":  "AI Tech",
        "rss": [
            "https://techcrunch.com/category/artificial-intelligence/feed/",
            "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
            "https://feeds.feedburner.com/venturebeat/SZYF",
        ],
        "newsapi_q": "artificial intelligence technology",
    },
    "finance": {
        "emoji":  "💰",
        "label":  "Finance",
        "rss": [
            "https://feeds.reuters.com/reuters/businessNews",
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",
            "https://feeds.finance.yahoo.com/rss/2.0/headline",
        ],
        "newsapi_q": "finance markets economy",
    },
    "geopolitics": {
        "emoji":  "🌍",
        "label":  "GeoPolitics",
        "rss": [
            "https://feeds.reuters.com/reuters/worldNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
        "newsapi_q": "geopolitics international relations world affairs",
    },
    "crypto": {
        "emoji":  "₿",
        "label":  "Crypto",
        "rss": [
            "https://cointelegraph.com/rss",
            "https://coindesk.com/arc/outboundfeeds/rss/",
            "https://decrypt.co/feed",
        ],
        "newsapi_q": "cryptocurrency bitcoin ethereum blockchain",
    },
}

# ── In-memory state ───────────────────────────────────────────────────────────
todays_digest: dict = {}                  # { section_key: [article, ...] }
conversation_history: dict[int, list] = {}
last_reply: dict[int, str] = {}           # { chat_id: last Telugu reply text }


# ══════════════════════════════════════════════════════════════════════════════
#  FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_rss_articles(urls: list[str], max_total: int = 5) -> list[dict]:
    articles = []
    for url in urls:
        if len(articles) >= max_total:
            break
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                if len(articles) >= max_total:
                    break
                title = entry.get("title", "").strip()
                link  = entry.get("link", "").strip()
                if title and link:
                    articles.append({"title": title, "link": link,
                                     "summary": entry.get("summary", "")[:400]})
        except Exception as e:
            logger.warning(f"RSS error {url}: {e}")
    return articles


async def fetch_newsapi_articles(query: str, max_items: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q":        query,
                    "apiKey":   NEWS_API_KEY,
                    "pageSize": max_items,
                    "language": "en",
                    "sortBy":   "publishedAt",
                },
            )
        data = r.json()
        return [
            {
                "title":   a.get("title", "").strip(),
                "link":    a.get("url", ""),
                "summary": (a.get("description") or "")[:400],
            }
            for a in data.get("articles", [])[:max_items]
            if a.get("title") and "[Removed]" not in a.get("title", "")
        ]
    except Exception as e:
        logger.warning(f"NewsAPI error '{query}': {e}")
        return []


async def fetch_section(key: str, cfg: dict) -> list[dict]:
    """Fetch up to 5 articles for a section, RSS first then NewsAPI fallback."""
    articles = fetch_rss_articles(cfg["rss"], max_total=5)
    if len(articles) < 3:
        api_articles = await fetch_newsapi_articles(cfg["newsapi_q"], max_items=5)
        # merge, de-duplicate by title
        existing_titles = {a["title"] for a in articles}
        for a in api_articles:
            if a["title"] not in existing_titles:
                articles.append(a)
                existing_titles.add(a["title"])
    return articles[:5]


# ══════════════════════════════════════════════════════════════════════════════
#  TELUGU TRANSLATION
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION MESSAGE BUILDER (Telugu via Claude)
# ══════════════════════════════════════════════════════════════════════════════

async def build_telugu_section(key: str, articles: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """
    Ask Claude to translate headlines to Telugu and return formatted HTML + keyboard.
    Falls back to English if translation fails.
    """
    cfg     = SECTIONS[key]
    emoji   = cfg["emoji"]
    label   = cfg["label"].upper()
    divider = "―" * 22

    # Build numbered English list for Claude
    english_lines = "\n".join(f"{i}. {a['title']}" for i, a in enumerate(articles, 1))

    # Ask Claude to translate — very simple prompt, just return numbered Telugu lines
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    f"Translate these news headlines to Telugu. "
                    f"Reply with ONLY the numbered Telugu translations. "
                    f"Keep company names and people names in English.\n\n"
                    f"{english_lines}"
                )
            }],
        )
        translated = resp.content[0].text.strip()
        logger.info(f"Telugu output for {label}:\n{translated}")

        # Parse numbered lines from Claude response
        titles = []
        for line in translated.split("\n"):
            line = line.strip()
            if line:
                cleaned = re.sub(r"^\d+[\.\)\:\-]\s*", "", line).strip()
                if cleaned:
                    titles.append(cleaned)

        # Pad with English if short
        while len(titles) < len(articles):
            titles.append(articles[len(titles)]["title"])
        titles = titles[:len(articles)]

    except Exception as e:
        logger.error(f"Translation error for {label}: {e}")
        titles = [a["title"] for a in articles]

    # Build the message
    lines = "\n".join(f"{i}. {h(t)}" for i, t in enumerate(titles, 1))
    text  = f"{divider}\n{emoji} <b>{h(label)}</b>\n{divider}\n\n{lines}"

    # Buttons
    ask_row  = [InlineKeyboardButton(f"💬 {i}", callback_data=f"ask|{key}|{i-1}") for i in range(1, len(articles)+1)]
    link_row = [InlineKeyboardButton(f"🔗 {i}", url=articles[i-1]["link"])         for i in range(1, len(articles)+1)]
    keyboard = InlineKeyboardMarkup([ask_row, link_row])

    return text, keyboard


# ══════════════════════════════════════════════════════════════════════════════
#  DIGEST SENDER
# ══════════════════════════════════════════════════════════════════════════════

async def send_digest(app: Application, chat_id: int):
    global todays_digest

    date_str = datetime.utcnow().strftime("%A, %d %B %Y")
    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🌅 <b>శుభోదయం!</b>\n"
            f"📅 {h(date_str)}\n\n"
            f"ఈరోజు మీ ముఖ్యమైన వార్తలు ఇక్కడ ఉన్నాయి.\n"
            f"ఏదైనా వార్త గురించి అడగాలంటే 💬 నొక్కండి.\n"
            f"పూర్తి వ్యాసం చదవాలంటే 🔗 నొక్కండి."
        ),
        parse_mode="HTML",
    )

    todays_digest = {}

    for key, cfg in SECTIONS.items():
        await app.bot.send_chat_action(chat_id=chat_id, action="typing")
        articles = await fetch_section(key, cfg)
        todays_digest[key] = articles

        if not articles:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"{cfg['emoji']} <b>{h(cfg['label'].upper())}</b>\n\n<i>నేడు వార్తలు అందుబాటులో లేవు.</i>",
                parse_mode="HTML",
            )
            continue

        # Build Telugu section message
        text, keyboard = await build_telugu_section(key, articles)
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    await app.bot.send_message(
        chat_id=chat_id,
        text="✅ <b>ఈరోజు వార్తలు పూర్తయ్యాయి!</b>\n\nఏదైనా ప్రశ్న అడగాలంటే నేరుగా టైప్ చేయండి — నేను తెలుగులో జవాబిస్తాను.",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE CHAT
# ══════════════════════════════════════════════════════════════════════════════

def build_system_prompt() -> str:
    context = ""
    for key, articles in todays_digest.items():
        cfg = SECTIONS[key]
        context += f"\n## {cfg['emoji']} {cfg['label']}\n"
        for i, a in enumerate(articles, 1):
            context += f"{i}. {a['title']}\n   {a['summary']}\n"
    return (
        "మీరు ఒక తెలివైన, సంక్షిప్త AI వార్తల విశ్లేషకుడు. "
        "వినియోగదారుడు నేటి వార్తల సారాంశం చదివారు. "
        "వారు ఏదైనా వార్త గురించి అడిగినప్పుడు, తెలుగులో 3-5 వాక్యాల విశ్లేషణ ఇవ్వండి: "
        "ఏమి జరిగింది, ఎందుకు ముఖ్యమైనది, మరియు తదుపరి ఏమి చూడాలి. "
        "ప్రత్యక్షంగా మరియు అంతర్దృష్టితో ఉండండి. "
        "సంస్థల పేర్లు, వ్యక్తుల పేర్లు ఆంగ్లంలోనే ఉంచండి.\n\n"
        f"నేటి వార్తలు:\n{context}"
    )


async def ask_claude(chat_id: int, message: str) -> str:
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    conversation_history[chat_id].append({"role": "user", "content": message})
    history = conversation_history[chat_id][-20:]
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system=build_system_prompt(),
            messages=history,
        )
        reply = resp.content[0].text.strip()
        conversation_history[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return "క్షమించండి, నేను దాన్ని ప్రాసెస్ చేయలేకపోయాను. దయచేసి మళ్ళీ ప్రయత్నించండి."


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 <b>Welcome to your Daily News Digest Bot!</b>\n\n"
        f"Your Chat ID: <code>{chat_id}</code>\n\n"
        f"📰 Every morning at <b>7:00 AM IST</b> I'll send you:\n\n"
        f"🤖 <b>AI Tech</b> — top 5 stories\n"
        f"💰 <b>Finance</b> — top 5 stories\n"
        f"🌍 <b>GeoPolitics</b> — top 5 stories\n"
        f"₿  <b>Crypto</b> — top 5 stories\n\n"
        f"<b>Commands:</b>\n"
        f"/digest — Get today's digest now\n"
        f"/clear — Clear chat history\n"
        f"/help — Show this message",
        parse_mode="HTML",
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Fetching your digest... about 30 seconds!")
    await send_digest(context.application, chat_id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_history[update.effective_chat.id] = []
    await update.message.reply_text("🧹 Conversation history cleared!")


async def send_reply_with_audio_btn(update: Update, chat_id: int, reply: str):
    """Send Claude's Telugu reply with a 🔊 listen button below it."""
    last_reply[chat_id] = reply
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔊 తెలుగులో వినండి", callback_data="tts")
    ]])
    await update.message.reply_text(reply, reply_markup=keyboard)


async def handle_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Convert last Telugu reply to audio and send as voice message."""
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
            tts.save(f.name)
            tmp_path = f.name

        with open(tmp_path, "rb") as audio:
            await context.bot.send_voice(chat_id=chat_id, voice=audio)

        os.unlink(tmp_path)   # clean up temp file

    except Exception as e:
        logger.error(f"TTS error: {e}")
        await q.message.reply_text("ఆడియో తయారు చేయడంలో సమస్య వచ్చింది. మళ్ళీ ప్రయత్నించండి.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_chat.id
    user_text = update.message.text
    pending   = context.user_data.get("pending_story")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    if pending:
        section_key = pending["section_key"]
        idx         = pending["idx"]
        context.user_data.pop("pending_story")

        article = todays_digest.get(section_key, [])[idx] if todays_digest else None
        cfg     = SECTIONS.get(section_key, {})

        if article:
            full_prompt = (
                f"The user is asking about this specific news story:\n"
                f"Section: {cfg.get('label', '')}\n"
                f"Title: {article['title']}\n"
                f"Summary: {article['summary']}\n\n"
                f"User's question: {user_text}\n\n"
                f"Answer in Telugu in 3-5 sentences. Keep company/people names in English."
            )
            reply = await ask_claude(chat_id, full_prompt)
        else:
            reply = await ask_claude(chat_id, user_text)

        await send_reply_with_audio_btn(update, chat_id, reply)

    else:
        reply = await ask_claude(chat_id, user_text)
        await send_reply_with_audio_btn(update, chat_id, reply)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 💬 N button — show story title and prompt user to ask a question."""
    q = update.callback_query
    await q.answer()

    try:
        _, section_key, idx_str = q.data.split("|")
        idx     = int(idx_str)
        article = todays_digest.get(section_key, [])[idx]
    except (ValueError, IndexError):
        await q.message.reply_text("వార్త కనుగొనలేదు. /digest తో మళ్ళీ ప్రయత్నించండి.")
        return

    cfg = SECTIONS[section_key]

    # Store which story the user selected — next message will be their question
    context.user_data["pending_story"] = {
        "section_key": section_key,
        "idx":         idx,
    }

    # Show story title and ask for their question
    await q.message.reply_text(
        f"📌 <b>{h(cfg['emoji'])} {h(cfg['label'])} #{idx+1}</b>\n"
        f"<i>{h(article['title'])}</i>\n\n"
        f"❓ ఈ వార్త గురించి మీ ప్రశ్న అడగండి — నేను తెలుగులో జవాబిస్తాను.",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER + MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE):
    await send_digest(context.application, TELEGRAM_CHAT_ID)


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CallbackQueryHandler(handle_tts,      pattern="^tts$"))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^ask\\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 7:00 AM IST = 01:30 UTC
    app.job_queue.run_daily(
        scheduled_digest,
        time=datetime.strptime("01:30", "%H:%M").time(),
        name="daily_digest",
    )

    logger.info("🤖 News Digest Bot v2 is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
