"""
Daily AI News Digest
---------------------
Pulls fresh AI/tech articles from RSS feeds, filters and dedupes them,
and emails you a digest. Runs once, meant to be scheduled daily
(see the GitHub Actions workflow file that comes with this guide).

Setup needed before running:
1. pip install feedparser requests beautifulsoup4 markdown
2. Set these environment variables (locally, or as GitHub Secrets):
   - DIGEST_TO        the email address you want to receive the digest
   - DIGEST_FROM      the Gmail address sending it
   - GMAIL_APP_PASSWORD   the 16-character App Password from your Google Account
"""

import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import feedparser
import markdown


# ---------------------------------------------------------------------------
# 1. Sources — add or remove feeds here
# ---------------------------------------------------------------------------

FEEDS = [
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.technologyreview.com/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://hnrss.org/newest?q=AI",
    "https://export.arxiv.org/rss/cs.AI",
]

AI_KEYWORDS = [
    "ai", "artificial intelligence", "llm", "machine learning",
    "openai", "anthropic", "claude", "gpt", "neural network",
]


# ---------------------------------------------------------------------------
# 2. Collect articles from RSS feeds
# ---------------------------------------------------------------------------

def fetch_rss_articles(feeds, hours_back=24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    articles = []
    for url in feeds:
        parsed = feedparser.parse(url)
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
                "summary": entry.get("summary", "")[:500],
                "published": pub_dt,
                "source": parsed.feed.get("title", url),
            })
    return articles


# ---------------------------------------------------------------------------
# 3. Filter and dedupe
# ---------------------------------------------------------------------------

def filter_by_keywords(articles, keywords, mode="any"):
    keywords = [k.lower() for k in keywords]

    def matches(a):
        text = (a["title"] + " " + a.get("summary", "")).lower()
        hits = [k in text for k in keywords]
        return any(hits) if mode == "any" else all(hits)

    return [a for a in articles if matches(a)]


def dedupe_articles(articles):
    seen_titles = set()
    unique = []
    for a in articles:
        key = re.sub(r"\W+", "", a["title"].lower())[:60]
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(a)
    return unique


# ---------------------------------------------------------------------------
# 4. Format into an email body
# ---------------------------------------------------------------------------

def format_digest(articles):
    lines = [f"# AI & Tech Digest — {len(articles)} stories\n"]
    for a in articles:
        lines.append(f"### {a['title']}")
        lines.append(f"*{a['source']}*")
        if a.get("summary"):
            lines.append(a["summary"])
        lines.append(f"[Read more]({a['link']})\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Send the email
# ---------------------------------------------------------------------------

def send_email(subject, markdown_body, to_addr, from_addr, app_password):
    html_body = markdown.markdown(markdown_body)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(markdown_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(from_addr, app_password)
        server.sendmail(from_addr, to_addr, msg.as_string())


# ---------------------------------------------------------------------------
# 6. Run everything
# ---------------------------------------------------------------------------

def run_daily_digest():
    articles = fetch_rss_articles(FEEDS, hours_back=24)
    articles = filter_by_keywords(articles, AI_KEYWORDS)
    articles = dedupe_articles(articles)
    articles.sort(key=lambda a: a["published"], reverse=True)

    if not articles:
        print("No new articles found — skipping email.")
        return

    body = format_digest(articles)
    send_email(
        subject=f"AI Digest — {len(articles)} new stories",
        markdown_body=body,
        to_addr=os.environ["DIGEST_TO"],
        from_addr=os.environ["DIGEST_FROM"],
        app_password=os.environ["GMAIL_APP_PASSWORD"],
    )
    print(f"Sent digest with {len(articles)} articles.")


if __name__ == "__main__":
    run_daily_digest()
