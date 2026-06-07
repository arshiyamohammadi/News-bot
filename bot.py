import os
import json
import math
import feedparser
import requests
import time
import random
import re
import hashlib
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from groq import Groq
from email.utils import parsedate_to_datetime

# ─────────────────────────────────────────────
# ۱. تنظیمات
# ─────────────────────────────────────────────
GROQ_KEYS = [os.environ.get(f"GROQ_KEY_{i}").strip() for i in range(1, 5) if os.environ.get(f"GROQ_KEY_{i}")]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

if not GROQ_KEYS or not TELEGRAM_TOKEN or not CHANNEL_ID:
    raise EnvironmentError("❌ متغیرهای محیطی ضروری تنظیم نشده‌اند.")

print(f"🔑 کلیدهای فعال Groq: {len(GROQ_KEYS)}")

GROQ_MODEL = "llama-3.3-70b-versatile"
HISTORY_FILE = "history.json"
MAX_HISTORY = 500
EVENT_WINDOW_HOURS = 24
MAX_WORKERS = min(len(GROQ_KEYS), 4)
FEED_LIMIT = 10
DELIMITER = "|||NEWS_SEPARATOR|||"
SIGNATURE = "\n\nاخبار روز، ارشیا نیوز😁"

history_lock = threading.Lock()

# ─────────────────────────────────────────────
# ۲. منابع خبری (بازگشت به حالت گوگل نیوز برای همه)
# ─────────────────────────────────────────────
DOMESTIC_FEEDS = [
    ("iranintl",   "https://news.google.com/rss/search?q=site:iranintl.com+when:1d&hl=fa&gl=IR&ceid=IR:fa"),
    ("radiofarda", "https://news.google.com/rss/search?q=site:radiofarda.com+when:1d&hl=fa&gl=IR&ceid=IR:fa"),
    ("tasnim",     "https://news.google.com/rss/search?q=site:tasnimnews.com+when:1d&hl=fa&gl=IR&ceid=IR:fa"),
    ("fars",       "https://news.google.com/rss/search?q=site:farsnews.ir+when:1d&hl=fa&gl=IR&ceid=IR:fa"),
]

FOREIGN_FEEDS = [
    ("reuters", "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("ap",      "https://news.google.com/rss/search?q=site:apnews.com+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("afp",     "https://www.france24.com/en/rss"),
    ("nyt",     "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
]

SOURCE_LABELS = {
    "iranintl": "🇮🇷 ایران اینترنشنال", "radiofarda": "🇮🇷 رادیو فردا",
    "tasnim": "⚠️ تسنیم", "fars": "⚠️ فارس",
    "reuters": "🌍 رویترز", "ap": "🌍 آسوشیتدپرس",
    "afp": "🌍 فرانس‌پرس", "nyt": "🌍 نیویورک تایمز",
}

FOREIGN_KEYWORDS = [
    "iran", "war", "crisis", "famine", "sanction", "nuclear", "attack",
    "missile", "protest", "revolution", "oil", "deal", "election", "coup",
    "refugee", "flood", "earthquake", "assassination", "ceasefire",
    "politics", "military", "economy", "conflict", "tension", "strike",
]

# ─────────────────────────────────────────────
# ۳. پرامپت‌ها
# ─────────────────────────────────────────────
PROMPT_TEMPLATE = """تو یک خبرنگار حرفه‌ای فارسی‌زبان هستی.
خبرهای زیر با جداکننده '{delimiter}' از هم جدا شده‌اند. هر خبر دارای یک [ID] منحصر‌به‌فرد است.

خروجی تو برای هر خبر باید دقیقاً طبق این قالب باشد (حتماً بین هر بخش یک خط خالی بگذار):

[ID خبر]
خلاصه فارسی روان (۳-۵ جمله)

شباهت تاریخی: [نام رویداد + سال + نتیجه]

دومینوی ژئوپلیتیک: [نتیجه احتمالی آینده]
🔴 پروپاگاندا (فقط اگر منبع خبر تسنیم یا فارس بود این خط را اضافه کن): [درصد]٪ — [توضیح کوتاه]

⚠️ قوانین حیاتی:
1. خروجی هر خبر MUST شامل [ID] مربوطه در ابتدای خط اول باشد. مثال: [ID:3]
2. فقط و فقط به زبان فارسی پاسخ بده. تحت هیچ شرایطی از کلمات انگلیسی یا زبان‌های دیگر استفاده نکن.
3. خروجی‌ها را دقیقاً با '{delimiter}' جدا کن و هیچ مقدمه اضافی ننویس.
4. از هیچ‌گونه فرمت‌بندی مارک‌داون (مثل ** یا #) استفاده نکن.
5. بسیار مهم: اگر متن خبر کوتاه بود، فقط از روی عنوان بهترین حدس و تحلیل خودت را بنویس. تحت هیچ شرایطی از عباراتی مثل "اطلاعات کافی نیست"، "بنابراین نمی‌توان پاسخ داد" یا "متن کوتاه است" استفاده نکن.

اخبار:
{batched_news}"""

# ─────────────────────────────────────────────
# ۴. توابع کمکی
# ─────────────────────────────────────────────
def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {} if isinstance(data, list) else data
        except Exception:
            pass
    return {}

def save_history(history: dict):
    with history_lock:
        sorted_items = sorted(
            history.items(),
            key=lambda x: x[1].get("timestamp", ""),
            reverse=True,
        )
        trimmed = dict(sorted_items[:MAX_HISTORY])
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ خطا در ذخیره تاریخچه: {e}")

def normalize(text: str) -> str:
    text = re.sub(r'[^\w\s]', '', text.lower())
    return re.sub(r'\s+', ' ', text).strip()

def get_fingerprint(title: str) -> str:
    return hashlib.md5(normalize(title).encode('utf-8')).hexdigest()

def clean_html(text: str) -> str:
    return re.sub(r'<[^>]+>', ' ', text).strip()

def parse_entry_date(entry) -> datetime:
    try:
        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if hasattr(entry, 'published'):
            return parsedate_to_datetime(entry.published)
        if hasattr(entry, 'updated_parsed') and entry.updated_parsed:
            return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc)

def is_duplicate(history: dict, fp: str, url: str, pub_date: datetime) -> bool:
    with history_lock:
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

def is_important_foreign(title: str, desc: str) -> bool:
    text = (title + " " + desc).lower()
    return any(kw in text for kw in FOREIGN_KEYWORDS)

# ─────────────────────────────────────────────
# ۵. جمع‌آوری اخبار
# ─────────────────────────────────────────────
def collect_news(feeds: list, history: dict, feed_type: str) -> list:
    candidates = []
    # هدرهای تقویت شده برای جلوگیری از خطای 503 گوگل
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    
    for source, url in feeds:
        try:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            
            parsed = feedparser.parse(response.content)
            entries = parsed.entries[:FEED_LIMIT]
            
            for e in entries:
                t = clean_html(e.get("title", ""))
                l = e.get("link", "")
                
                # بررسی متن خبر: اگر متن کوتاه بود، خود عنوان را به جای متن قرار بده تا مدل گیج نشود
                d = clean_html(e.get("summary", e.get("description", "")))
                if not d or len(d) < 15:
                    d = t

                pub_date = parse_entry_date(e)
                fp = get_fingerprint(t)

                if not t or not l:  
                    continue  
                if is_duplicate(history, fp, l, pub_date):  
                    continue  

                if feed_type == "foreign" and not is_important_foreign(t, d):  
                    continue  

                candidates.append({  
                    "title": t, "desc": d, "link": l, "fp": fp,  
                    "pub_date": pub_date, "source": source,  
                    "source_label": SOURCE_LABELS.get(source, source),  
                })  
        except Exception as ex:  
            print(f"❌ خطای RSS برای منبع {source}: {ex}")  
    return candidates

# ─────────────────────────────────────────────
# ۶. پردازش با Groq
# ─────────────────────────────────────────────
def process_batch(news_batch: list, api_key: str) -> dict:
    id_to_fp = {}
    batch_parts = []

    for idx, n in enumerate(news_batch):  
        uid = f"ID:{idx}"  
        id_to_fp[uid] = n["fp"]  
        batch_parts.append(  
            f"[{uid}] [منبع: {n['source_label']}]\nعنوان: {n['title']}\nمتن: {n['desc'][:1500]}"  
        )  

    batched_text = f"\n{DELIMITER}\n".join(batch_parts)  
    prompt = PROMPT_TEMPLATE.format(delimiter=DELIMITER, batched_news=batched_text)  
    client = Groq(api_key=api_key)  
    result_map = {}  

    try:  
        resp = client.chat.completions.create(  
            model=GROQ_MODEL,  
            messages=[{"role": "user", "content": prompt}],  
            max_tokens=4096,  
            temperature=0.5,
        )  
        full_response = resp.choices[0].message.content.strip()  
        analyses = [a.strip() for a in full_response.split(DELIMITER) if a.strip()]  

        for analysis in analyses:  
            match = re.search(r'\[ID:(\d+)\]', analysis)  
            if match:  
                uid = f"ID:{match.group(1)}"  
                if uid in id_to_fp:  
                    clean_analysis = re.sub(r'\[ID:\d+\]\s*', '', analysis).strip()  
                    result_map[id_to_fp[uid]] = clean_analysis  
            else:  
                print(f"⚠️ پاسخ بدون ID یافت شد.")  

    except Exception as e:  
        print(f"❌ خطای Groq: {e}")  

    return result_map

# ─────────────────────────────────────────────
# ۷. ارسال به تلگرام
# ─────────────────────────────────────────────
def send_to_telegram(text: str, max_retries: int = 3) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID, 
        "text": text, 
        "parse_mode": "HTML", 
        "disable_web_page_preview": True
    }

    for attempt in range(max_retries):  
        try:  
            r = requests.post(url, json=payload, timeout=20)  
            if r.status_code == 200:  
                return True  
            if r.status_code == 429:  
                wait = r.json().get("parameters", {}).get("retry_after", 30)  
                print(f"⏳ Rate limit تلگرام، انتظار {wait} ثانیه...")  
                time.sleep(wait)  
                continue  
            print(f"❌ خطای تلگرام: {r.status_code} - {r.text}")  
            break  
        except Exception as e:  
            print(f"❌ خطای اتصال تلگرام: {e}")  
            time.sleep(5)  

    return False

# ─────────────────────────────────────────────
# ۸. اجرای اصلی
# ─────────────────────────────────────────────
def main():
    print(f"\n🕐 شروع: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    history = load_history()

    domestic = collect_news(DOMESTIC_FEEDS, history, "domestic")  
    foreign = collect_news(FOREIGN_FEEDS, history, "foreign")  

    all_news = domestic + foreign  

    print(f"📊 مجموع اخبار جدید: {len(all_news)}")  
    if not all_news:  
        print("✅ خبر جدیدی یافت نشد.")  
        return  

    batch_size = max(1, math.ceil(len(all_news) / MAX_WORKERS))  
    batches = [all_news[i:i + batch_size] for i in range(0, len(all_news), batch_size)]  
    batches = batches[:MAX_WORKERS]  

    fp_to_analysis = {}  
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:  
        future_to_batch = {  
            executor.submit(process_batch, batch, GROQ_KEYS[i % len(GROQ_KEYS)]): batch  
            for i, batch in enumerate(batches)  
        }  

        for future in as_completed(future_to_batch):  
            try:  
                result_map = future.result()  
                fp_to_analysis.update(result_map)  
            except Exception as e:  
                print(f"❌ خطای پردازش دسته: {e}")  

    processed_count = 0  
    for news in all_news:  
        analysis = fp_to_analysis.get(news["fp"])  
        if not analysis:  
            continue  

        msg = (  
            f"📰 <b>{news['source_label']}</b>\n\n"  
            f"{analysis}\n\n"  
            f"🔗 <a href='{news['link']}'>منبع خبر</a>"  
            f"{SIGNATURE}"  
        )  

        if send_to_telegram(msg):  
            with history_lock:  
                history[news["fp"]] = {  
                    "url": news["link"],  
                    "timestamp": news["pub_date"].isoformat(),  
                    "source": news["source"],  
                }  
            processed_count += 1  
            print(f"✅ [{processed_count}/{len(fp_to_analysis)}] {news['title'][:50]}...")  
            time.sleep(random.uniform(2, 6))  

    save_history(history)  
    print(f"\n🏁 پایان: {processed_count} خبر ارسال شد | حافظه: {len(history)} آیتم")

if __name__ == "__main__":
    main()
