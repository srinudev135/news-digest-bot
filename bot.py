#!/usr/bin/env python3
"""
Daily News Digest Telegram Bot v2
- Clean UI: 4 sections, numbered stories, per-story follow-up buttons
- Sections: AI Tech | Finance | GeoPolitics | Crypto
- Powered by Claude AI for summaries and follow-up chat
"""

import os
import logging
import feedparser
import httpx
from datetime import datetime
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
#  FORMATTING  (clean numbered list, one message per section)
# ══════════════════════════════════════════════════════════════════════════════

def escape_md(text: str) -> str:
    """Escape special MarkdownV2 characters."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def build_section_message(key: str, articles: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """
    Returns (text, keyboard) for one section.

    Format:
    ────────────────────
    🤖 *AI TECH*
    ────────────────────
    1. Article headline one
    2. Article headline two
    3. Article headline three
    4. Article headline four
    5. Article headline five

    [ 💬 1 ] [ 💬 2 ] [ 💬 3 ] [ 💬 4 ] [ 💬 5 ]
    [ 🔗 1 ] [ 🔗 2 ] [ 🔗 3 ] [ 🔗 4 ] [ 🔗 5 ]
    """
    cfg   = SECTIONS[key]
    emoji = cfg["emoji"]
    label = cfg["label"].upper()

    divider = escape_md("―" * 22)
    header  = f"{divider}\n{emoji} *{escape_md(label)}*\n{divider}\n\n"

    lines = ""
    for i, a in enumerate(articles, 1):
        lines += f"{i}\\. {escape_md(a['title'])}\n"

    text = header + lines

    # Row 1: "Ask about #N" buttons
    ask_row  = [
        InlineKeyboardButton(f"💬 {i}", callback_data=f"ask|{key}|{i-1}")
        for i in range(1, len(articles) + 1)
    ]
    # Row 2: "Read link #N" buttons
    link_row = [
        InlineKeyboardButton(f"🔗 {i}", url=articles[i-1]["link"])
        for i in range(1, len(articles) + 1)
    ]

    keyboard = InlineKeyboardMarkup([ask_row, link_row])
    return text, keyboard


# ══════════════════════════════════════════════════════════════════════════════
#  DIGEST SENDER
# ══════════════════════════════════════════════════════════════════════════════

async def send_digest(app: Application, chat_id: int):
    global todays_digest

    # Header message
    now_ist = datetime.utcnow()  # approx; actual IST offset handled by schedule
    date_str = escape_md(now_ist.strftime("%A, %d %B %Y"))
    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🌅 *Good Morning\\!*\n"
            f"📅 {date_str}\n\n"
            f"Here are your top stories for today\\.\n"
            f"Tap 💬 on any story number to ask Claude about it\\.\n"
            f"Tap 🔗 to read the full article\\."
        ),
        parse_mode="MarkdownV2",
    )

    todays_digest = {}

    for key, cfg in SECTIONS.items():
        await app.bot.send_chat_action(chat_id=chat_id, action="typing")
        articles = await fetch_section(key, cfg)
        todays_digest[key] = articles

        if not articles:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"{cfg['emoji']} *{escape_md(cfg['label'].upper())}*\n\n_No stories found today\\._",
                parse_mode="MarkdownV2",
            )
            continue

        text, keyboard = build_section_message(key, articles)
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    # Footer
    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ *That's your digest for today\\!*\n\n"
            "You can also just *type any question* and I'll answer it\\."
        ),
        parse_mode="MarkdownV2",
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
        "You are a sharp, concise AI news analyst. "
        "The user has received today's digest shown below. "
        "When they ask about a story, give a 3-5 sentence insight: "
        "what happened, why it matters, and what to watch next. "
        "Be direct and insightful, not generic.\n\n"
        f"TODAY'S DIGEST:\n{context}"
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
        return "Sorry, I couldn't process that. Please try again."


# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 *Welcome to your Daily News Digest Bot\\!*\n\n"
        f"Your Chat ID: `{chat_id}`\n\n"
        f"📰 Every morning at *7:00 AM IST* I'll send you:\n\n"
        f"🤖 *AI Tech* — top 5 stories\n"
        f"💰 *Finance* — top 5 stories\n"
        f"🌍 *GeoPolitics* — top 5 stories\n"
        f"₿  *Crypto* — top 5 stories\n\n"
        f"*Commands:*\n"
        f"/digest — Get today's digest now\n"
        f"/clear — Clear chat history\n"
        f"/help — Show this message",
        parse_mode="MarkdownV2",
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Fetching your digest\\.\\.\\. ~30 seconds", parse_mode="MarkdownV2")
    await send_digest(context.application, chat_id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_history[update.effective_chat.id] = []
    await update.message.reply_text("🧹 Conversation history cleared\\!", parse_mode="MarkdownV2")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await ask_claude(chat_id, update.message.text)
    await update.message.reply_text(reply)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 💬 N button — ask Claude about that specific story."""
    q = update.callback_query
    await q.answer()

    try:
        _, section_key, idx_str = q.data.split("|")
        idx     = int(idx_str)
        article = todays_digest.get(section_key, [])[idx]
    except (ValueError, IndexError):
        await q.message.reply_text("Couldn't find that story. Try /digest to refresh.")
        return

    cfg    = SECTIONS[section_key]
    prompt = (
        f"The user tapped on story #{idx+1} from the {cfg['label']} section:\n\n"
        f"Title: {article['title']}\n"
        f"Summary: {article['summary']}\n\n"
        f"Give a sharp 3-5 sentence analysis: what happened, why it matters, and what to watch next."
    )

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await ask_claude(chat_id, prompt)

    # Show which story they asked about, then the analysis
    story_ref = escape_md(f"{cfg['emoji']} {cfg['label']} #{idx+1}: {article['title']}")
    await q.message.reply_text(
        f"_{story_ref}_\n\n{escape_md(reply)}",
        parse_mode="MarkdownV2",
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
    app.add_handler(CallbackQueryHandler(handle_callback))
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
