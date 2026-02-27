#!/usr/bin/env python3
"""
Daily News Digest Telegram Bot
- Sends rich daily digest at 7 AM IST
- Supports follow-up questions via Claude AI
- Covers: TikTok trends, Instagram trends, AI news, Tech news, Finance news
"""

import os
import asyncio
import logging
import json
from datetime import datetime
import feedparser
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import anthropic

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config from environment ───────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]       # Your personal chat ID
NEWS_API_KEY        = os.environ["NEWS_API_KEY"]            # newsapi.org
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── News sources ──────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "🤖 AI & Tech": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://feeds.feedburner.com/venturebeat/SZYF",
        "https://openai.com/news/rss.xml",
    ],
    "💻 Tech General": [
        "https://feeds.wired.com/wired/index",
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
    ],
    "💰 Finance": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.bloomberg.com/markets/news.rss",  # may need auth
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "https://feeds.finance.yahoo.com/rss/2.0/headline",
    ],
    "📱 Social Trends (TikTok/Instagram)": [
        "https://www.socialmediatoday.com/rss.xml",
        "https://later.com/blog/feed/",
        "https://www.socialsamosa.com/feed/",   # India Instagram trends
        "https://www.businessofapps.com/feed/",
    ],
}

NEWSAPI_QUERIES = {
    "🎵 TikTok Viral (USA)":        ("TikTok viral trending",  "us"),
    "🎵 TikTok Viral (Global)":     ("TikTok viral trending",  None),
    "📸 Instagram Viral (USA)":     ("Instagram trending viral","us"),
    "📸 Instagram Viral (India)":   ("Instagram trending India","in"),
    "📸 Instagram Viral (Global)":  ("Instagram trending viral",None),
}

# ── Conversation memory (per chat) ───────────────────────────────────────────
conversation_history: dict[int, list] = {}
todays_digest: dict = {}           # stored so follow-up can reference it


# ══════════════════════════════════════════════════════════════════════════════
#  NEWS FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_rss(url: str, max_items: int = 3) -> list[dict]:
    """Fetch and parse an RSS feed, returning a list of article dicts."""
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            items.append({
                "title":   entry.get("title", "No title"),
                "link":    entry.get("link", ""),
                "summary": entry.get("summary", entry.get("description", ""))[:300],
                "image":   _extract_image(entry),
            })
        return items
    except Exception as e:
        logger.warning(f"RSS fetch failed for {url}: {e}")
        return []


def _extract_image(entry) -> str | None:
    """Try to pull a thumbnail/image URL from an RSS entry."""
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")
    if hasattr(entry, "links"):
        for link in entry.links:
            if link.get("type", "").startswith("image"):
                return link.get("href")
    return None


async def fetch_newsapi(query: str, country: str | None, max_items: int = 3) -> list[dict]:
    """Fetch top headlines from NewsAPI."""
    params = {
        "q":       query,
        "apiKey":  NEWS_API_KEY,
        "pageSize": max_items,
        "language": "en",
        "sortBy":  "publishedAt",
    }
    if country:
        params["country"] = country
        del params["q"]
        params["q"] = query

    url = "https://newsapi.org/v2/everything" if not country else "https://newsapi.org/v2/top-headlines"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params)
            data = r.json()
        articles = []
        for a in data.get("articles", [])[:max_items]:
            articles.append({
                "title":   a.get("title", ""),
                "link":    a.get("url", ""),
                "summary": a.get("description", "")[:300],
                "image":   a.get("urlToImage"),
            })
        return articles
    except Exception as e:
        logger.warning(f"NewsAPI failed for '{query}': {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  AI SUMMARIZATION
# ══════════════════════════════════════════════════════════════════════════════

def ai_summarize_section(section_name: str, articles: list[dict]) -> str:
    """Use Claude to generate a crisp 2-line summary for a news section."""
    if not articles:
        return "No articles found for this section today."
    
    articles_text = "\n".join(
        f"- {a['title']}: {a['summary']}" for a in articles
    )
    prompt = (
        f"You are a sharp news editor. Given these headlines for '{section_name}', "
        f"write a 2-sentence executive summary capturing the biggest trend or story. "
        f"Be concise and insightful.\n\nHeadlines:\n{articles_text}"
    )
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.warning(f"Claude summarization failed: {e}")
        return articles[0]["summary"] if articles else ""


# ══════════════════════════════════════════════════════════════════════════════
#  DIGEST BUILDER
# ══════════════════════════════════════════════════════════════════════════════

async def build_digest() -> dict:
    """Fetch all news and build the full digest dict."""
    digest = {}

    # RSS-based sections
    for section, urls in RSS_FEEDS.items():
        articles = []
        for url in urls:
            articles.extend(fetch_rss(url, max_items=2))
            if len(articles) >= 4:
                break
        articles = articles[:4]
        digest[section] = {
            "articles": articles,
            "summary":  ai_summarize_section(section, articles),
        }

    # NewsAPI-based sections
    for section, (query, country) in NEWSAPI_QUERIES.items():
        articles = await fetch_newsapi(query, country, max_items=3)
        digest[section] = {
            "articles": articles,
            "summary":  ai_summarize_section(section, articles),
        }

    return digest


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM SENDING
# ══════════════════════════════════════════════════════════════════════════════

async def send_digest(app: Application, chat_id: int):
    """Build and send the full digest to a Telegram chat."""
    global todays_digest

    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            "🌅 *Good Morning\\! Your Daily News Digest is ready\\!*\n\n"
            "Here's what's trending today across all your topics 👇\n"
            "_Ask me anything about any story after the digest\\!_"
        ),
        parse_mode="MarkdownV2"
    )

    digest = await build_digest()
    todays_digest = digest

    for section, data in digest.items():
        # Section header + AI summary
        section_safe = section.replace(".", "\\.").replace("(", "\\(").replace(")", "\\)")
        summary_safe = (data["summary"]
                        .replace(".", "\\.").replace("!", "\\!")
                        .replace("(", "\\(").replace(")", "\\)")
                        .replace("-", "\\-").replace(">", "\\>")
                        .replace("#", "\\#"))

        header = f"*{section_safe}*\n_{summary_safe}_\n"
        await app.bot.send_message(chat_id=chat_id, text=header, parse_mode="MarkdownV2")

        # Individual articles
        for idx, article in enumerate(data["articles"][:3], 1):
            title   = article["title"] or "Untitled"
            link    = article["link"]  or ""
            image   = article.get("image")

            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔗 Read More", url=link),
                InlineKeyboardButton(f"💬 Ask about this", callback_data=f"ask|{section}|{idx-1}"),
            ]])

            if image:
                try:
                    await app.bot.send_photo(
                        chat_id=chat_id,
                        photo=image,
                        caption=f"*{title}*",
                        parse_mode="Markdown",
                        reply_markup=btn,
                    )
                    continue
                except Exception:
                    pass  # fall through to text message

            await app.bot.send_message(
                chat_id=chat_id,
                text=f"📰 *{title}*",
                parse_mode="Markdown",
                reply_markup=btn,
            )

    # End of digest
    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ *That's your full digest for today!*\n\n"
            "💬 You can now:\n"
            "• Type any question about today's news\n"
            "• Tap *'Ask about this'* on any story\n"
            "• Use /digest to re-fetch anytime\n"
            "• Use /help for more commands"
        ),
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  FOLLOW-UP CHAT (Claude-powered)
# ══════════════════════════════════════════════════════════════════════════════

def build_system_prompt() -> str:
    """Build Claude system prompt with today's digest as context."""
    digest_text = ""
    for section, data in todays_digest.items():
        digest_text += f"\n\n### {section}\nSummary: {data['summary']}\nArticles:\n"
        for a in data["articles"]:
            digest_text += f"  - {a['title']}: {a['summary']}\n"

    return (
        "You are a sharp, friendly AI news assistant. "
        "The user has received today's news digest (below). "
        "Answer their follow-up questions insightfully, referencing specific stories when relevant. "
        "Keep answers concise (3-5 sentences). If asked for deeper analysis, provide it.\n\n"
        f"TODAY'S DIGEST CONTEXT:\n{digest_text}"
    )


async def chat_with_claude(chat_id: int, user_message: str) -> str:
    """Send a message to Claude with conversation history."""
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": user_message})

    # Keep last 20 messages to avoid token overflow
    history = conversation_history[chat_id][-20:]

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=build_system_prompt(),
            messages=history,
        )
        reply = response.content[0].text.strip()
        conversation_history[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Claude chat error: {e}")
        return "Sorry, I had trouble thinking that through. Please try again!"


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 *Welcome to your Personal News Digest Bot!*\n\n"
        f"Your Chat ID is: `{chat_id}`\n\n"
        f"📰 I'll send you a rich daily digest every morning at *7:00 AM IST*\n\n"
        f"*Commands:*\n"
        f"/digest — Get today's digest now\n"
        f"/topics — See what topics I cover\n"
        f"/clear — Clear conversation history\n"
        f"/help — Show this message\n\n"
        f"Or just *type any question* about the news!",
        parse_mode="Markdown"
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Fetching your digest... this takes ~30 seconds!")
    await send_digest(context.application, chat_id)


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Topics I cover daily:*\n\n"
        "🎵 TikTok Viral — USA & Global\n"
        "📸 Instagram Viral — USA, India & Global\n"
        "🤖 AI & Tech Updates\n"
        "💻 Tech Industry News\n"
        "💰 Finance & Markets\n\n"
        "All powered by NewsAPI + RSS + Claude AI!",
        parse_mode="Markdown"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conversation_history[chat_id] = []
    await update.message.reply_text("🧹 Conversation history cleared!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text follow-up questions."""
    chat_id  = update.effective_chat.id
    question = update.message.text

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await chat_with_claude(chat_id, question)
    await update.message.reply_text(reply)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Ask about this' button presses."""
    query = update.callback_query
    await query.answer()

    _, section, idx_str = query.data.split("|", 2)
    idx = int(idx_str)

    article = todays_digest.get(section, {}).get("articles", [])[idx] if todays_digest else None
    if not article:
        await query.message.reply_text("Sorry, I couldn't find that article. Try /digest to refresh.")
        return

    prompt = (
        f"The user wants to know more about this article:\n"
        f"Title: {article['title']}\n"
        f"Summary: {article['summary']}\n\n"
        f"Give a helpful 3-4 sentence analysis: what this means, why it matters, and what to watch next."
    )
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await chat_with_claude(chat_id, prompt)
    await query.message.reply_text(f"💡 *Analysis:*\n\n{reply}", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE):
    """Job that runs daily at 7 AM IST."""
    await send_digest(context.application, int(TELEGRAM_CHAT_ID))


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily job at 7:00 AM IST = 01:30 UTC
    job_queue = app.job_queue
    job_queue.run_daily(
        scheduled_digest,
        time=datetime.strptime("01:30", "%H:%M").time(),  # 7:00 AM IST
        name="daily_digest",
    )

    logger.info("🤖 News Digest Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
