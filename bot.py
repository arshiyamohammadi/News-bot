import os
import json
import math
import feedparser
import requests
import time
import random
import re
import hashlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from groq import Groq
from email.utils import parsedate_to_datetime

# ─────────────────────────────────────────────
# ۱. تنظیمات و کلیدها
# ─────────────────────────────────────────────
GROQ_KEYS = [os.environ.get(f"GROQ_KEY_{i}").strip() 
             for i in range(1, 5) if os.environ.get(f"GROQ_KEY_{i}")]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

if not GROQ_KEYS or not TELEGRAM_TOKEN or not CHANNEL_ID:
    raise EnvironmentError("❌ متغیرهای محیطی ضروری تنظیم نشده‌اند.")

print(f"🔑 کلیدهای فعال Groq: {len(GROQ_KEYS)}")

GROQ_MODEL = "llama-3.3-70b-versatile"
HISTORY_FILE = "history.json"
MAX_HISTORY = 150
EVENT_WINDOW_HOURS = 12
MAX_WORKERS = len(GROQ_KEYS)
BATCHES_PER_GROUP = 4
FEED_LIMIT = 20
DELIMITER = "|||NEWS_SEPARATOR|||"

# ─────────────────────────────────────────────
# ۲. منابع خبری
# ─────────────────────────────────────────────
DOMESTIC_FEEDS = [
    ("iran", "https://www.iranintl.com/rss/fa"),
    ("iran", "https://www.radiofarda.com/api/zu_oe-opy"),
    ("tasnim", "https://www.tasnimnews.com/fa/rss"),
    ("fars", "https://www.farsnews.ir/rss"),
]

FOREIGN_FEEDS = [
    ("world", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("world", "https://rss.cnn.com/rss/edition_world.rss"),    ("world", "https://feeds.reuters.com/reuters/worldNews"),
    ("world", "https://english.alarabiya.net/.mrss/en.xml"),
]

SOURCE_LABELS = {
    "world": "🌍 جهان", "iran": "🇮🇷 ایران",
    "tasnim": "⚠️ تسنیم", "fars": "⚠️ فارس"
}

CRISIS_KEYWORDS = [
    "سیاسی", "بحران", "قطع اینترنت", "تظاهرات", "امنیت", "دستگاه قضایی",
    "ناآرامی", "مرگ", "کشتار", "بمب", "جنگ", "توافق", "مذاکره", "تحریم",
    "پرونده", "ادعای", "افشا", "کذب", "تبلیغ", "پروپاگاندا", "نفوذ"
]

# ─────────────────────────────────────────────
# ۳. توابع کمکی و مدیریت حافظه
# ─────────────────────────────────────────────
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {} if isinstance(data, list) else data
        except Exception:
            pass
    return {}

def save_history(history):
    sorted_items = sorted(history.items(), key=lambda x: x[1].get("timestamp", ""), reverse=True)
    trimmed = dict(sorted_items[:MAX_HISTORY])
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)

def normalize(text):
    text = re.sub(r'[^\w\s]', '', text.lower())
    return re.sub(r'\s+', ' ', text).strip()

def get_fingerprint(title):
    return hashlib.md5(normalize(title).encode('utf-8')).hexdigest()

def clean_html(text):
    return re.sub(r'<[^>]+>', ' ', text).strip()

def parse_entry_date(entry):
    """استخراج امن تاریخ انتشار"""
    try:
        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if hasattr(entry, 'published'):            return parsedate_to_datetime(entry.published)
    except Exception:
        pass
    return datetime.now(timezone.utc)

def is_duplicate(history, fp, url, pub_date):
    if fp not in history:
        return False
    record = history[fp]
    if record.get("url") == url:
        return True
    try:
        last_seen = datetime.fromisoformat(record["timestamp"])
        return abs((pub_date - last_seen).total_seconds()) / 3600 < EVENT_WINDOW_HOURS
    except Exception:
        return True

# ─────────────────────────────────────────────
# ۴. پرامپت‌های هوش مصنوعی
# ─────────────────────────────────────────────
PROPAGANDA_PROMPT = """تو یک تحلیلگر رسانه‌ای مستقل هستی. درجه پروپاگاندا این خبر را از ۰ تا ۱۰۰ امتیاز بده.
فقط یک عدد صحیح برگردان. هیچ متن دیگری ننویس.
عنوان: {title}
متن: {desc}"""

BATCH_ANALYSIS_PROMPT = """تو یک تحلیلگر ارشد ژئوپلیتیک و مورخ استراتژیک هستی.
چندین خبر را با جداکننده '{delimiter}' دریافت کرده‌ای.
برای هر خبر، تحلیل زیر را انجام بده و خروجی‌ها را دقیقاً با همان جداکننده '{delimiter}' از هم جدا کن.

قالب تحلیل برای هر خبر:
🏛️ **باستان‌شناسی بحران**
- شباهت تاریخی (نام رویداد و سال) + شباهت استراتژیک + عاقبت تاریخی و سرنوشت امروز

🎯 **دومینوی ژئوپلیتیک**
گام ۱: [اثر سیاسی/نظامی]
گام ۲: [اثر اقتصادی منطقه‌ای]
گام ۳: [اثر زنجیره تأمین جهانی]
گام ۴: [اثر بازار داخلی/معیشت]
گام ۵: [نتیجه ملموس برای شهروند]

⚠️ قوانین حیاتی حفظ داده‌ها:
- تمام آمار، ارقام، درصد‌ها، اسامی خاص، تاریخ‌ها و نقل‌قول‌های مستقیم موجود در متن اصلی را عیناً در تحلیل بیاور
- هرگز داده‌های کمی یا کیفی خبر را خلاصه، گرد یا حذف نکن
- تحلیل باید مکمل داده‌های خبر باشد، نه جایگزین آن‌ها
- لحن تحلیلی و هشداردهنده باشد
- حداکثر ۳۵۰ کلمه برای هر خبر
- ۳ هشتگ فارسی تخصصی در انتهای هر تحلیل
- هیچ مقدمه یا توضیح اضافه‌ای ننویس
- دقیقاً به همان تعداد اخبار ورودی، خروجی بده
اخبار:
{batched_news}"""

# ─────────────────────────────────────────────
# ۵. تعامل با Groq و Telegram
# ─────────────────────────────────────────────
def assess_propaganda(title, desc, source):
    if source not in ["tasnim", "fars"]:
        return 0
    client = Groq(api_key=random.choice(GROQ_KEYS))
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": PROPAGANDA_PROMPT.format(title=title, desc=desc[:1000])}],
            max_tokens=10, temperature=0.1
        )
        raw = resp.choices[0].message.content.strip()
        score = int(re.search(r'\d+', raw).group()) if re.search(r'\d+', raw) else 70
        return max(0, min(100, score))
    except Exception:
        return 75

def process_batch(news_batch):
    batched_text = f"\n{DELIMITER}\n".join([
        f"[منبع: {n['source_label']}]\nعنوان: {n['title']}\nمتن: {n['desc'][:1800]}"
        for n in news_batch
    ])
    
    prompt = BATCH_ANALYSIS_PROMPT.format(delimiter=DELIMITER, batched_news=batched_text)
    client = Groq(api_key=random.choice(GROQ_KEYS))
    
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.6
        )
        full_response = resp.choices[0].message.content.strip()
        analyses = [a.strip() for a in full_response.split(DELIMITER) if a.strip()]
        return analyses
    except Exception as e:
        print(f"❌ خطای پردازش بچ: {e}")
        return []

def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=20)        if r.status_code == 200:
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 30)
            time.sleep(wait)
            return send_to_telegram(text)
    except Exception as e:
        print(f"❌ خطای تلگرام: {e}")
    return False

def collect_news(feeds, history, apply_crisis_filter=False):
    candidates = []
    for cat, url in feeds:
        try:
            entries = feedparser.parse(url).entries[:FEED_LIMIT]
            for e in entries:
                t = clean_html(e.get("title", ""))
                l = e.get("link", "")
                d = clean_html(e.get("summary", e.get("description", "")))
                pub_date = parse_entry_date(e)
                fp = get_fingerprint(t)

                if apply_crisis_filter and cat in ["tasnim", "fars"]:
                    if not any(kw in t.lower() or kw in d.lower() for kw in CRISIS_KEYWORDS):
                        continue

                if t and l and not is_duplicate(history, fp, l, pub_date):
                    candidates.append({
                        "title": t, "desc": d, "link": l,
                        "fp": fp, "pub_date": pub_date,
                        "source": cat, "source_label": SOURCE_LABELS.get(cat, cat)
                    })
        except Exception as ex:
            print(f"❌ خطای RSS {url}: {ex}")
    return candidates

def create_batches(candidates, num_batches):
    if not candidates:
        return []
    batch_size = max(1, math.ceil(len(candidates) / num_batches))
    batches = []
    for i in range(0, len(candidates), batch_size):
        batches.append(candidates[i:i+batch_size])
    return batches[:num_batches]

# ─────────────────────────────────────────────
# ۶. اجرای اصلی
# ─────────────────────────────────────────────
def main():
    print(f"\n🕐 شروع: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")    history = load_history()

    domestic = collect_news(DOMESTIC_FEEDS, history, apply_crisis_filter=True)
    foreign = collect_news(FOREIGN_FEEDS, history, apply_crisis_filter=False)
    
    random.shuffle(domestic)
    random.shuffle(foreign)
    
    domestic_batches = create_batches(domestic, BATCHES_PER_GROUP)
    foreign_batches = create_batches(foreign, BATCHES_PER_GROUP)
    
    all_batches = domestic_batches + foreign_batches
    
    print(f"📊 داخلی: {len(domestic)} خبر → {len(domestic_batches)} دسته")
    print(f"📊 خارجی: {len(foreign)} خبر → {len(foreign_batches)} دسته")
    print(f"📊 مجموع دسته‌ها: {len(all_batches)}")

    if not all_batches:
        print("✅ خبر جدیدی یافت نشد.")
        return

    processed_count = 0
    total_news = sum(len(b) for b in all_batches)
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(process_batch, batch): batch for batch in all_batches}
        
        for future in as_completed(future_map):
            batch = future_map[future]
            analyses = future.result()
            
            for idx, analysis in enumerate(analyses):
                if idx >= len(batch):
                    break
                    
                news = batch[idx]
                prop_score = assess_propaganda(news["title"], news["desc"], news["source"])
                header = f"منبع: {news['source_label']} | پروپاگاندا: {prop_score}%"
                msg = f"{header}\n\n{analysis}\n\n🔗 <a href='{news['link']}'>منبع خبر</a>"
                
                if send_to_telegram(msg):
                    history[news["fp"]] = {
                        "url": news["link"],
                        "timestamp": news["pub_date"].isoformat(),
                        "source": news["source"]
                    }
                    processed_count += 1
                    print(f"✅ [{processed_count}/{total_news}] {news['title'][:40]}...")
                    time.sleep(random.uniform(2, 6))
    save_history(history)
    print(f"\n🏁 پایان: {processed_count} تحلیل ارسال شد | حافظه: {len(history)} آیتم")

if __name__ == "__main__":
    main()
