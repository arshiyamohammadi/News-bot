import os
import json
import feedparser
import requests
import time
import random
import re
import hashlib
from datetime import datetime, timezone

from groq import Groq

# ─────────────────────────────────────────────
# ۱. بارگذاری کلیدهای امنیتی از محیط گیت‌هاب
# ─────────────────────────────────────────────
GROQ_KEY       = os.environ.get("GROQ_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID     = os.environ.get("TELEGRAM_CHANNEL_ID")

if not all([GROQ_KEY, TELEGRAM_TOKEN, CHANNEL_ID]):
    raise EnvironmentError("❌ یک یا چند کلید محیطی مقداردهی نشده‌اند.")

# ─────────────────────────────────────────────
# ۲. راه‌اندازی مدل Groq (LLaMA 3.3 70B)
# ─────────────────────────────────────────────
client = Groq(api_key=GROQ_KEY)
GROQ_MODEL = "llama-3.3-70b-versatile"

# ─────────────────────────────────────────────
# ۳. مدیریت حافظه (history.json)
# ─────────────────────────────────────────────
HISTORY_FILE  = "history.json"
MAX_HISTORY   = 800   # حداکثر آیتم نگهداری در حافظه

def load_history() -> set:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                return set(json.load(f))
            except Exception:
                return set()
    return set()

def save_history(history: set):
    items = list(history)
    if len(items) > MAX_HISTORY:
        items = items[-MAX_HISTORY:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)

history = load_history()

# ─────────────────────────────────────────────
# ۴. منابع خبری (جهان + ایران + اقتصاد + ورزش + تکنولوژی)
# ─────────────────────────────────────────────
FEEDS = [
    # ── اخبار جهان ──────────────────────────
    ("world",   "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("world",   "https://rss.cnn.com/rss/edition_world.rss"),
    ("world",   "https://feeds.reuters.com/reuters/worldNews"),
    ("world",   "https://english.alarabiya.net/.mrss/en.xml"),
    ("world",   "https://feeds.skynews.com/feeds/rss/world.xml"),

    # ── اخبار ایران ──────────────────────────
    ("iran",    "https://www.iranintl.com/rss/fa"),
    ("iran",    "https://www.radiofarda.com/api/zu_oe-opy"),
    ("iran",    "https://www.bbc.com/persian/index.xml"),

    # ── اقتصاد ──────────────────────────────
    ("economy", "https://feeds.reuters.com/reuters/businessNews"),
    ("economy", "https://feeds.bloomberg.com/markets/news.rss"),
    ("economy", "https://www.ft.com/?format=rss"),
    ("economy", "https://feeds.bbci.co.uk/news/business/rss.xml"),

    # ── ورزش ────────────────────────────────
    ("sports",  "https://feeds.bbci.co.uk/sport/rss.xml"),
    ("sports",  "https://www.espn.com/espn/rss/news"),
    ("sports",  "https://feeds.skynews.com/feeds/rss/sports.xml"),
    ("sports",  "https://www.goal.com/feeds/en/news"),

    # ── تکنولوژی ────────────────────────────
    ("tech",    "https://feeds.feedburner.com/TechCrunch"),
    ("tech",    "https://www.theverge.com/rss/index.xml"),
    ("tech",    "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("tech",    "https://feeds.bbci.co.uk/news/technology/rss.xml"),
]

CATEGORY_LABELS = {
    "world":   "🌍 اخبار جهان",
    "iran":    "🇮🇷 اخبار ایران",
    "economy": "📈 اقتصاد",
    "sports":  "⚽ ورزش",
    "tech":    "💻 تکنولوژی",
}

# ─────────────────────────────────────────────
# ۵. توابع کمکی
# ─────────────────────────────────────────────

def clean_html(text: str) -> str:
    """حذف تگ‌های HTML و فاصله‌های اضافی از متن"""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def make_fingerprint(title: str, url: str) -> str:
    """ساخت اثر انگشت منحصر‌به‌فرد برای هر خبر بر اساس هش عنوان"""
    normalized = re.sub(r'[^\w\s]', '', title.lower().strip())
    normalized = re.sub(r'\s+', ' ', normalized)
    return hashlib.md5(normalized.encode()).hexdigest()

def is_duplicate(fingerprint: str, url: str) -> bool:
    """بررسی تکراری بودن خبر با هش عنوان و URL"""
    return fingerprint in history or url in history

def translate_and_summarize(title: str, desc: str, category: str) -> str | None:
    """ارسال خبر به Groq و دریافت متن فارسی آماده انتشار"""
    cat_label = CATEGORY_LABELS.get(category, "📰 خبر")
    prompt = (
        "تو یک خبرنگار حرفه‌ای، بی‌طرف و خلاق برای یک کانال خبری تلگرام فارسی هستی.\n"
        "وظیفه‌ات اینه که خبر زیر رو (که ممکنه انگلیسی یا فارسی باشه) به یک پست جذاب "
        "و کاملاً فارسی تبدیل کنی.\n\n"
        "قوانین مهم:\n"
        "- خلاصه باشه: حداکثر ۵-۶ جمله\n"
        "- از اموجی‌های مرتبط استفاده کن\n"
        "- در پایان دقیقاً ۳ هشتگ فارسی مرتبط بزن\n"
        "- هیچ توضیح اضافه‌ای ننویس؛ فقط متن نهایی پست\n"
        f"- دسته‌بندی خبر: {cat_label}\n\n"
        f"عنوان: {title}\n"
        f"متن: {desc}"
    )

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.7,
        )
        time.sleep(2)  # Groq سریع‌تره؛ sleep کوتاه کافیه
        text = response.choices[0].message.content
        if text:
            return text.strip()
    except Exception as e:
        print(f"  ⚠️  خطای Groq: {e}")
        time.sleep(5)
    return None

def send_to_telegram(text: str) -> bool:
    """ارسال پیام به کانال تلگرام با مدیریت خطا و طول پیام"""
    MAX_LEN = 4000

    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "…"

    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    CHANNEL_ID,
        "text":       text,
        "parse_mode": "HTML",
    }

    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                retry_after = r.json().get("parameters", {}).get("retry_after", 10)
                print(f"  ⏳ Rate limit تلگرام؛ انتظار {retry_after} ثانیه...")
                time.sleep(retry_after)
                continue
            print(f"  ❌ خطای تلگرام {r.status_code}: {r.text}")
        except requests.exceptions.RequestException as e:
            print(f"  ❌ خطای شبکه (تلاش {attempt+1}): {e}")
            time.sleep(5)

    return False

# ─────────────────────────────────────────────
# ۶. جمع‌آوری اخبار از تمام منابع
# ─────────────────────────────────────────────
print(f"\n🕐 شروع اجرا: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print("📡 در حال دریافت اخبار از منابع...")

all_entries = []

for category, feed_url in FEEDS:
    try:
        feed = feedparser.parse(feed_url)
        count = len(feed.entries)
        print(f"  ✅ {feed_url.split('/')[2]} → {count} خبر")
        for entry in feed.entries:
            all_entries.append((category, entry))
    except Exception as e:
        print(f"  ❌ خطا در دریافت {feed_url}: {e}")

print(f"\n📊 مجموع اخبار دریافتی: {len(all_entries)}")

# ─────────────────────────────────────────────
# ۷. بُر زدن تصادفی برای تنوع منابع
# ─────────────────────────────────────────────
random.shuffle(all_entries)

# ─────────────────────────────────────────────
# ۸. پردازش و ارسال اخبار جدید
# ─────────────────────────────────────────────
MAX_PER_RUN   = 3
processed     = 0
skipped_dup   = 0
skipped_empty = 0

print("\n🔄 در حال پردازش اخبار...\n")

for category, entry in all_entries:
    if processed >= MAX_PER_RUN:
        break

    title = clean_html(entry.get("title", "").strip())
    url   = entry.get("link", "").strip()
    desc  = clean_html(entry.get("summary", entry.get("description", "")).strip())

    if not title or not url:
        skipped_empty += 1
        continue

    fingerprint = make_fingerprint(title, url)

    if is_duplicate(fingerprint, url):
        skipped_dup += 1
        continue

    print(f"📰 پردازش: {title[:60]}...")

    farsi_text = translate_and_summarize(title, desc, category)
    if not farsi_text:
        print("  ⚠️  متن فارسی تولید نشد؛ رد شد.")
        continue

    cat_label  = CATEGORY_LABELS.get(category, "📰 خبر")
    final_post = (
        f"{cat_label}\n\n"
        f"{farsi_text}\n\n"
        f"🔗 <a href='{url}'>منبع خبر</a>\n"
        f"📢 {CHANNEL_ID}"
    )

    success = send_to_telegram(final_post)
    if success:
        history.add(fingerprint)
        history.add(url)
        processed += 1
        print(f"  ✅ ارسال شد ({processed}/{MAX_PER_RUN})")
        time.sleep(4)
    else:
        print("  ❌ ارسال ناموفق بود.")

# ─────────────────────────────────────────────
# ۹. ذخیره حافظه و گزارش نهایی
# ─────────────────────────────────────────────
save_history(history)

print("\n" + "─" * 40)
print(f"✅ ارسال شد:        {processed} خبر")
print(f"🔁 تکراری رد شد:   {skipped_dup} خبر")
print(f"⚪ ناقص رد شد:      {skipped_empty} خبر")
print(f"💾 حافظه ذخیره شد: {len(history)} آیتم")
print("─" * 40 + "\n")
