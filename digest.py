"""
Daily Tech Digest 
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
import json
import time
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote

import feedparser
import requests

# Where the website's story data files get written. The GitHub Actions
# workflow commits this folder back to the repo after each run, and
# docs/stories.html reads these files to render the site.
SITE_DATA_DIR = "docs/data"
MAX_ARCHIVED_DAYS = 30   # how many past days the site keeps browsable


# ---------------------------------------------------------------------------
# 1. Sources, grouped by section — add or remove feeds/sections here
# ---------------------------------------------------------------------------

CATEGORIES = {
    "AI & Machine Learning": {
        "emoji": "🤖",
        "color": "#8B5CF6",
        "tint": "#F1EDFF",
        "feeds": [
            "https://techcrunch.com/category/artificial-intelligence/feed/",
            "https://venturebeat.com/category/ai/feed/",
            "https://export.arxiv.org/rss/cs.AI",
            "https://www.wired.com/feed/tag/ai/latest/rss",
            "https://www.artificialintelligence-news.com/feed/",
        ],
    },
    "Big Tech & Gadgets": {
        "emoji": "🏢",
        "color": "#38BDF8",
        "tint": "#E8F5FC",
        "feeds": [
            "https://www.theverge.com/rss/index.xml",
            "https://feeds.arstechnica.com/arstechnica/technology-lab",
            "https://techcrunch.com/feed/",
            "https://www.engadget.com/rss.xml",
            "https://gizmodo.com/rss",
            "https://www.techradar.com/rss",
        ],
    },
    "Startups & Funding": {
        "emoji": "🚀",
        "color": "#FB923C",
        "tint": "#FFEEE3",
        "feeds": [
            "https://techcrunch.com/category/startups/feed/",
            "https://news.crunchbase.com/feed/",
            "https://techcrunch.com/category/venture/feed/",
        ],
    },
    "Science & Research": {
        "emoji": "🔬",
        "color": "#2DD4BF",
        "tint": "#E3F7F3",
        "feeds": [
            "https://www.technologyreview.com/feed/",
            "https://www.sciencedaily.com/rss/top/technology.xml",
            "https://phys.org/rss-feed/",
        ],
    },
    "Crypto": {
        "emoji": "💰",
        "color": "#FBBF24",
        "tint": "#FFF7DC",
        "feeds": [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cointelegraph.com/rss",
            "https://decrypt.co/feed",
        ],
    },
}

MAX_PER_CATEGORY = 5   # default stories per section, unless a category sets "max_items"
HOURS_BACK = 24        # only include articles published in this window
# Slow news day? Sections look back this far so they can still fill all
# MAX_PER_CATEGORY slots. Stories are sorted newest-first, so the extra
# window only ever fills gaps — fresh stories always win.
FILL_HOURS_BACK = 72

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-flash-latest:generateContent"
)


# ---------------------------------------------------------------------------
# 2. Collect articles
# ---------------------------------------------------------------------------

def fetch_rss_articles(feeds, hours_back=HOURS_BACK):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; UplinkDigest/1.0)"}
    articles = []
    for url in feeds:
        try:
            # feedparser.parse(url) has no built-in timeout and can hang
            # indefinitely on a slow/unresponsive feed. Fetching the raw
            # content ourselves with a hard timeout avoids that entirely.
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"Skipping feed (fetch failed): {url} — {e}")
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
                "image": extract_feed_image(entry),
            })
    return articles


def extract_feed_image(entry):
    """Pulls an image straight from the RSS entry if the feed provides one —
    no extra network request needed. Checks the common places feeds put
    images: media:content, media:thumbnail, and image-type enclosures."""
    media_content = entry.get("media_content")
    if media_content:
        url = media_content[0].get("url")
        if url:
            return url

    media_thumbnail = entry.get("media_thumbnail")
    if media_thumbnail:
        url = media_thumbnail[0].get("url")
        if url:
            return url

    for link in entry.get("links", []):
        if str(link.get("type", "")).startswith("image"):
            return link.get("href")

    return None


def fetch_og_image(url, timeout=5):
    """Fallback for articles whose feed didn't include an image: scrapes the
    page's og:image meta tag. Only called for the small number of articles
    that actually make it into the final digest, with a tight timeout so a
    slow page can't stall the run."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; UplinkDigest/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        match = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            resp.text, re.IGNORECASE,
        )
        if not match:
            match = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
                resp.text, re.IGNORECASE,
            )
        return match.group(1) if match else None
    except Exception:
        return None


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
        articles = fetch_rss_articles(info["feeds"], hours_back=FILL_HOURS_BACK)
        articles = dedupe_articles(articles)
        articles.sort(key=lambda a: a["published"], reverse=True)
        limit = info.get("max_items", MAX_PER_CATEGORY)
        top = articles[:limit]

        # Only the articles that actually made the cut are worth the extra
        # network request for a fallback image — keeps this bounded and fast.
        for a in top:
            if not a.get("image"):
                a["image"] = fetch_og_image(a["link"])

        if top:
            result[name] = {
                "emoji": info["emoji"],
                "color": info["color"],
                "tint": info["tint"],
                "articles": top,
            }
    return result


# ---------------------------------------------------------------------------
# 3. AI summaries (Gemini free tier) with safe fallback
# ---------------------------------------------------------------------------

def call_gemini(prompt, api_key, max_tokens=100, retries=2):
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                f"{GEMINI_URL}?key={api_key}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens},
                },
                timeout=15,
            )
            if resp.status_code == 429:
                # Rate limited — wait longer and try again rather than giving up immediately
                wait = 15 * (attempt + 1)
                print(f"Gemini rate limited, waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates or "content" not in candidates[0]:
                print(f"Gemini returned no usable content (likely blocked or empty): {data}")
                return None
            parts = candidates[0]["content"].get("parts", [])
            if not parts:
                return None
            return parts[0]["text"].strip()
        except Exception as e:
            print(f"Gemini call failed, falling back to plain text: {e}")
            return None
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


def generate_section_brief(cat_name, articles, api_key):
    """One short paragraph capturing the section's stories for the website."""
    fallback = " ".join(a["ai_summary"] for a in articles[:3])
    if not api_key:
        return fallback

    stories = "\n".join(f"- {a['title']}: {a['ai_summary']}" for a in articles)
    prompt = (
        f"Write ONE short paragraph (2-3 sentences, max 60 words) in your own words "
        f"capturing what happened today in '{cat_name}' based on these stories. "
        "Plain English, neutral tone, no fluff, no bullet points, no markdown, "
        "no quotes copied from the headlines:\n\n" + stories
    )
    result = call_gemini(prompt, api_key, max_tokens=300)
    return result if result else fallback


def generate_subject_line(categorized, api_key, today_str):
    default = f"📡 Uplink — {today_str}"
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

    # Header legend — a colored dot per section, doubling as a visual index
    legend_html = ""
    for cat_name, cat_data in categorized.items():
        legend_html += f"""
        <span style="display:inline-block; margin-right:8px; margin-bottom:6px; font-size:11.5px; font-weight:700; color:#E4E0F5; background-color:#2E2A4D; border-radius:100px; padding:5px 12px;">
          <span style="display:inline-block; width:7px; height:7px; border-radius:50%; background-color:{cat_data['color']}; margin-right:6px;"></span>{cat_name}
        </span>
        """

    sections_html = ""
    for cat_name, cat_data in categorized.items():
        color = cat_data["color"]
        tint = cat_data["tint"]
        emoji = cat_data["emoji"]
        articles_html = ""
        for i, a in enumerate(cat_data["articles"]):
            border_top = "border-top:1px solid #EDEDF5;" if i > 0 else ""

            # Real photo if we found one; otherwise a tinted placeholder in
            # the section's own color so every story still looks intentional.
            if a.get("image"):
                thumb_html = f"""<img src="{a['image']}" width="72" height="72" alt=""
                    style="width:72px; height:72px; border-radius:12px; object-fit:cover; display:block; background-color:{tint};">"""
            else:
                thumb_html = f"""<table role="presentation" width="72" height="72" cellpadding="0" cellspacing="0"
                    style="width:72px; height:72px; background-color:{tint}; border-radius:12px;">
                    <tr><td align="center" valign="middle" style="font-size:26px;">{emoji}</td></tr>
                  </table>"""

            articles_html += f"""
            <tr>
              <td style="padding: 18px 0; {border_top}">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    <td width="4" style="background-color:{color}; border-radius:2px;">&nbsp;</td>
                    <td width="14">&nbsp;</td>
                    <td valign="top">
                      <a href="{a['link']}" style="color:#14162B; font-size:17px; font-weight:800; text-decoration:none; line-height:1.35;">
                        {a['title']}
                      </a>
                      <div style="color:{color}; font-size:11px; font-weight:700; letter-spacing:0.3px; margin-top:5px; margin-bottom:7px;">
                        {a['source']}
                      </div>
                      <div style="color:#4A4B5C; font-size:14.5px; line-height:1.6;">
                        {a['ai_summary']}
                      </div>
                    </td>
                    <td width="14">&nbsp;</td>
                    <td width="72" valign="top">{thumb_html}</td>
                  </tr>
                </table>
              </td>
            </tr>
            """
        sections_html += f"""
        <tr>
          <td style="padding: 32px 0 2px 0;">
            <span style="display:inline-block; background-color:{cat_data["tint"]}; color:{color}; font-size:13px; font-weight:800; padding:6px 14px; border-radius:20px;">
              {cat_data['emoji']}&nbsp;&nbsp;{cat_name}
            </span>
          </td>
        </tr>
        <tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{articles_html}</table></td></tr>
        """

    html = f"""
    <html>
    <body style="margin:0; padding:0; background-color:#FFFBF5; font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#FFFBF5; padding: 28px 0;">
        <tr>
          <td align="center">
            <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color:#FFFFFF; border-radius:20px; overflow:hidden; max-width:600px; width:100%; box-shadow: 0 1px 3px rgba(36,31,61,0.08);">
              <tr>
                <td style="background-color:#241F3D; padding:0;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td bgcolor="#FF5C7A" style="height:4px; line-height:4px; font-size:4px; background:linear-gradient(90deg,#FF5C7A,#8B5CF6,#38BDF8);">&nbsp;</td>
                    </tr>
                  </table>
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:26px 32px 20px;">
                    <tr>
                      <td valign="middle">
                        <span style="color:#ffffff; font-size:34px; font-weight:800; letter-spacing:-0.8px; display:block; line-height:1.1;">Uplink</span>
                        <span style="color:#B8B2D6; font-size:14px; line-height:1.5; display:block; margin-top:6px;">Tech news,<br/>transmitted daily.</span>
                      </td>
                      <td width="120" valign="middle" align="right">
                        <img src="https://uplinkbrief.com/email-assets/uplink-hero.png" width="120" height="120" alt="Uplink" style="display:block; width:120px; height:120px; border:0; border-radius:20px;">
                      </td>
                    </tr>
                  </table>
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:0 32px 26px;">
                    <tr>
                      <td style="border-top:1px solid #35304F; padding-top:16px;">
                        <span style="color:#B8B2D6; font-size:13px;">{today_str} &nbsp;·&nbsp; {total_articles} stories &nbsp;·&nbsp; {read_minutes} min read</span>
                        <div style="margin-top:14px; line-height:2.1;">{legend_html}</div>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding: 6px 32px 24px 32px;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                    {sections_html}
                  </table>
                </td>
              </tr>
              <tr>
                <td style="background-color:#FFFBF5; padding: 22px 32px; text-align:center;">
                  <span style="color:#9A93AE; font-size:12px;">Uplink — free, daily, independent. &nbsp;·&nbsp; <a href="__UNSUBSCRIBE_LINK__" style="color:#9A93AE;">Unsubscribe</a></span>
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
    lines = ["Uplink — your daily signal\n"]
    for cat_name, cat_data in categorized.items():
        lines.append(f"\n{cat_data['emoji']} {cat_name}")
        for a in cat_data["articles"]:
            lines.append(f"- {a['title']} ({a['source']})")
            lines.append(f"  {a['ai_summary']}")
            lines.append(f"  {a['link']}")
    lines.append("\n---\nUnsubscribe: __UNSUBSCRIBE_LINK__")
    return "\n".join(lines)


def write_site_data(categorized, date_str, today_str):
    """Saves today's stories as a JSON file the website can read, and keeps
    an index listing every available date so the site can browse past days.
    This is what makes the stories page possible without a real backend —
    GitHub Actions commits these files back to the repo after every run."""
    os.makedirs(SITE_DATA_DIR, exist_ok=True)

    snapshot = {
        "date": date_str,
        "display_date": today_str,
        "categories": [
            {
                "name": cat_name,
                "emoji": cat_data["emoji"],
                "color": cat_data["color"],
                "tint": cat_data["tint"],
                "brief": cat_data.get("brief", ""),
                "articles": [
                    {
                        "title": a["title"],
                        "link": a["link"],
                        "source": a["source"],
                        "summary": a["ai_summary"],
                        "image": a.get("image"),
                    }
                    for a in cat_data["articles"]
                ],
            }
            for cat_name, cat_data in categorized.items()
        ],
    }

    with open(f"{SITE_DATA_DIR}/{date_str}.json", "w") as f:
        json.dump(snapshot, f, indent=2)

    # Update the index of available dates, newest first, capped so the
    # repo doesn't grow forever.
    index_path = f"{SITE_DATA_DIR}/index.json"
    dates = []
    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                dates = json.load(f).get("dates", [])
        except Exception:
            dates = []

    if date_str not in dates:
        dates.insert(0, date_str)
    dates = dates[:MAX_ARCHIVED_DAYS]

    with open(index_path, "w") as f:
        json.dump({"dates": dates}, f, indent=2)

    print(f"Wrote site data for {date_str} ({len(dates)} days now archived).")


# ---------------------------------------------------------------------------
# 5. Send the email
# ---------------------------------------------------------------------------

def fetch_subscribers(endpoint_url, fallback_email):
    """Pulls the active subscriber list from the Google Apps Script endpoint.
    Falls back to a single address if the endpoint is unset or fails, so the
    script never silently sends to nobody."""
    if not endpoint_url:
        print("No SUBSCRIBERS_ENDPOINT set — sending only to DIGEST_TO.")
        return [fallback_email]
    try:
        resp = requests.get(endpoint_url, timeout=15)
        resp.raise_for_status()
        emails = resp.json().get("emails", [])
        if not emails:
            print("Subscriber list came back empty — sending only to DIGEST_TO.")
            return [fallback_email]
        return emails
    except Exception as e:
        print(f"Failed to fetch subscriber list ({e}) — falling back to DIGEST_TO.")
        return [fallback_email]


def send_to_all_subscribers(subject, html_body, plain_body, subscribers, from_addr, app_password, subscribers_endpoint):
    sent, failed = 0, []
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(from_addr, app_password)
        for to_addr in subscribers:
            try:
                unsubscribe_url = (
                    f"{subscribers_endpoint}?unsubscribe={quote(to_addr)}"
                    if subscribers_endpoint else "#"
                )
                personal_html = html_body.replace("__UNSUBSCRIBE_LINK__", unsubscribe_url)
                personal_plain = plain_body.replace("__UNSUBSCRIBE_LINK__", unsubscribe_url)

                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = from_addr
                msg["To"] = to_addr
                msg.attach(MIMEText(personal_plain, "plain"))
                msg.attach(MIMEText(personal_html, "html"))
                server.sendmail(from_addr, to_addr, msg.as_string())
                sent += 1
                time.sleep(1)  # small pause between sends, easy on Gmail's rate limits
            except Exception as e:
                print(f"Failed to send to {to_addr}: {e}")
                failed.append(to_addr)
    return sent, failed


# ---------------------------------------------------------------------------
# 6. Run everything
# ---------------------------------------------------------------------------

def run_daily_digest():
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    subscribers_endpoint = os.environ.get("SUBSCRIBERS_ENDPOINT", "")
    digest_to = os.environ["DIGEST_TO"]
    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    categorized = collect_by_category()
    if not categorized:
        print("No new articles found across any category — skipping email.")
        return

    # Write an AI summary for every article we're about to send.
    # A short pause between calls keeps us under Gemini's free-tier rate limit.
    # If Gemini fails 3 times in a row (e.g. daily quota exhausted), stop calling
    # it for the rest of this run instead of retrying uselessly on every article —
    # saves time and doesn't burn quota you don't have left today.
    consecutive_failures = 0
    gemini_disabled = False
    for cat_data in categorized.values():
        for a in cat_data["articles"]:
            if gemini_disabled or not gemini_key:
                a["ai_summary"] = a["raw_summary"][:200] + ("..." if len(a["raw_summary"]) > 200 else "")
                continue
            summary = summarize_tldr(a["title"], a["raw_summary"], gemini_key)
            fallback = a["raw_summary"][:200] + ("..." if len(a["raw_summary"]) > 200 else "")
            if summary == fallback:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print("Gemini failed 3 times in a row — skipping AI summaries for the rest of this run.")
                    gemini_disabled = True
            else:
                consecutive_failures = 0
            a["ai_summary"] = summary
            time.sleep(3)

    # One short paragraph per section for the website's topic cards.
    for cat_name, cat_data in categorized.items():
        if gemini_disabled or not gemini_key:
            cat_data["brief"] = generate_section_brief(cat_name, cat_data["articles"], "")
        else:
            cat_data["brief"] = generate_section_brief(cat_name, cat_data["articles"], gemini_key)
            time.sleep(3)

    subject = generate_subject_line(categorized, gemini_key, today_str)
    html_body = build_html_email(categorized, today_str)
    plain_body = build_plain_text_fallback(categorized)

    # Save today's stories for the website, independent of email sending —
    # the site should stay up to date even if something downstream fails.
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        write_site_data(categorized, date_str, today_str)
    except Exception as e:
        print(f"Failed to write site data (non-fatal): {e}")

    subscribers = fetch_subscribers(subscribers_endpoint, digest_to)

    sent, failed = send_to_all_subscribers(
        subject=subject,
        html_body=html_body,
        plain_body=plain_body,
        subscribers=subscribers,
        from_addr=os.environ["DIGEST_FROM"],
        app_password=os.environ["GMAIL_APP_PASSWORD"],
        subscribers_endpoint=subscribers_endpoint,
    )

    total_articles = sum(len(c["articles"]) for c in categorized.values())
    print(f"Digest: {total_articles} articles across {len(categorized)} sections.")
    print(f"Sent to {sent}/{len(subscribers)} subscribers.")
    if failed:
        print(f"Failed for: {', '.join(failed)}")


if __name__ == "__main__":
    run_daily_digest()
