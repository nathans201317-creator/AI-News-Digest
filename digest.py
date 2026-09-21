"""
Daily Tech Digest — V1.0
--------------------------------
Pulls articles from RSS feeds across multiple topic sections, picks the
top few per section, writes a punchy one-line AI summary for each, and
emails a styled HTML digest.

Setup needed before running:
1. pip install -r requirements.txt
2. Set these environment variables (locally, or as GitHub Secrets):
   - DIGEST_TO            the email address you want to receive the digest
   - DIGEST_FROM          the Gmail address sending it
   - GMAIL_APP_PASSWORD   the 16-character App Password from your Google Account
   - GEMINI_API_KEY       free API key from https://aistudio.google.com/apikey
                          (used to write the one-line summaries and subject line;
                          if missing, the script still runs using plain RSS text instead)
"""

import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import feedparser
import requests


# ---------------------------------------------------------------------------
# 1. Sources, grouped by section — add or remove feeds/sections here
# ---------------------------------------------------------------------------

CATEGORIES = {
    "AI & Machine Learning": {
        "emoji": "🤖",
        "feeds": [
            "https://techcrunch.com/category/artificial-intelligence/feed/",
            "https://venturebeat.com/category/ai/feed/",
            "https://export.arxiv.org/rss/cs.AI",
        ],
    },
    "Big Tech": {
        "emoji": "🏢",
        "feeds": [
            "https://www.theverge.com/rss/index.xml",
            "https://feeds.arstechnica.com/arstechnica/technology-lab",
        ],
    },
    "Startups & Funding": {
        "emoji": "🚀",
        "feeds": [
            "https://techcrunch.com/category/startups/feed/",
            "https://news.crunchbase.com/feed/",
        ],
    },
    "Science & Research": {
        "emoji": "🔬",
        "feeds": [
            "https://www.technologyreview.com/feed/",
            "https://www.sciencedaily.com/rss/top/technology.xml",
        ],
    },
    "Crypto": {
        "emoji": "💰",
        "feeds": [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cointelegraph.com/rss",
        ],
    },
}

MAX_PER_CATEGORY = 4   # how many stories to show per section
HOURS_BACK = 24        # only include articles published in this window

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)


# ---------------------------------------------------------------------------
# 2. Collect articles
# ---------------------------------------------------------------------------

def fetch_rss_articles(feeds, hours_back=HOURS_BACK):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    articles = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        for entry in parsed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if not published:
                continue
            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
            articles.append({
                "title": entry.get("title", "No title"),
                "link": entry.get("link", ""),
                "raw_summary": re.sub("<[^<]+?>", "", entry.get("summary", ""))[:600],
                "published": pub_dt,
                "source": parsed.feed.get("title", url),
            })
    return articles


def dedupe_articles(articles):
    seen = set()
    unique = []
    for a in articles:
        key = re.sub(r"\W+", "", a["title"].lower())[:60]
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique


def collect_by_category():
    result = {}
    for name, info in CATEGORIES.items():
        articles = fetch_rss_articles(info["feeds"])
        articles = dedupe_articles(articles)
        articles.sort(key=lambda a: a["published"], reverse=True)
        top = articles[:MAX_PER_CATEGORY]
        if top:
            result[name] = {"emoji": info["emoji"], "articles": top}
    return result


# ---------------------------------------------------------------------------
# 3. AI summaries (Gemini free tier) with safe fallback
# ---------------------------------------------------------------------------

def call_gemini(prompt, api_key, max_tokens=100):
    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={api_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Gemini call failed, falling back to plain text: {e}")
        return None


def summarize_tldr(title, raw_summary, api_key):
    if not api_key:
        return raw_summary[:200] + ("..." if len(raw_summary) > 200 else "")

    prompt = (
        "Write one punchy, plain-English sentence (max 25 words) summarizing "
        "this tech news story, in the style of a TLDR.tech newsletter blurb — "
        "no fluff, no 'this article discusses', just the actual news:\n\n"
        f"Title: {title}\nDetails: {raw_summary[:800]}"
    )
    result = call_gemini(prompt, api_key)
    if result:
        return result
    return raw_summary[:200] + ("..." if len(raw_summary) > 200 else "")


def generate_subject_line(categorized, api_key, today_str):
    default = f"🗞️ Your Tech Digest — {today_str}"
    if not api_key:
        return default

    headlines = []
    for cat_data in categorized.values():
        headlines.extend(a["title"] for a in cat_data["articles"][:2])
    if not headlines:
        return default

    prompt = (
        "Write one short, punchy email subject line (max 12 words) for a tech "
        "newsletter, teasing 2-3 of these headlines. Include one relevant emoji "
        "at the start. No quotation marks around the output:\n\n"
        + "\n".join(f"- {h}" for h in headlines[:6])
    )
    result = call_gemini(prompt, api_key, max_tokens=40)
    return result.strip('"') if result else default


# ---------------------------------------------------------------------------
# 4. Build the styled HTML email
# ---------------------------------------------------------------------------

def build_html_email(categorized, today_str):
    total_articles = sum(len(c["articles"]) for c in categorized.values())
    read_minutes = max(1, round(total_articles * 0.4))

    sections_html = ""
    for cat_name, cat_data in categorized.items():
        articles_html = ""
        for a in cat_data["articles"]:
            articles_html += f"""
            <tr>
              <td style="padding: 0 0 20px 0;">
                <a href="{a['link']}" style="color:#111827; font-size:16px; font-weight:600; text-decoration:none; line-height:1.4;">
                  {a['title']}
                </a>
                <div style="color:#6b7280; font-size:12px; margin-top:2px; margin-bottom:6px;">
                  {a['source']}
                </div>
                <div style="color:#374151; font-size:14px; line-height:1.5;">
                  {a['ai_summary']}
                </div>
              </td>
            </tr>
            """
        sections_html += f"""
        <tr>
          <td style="padding: 28px 0 8px 0; border-top: 2px solid #111827;">
            <span style="font-size:20px; font-weight:700; color:#111827;">
              {cat_data['emoji']} {cat_name}
            </span>
          </td>
        </tr>
        <tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{articles_html}</table></td></tr>
        """

    html = f"""
    <html>
    <body style="margin:0; padding:0; background-color:#f3f4f6; font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f3f4f6; padding: 24px 0;">
        <tr>
          <td align="center">
            <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:12px; overflow:hidden; max-width:600px; width:100%;">
              <tr>
                <td style="background-color:#111827; padding: 28px 32px;">
                  <span style="color:#ffffff; font-size:24px; font-weight:800;">Daily Tech Digest</span><br/>
                  <span style="color:#9ca3af; font-size:13px;">{today_str} · {total_articles} stories · {read_minutes} min read</span>
                </td>
              </tr>
              <tr>
                <td style="padding: 8px 32px 32px 32px;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                    {sections_html}
                  </table>
                </td>
              </tr>
              <tr>
                <td style="background-color:#f9fafb; padding: 20px 32px; text-align:center;">
                  <span style="color:#9ca3af; font-size:12px;">Built by your own daily digest script — free forever.</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """
    return html


def build_plain_text_fallback(categorized):
    lines = ["Your Daily Tech Digest\n"]
    for cat_name, cat_data in categorized.items():
        lines.append(f"\n{cat_data['emoji']} {cat_name}")
        for a in cat_data["articles"]:
            lines.append(f"- {a['title']} ({a['source']})")
            lines.append(f"  {a['ai_summary']}")
            lines.append(f"  {a['link']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Send the email
# ---------------------------------------------------------------------------

def send_email(subject, html_body, plain_body, to_addr, from_addr, app_password):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(from_addr, app_password)
        server.sendmail(from_addr, to_addr, msg.as_string())


# ---------------------------------------------------------------------------
# 6. Run everything
# ---------------------------------------------------------------------------

def run_daily_digest():
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    categorized = collect_by_category()
    if not categorized:
        print("No new articles found across any category — skipping email.")
        return

    # Write an AI summary for every article we're about to send
    for cat_data in categorized.values():
        for a in cat_data["articles"]:
            a["ai_summary"] = summarize_tldr(a["title"], a["raw_summary"], gemini_key)

    subject = generate_subject_line(categorized, gemini_key, today_str)
    html_body = build_html_email(categorized, today_str)
    plain_body = build_plain_text_fallback(categorized)

    send_email(
        subject=subject,
        html_body=html_body,
        plain_body=plain_body,
        to_addr=os.environ["DIGEST_TO"],
        from_addr=os.environ["DIGEST_FROM"],
        app_password=os.environ["GMAIL_APP_PASSWORD"],
    )
    total = sum(len(c["articles"]) for c in categorized.values())
    print(f"Sent digest with {total} articles across {len(categorized)} sections.")


if __name__ == "__main__":
    run_daily_digest()
