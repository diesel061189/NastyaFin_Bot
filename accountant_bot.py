import os
import json
import logging
import asyncio
import httpx
import base64
import sqlite3
import tempfile
import feedparser
import re
import io
import random
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ═══
TELEGRAM_TOKEN    = os.getenv("CARD_BOT_TOKEN")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")        # только для голоса если понадобится
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")   # для всего текстового
YOUR_CHAT_ID      = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID      = int(os.getenv("LILU_CHAT_ID", str(os.getenv("YOUR_CHAT_ID", "0"))))
LILU_BOT_TOKEN    = os.getenv("LILU_BOT_TOKEN", "")
DB_PATH           = os.getenv("DB_PATH", "/tmp/freelance.db")
USDT_WALLET       = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY", "")
KWORK_URL           = os.getenv("KWORK_URL", "https://kwork.ru/user/artem_sh")
AIDENTIKA_API_KEY   = os.getenv("AIDENTIKA_API_KEY", "")
AIDENTIKA_BASE      = "https://api.aidentika.com/api/v1/public"

# ═══ ANTHROPIC МОДЕЛИ ═══
ANTHROPIC_HAIKU  = "claude-haiku-4-5-20251001"   # генерация карточек — дёшево
ANTHROPIC_SONNET = "claude-sonnet-4-6"            # анализ сложных заказов
ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"

user_sessions = {}

# ═══ AIDENTIKA API ═══

async def lilu_check_card_quality(product: str, features: str) -> str:
    """
    Лила оценивает нужен ли премиум вариант карточки.
    Возвращает 'classic' или 'premium'
    """
    try:
        prompt = f"""Товар: {product}
Характеристики: {features[:300]}

Оцени одним словом — нужен ли премиум дизайн карточки для этого товара?
Ответь ТОЛЬКО: classic или premium

premium — если товар премиальный (косметика, украшения, техника, бренды)
classic — если обычный товар (хозтовары, простые гаджеты, еда)"""

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": ANTHROPIC_HAIKU,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": prompt}]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
            verdict = r.json()["content"][0]["text"].strip().lower()
            return "premium" if "premium" in verdict else "classic"
    except:
        return "classic"  # по умолчанию экономим искры

async def aidentika_analyze(image_url: str) -> dict:
    """Анализирует фото товара — определяет категорию, название, качества. Бесплатно!"""
    headers = {
        "Authorization": f"Bearer {AIDENTIKA_API_KEY}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{AIDENTIKA_BASE}/analyze",
            headers=headers,
            json={"image": {"url": image_url}}
        )
        if r.status_code == 200:
            return r.json()
        else:
            logger.error(f"Aidentika analyze ошибка: {r.status_code} {r.text[:200]}")
            return {}

async def aidentika_upload(image_b64: str) -> str:
    """Загружает фото на Aidentika и возвращает upload_id"""
    headers = {
        "Authorization": f"Bearer {AIDENTIKA_API_KEY}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{AIDENTIKA_BASE}/upload",
            headers=headers,
            json={"image": {"data": image_b64, "media_type": "image/jpeg"}}
        )
        if r.status_code == 200:
            return r.json().get("upload_id", "")
        else:
            logger.error(f"Aidentika upload ошибка: {r.status_code} {r.text[:200]}")
            return ""

async def aidentika_generate_card(upload_id: str, product_name: str, features: str, style: str = "classic") -> str:
    """Генерирует карточку товара через Aidentika. Возвращает action_id"""
    headers = {
        "Authorization": f"Bearer {AIDENTIKA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "images": [{"data": upload_id}],
        "product_name": product_name,
        "user_text": features,
        "style": style,
        "aspect_ratio": "3:4"
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{AIDENTIKA_BASE}/generate/card",
            headers=headers,
            json=payload
        )
        if r.status_code == 200:
            action_id = str(r.json().get("action_id", ""))
            logger.info(f"✅ Aidentika карточка запущена: action_id={action_id}")
            return action_id
        else:
            logger.error(f"Aidentika generate_card ошибка: {r.status_code} {r.text[:200]}")
            return ""

async def aidentika_generate_photo(upload_id: str, comment: str = "") -> str:
    """Генерирует фото товара на красивом фоне. Возвращает action_id"""
    headers = {
        "Authorization": f"Bearer {AIDENTIKA_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "images": [{"data": upload_id}],
        "aspect_ratio": "3:4",
        "photo_style": "classic"
    }
    if comment:
        payload["comment"] = comment
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{AIDENTIKA_BASE}/generate/photo",
            headers=headers,
            json=payload
        )
        if r.status_code == 200:
            action_id = str(r.json().get("action_id", ""))
            logger.info(f"✅ Aidentika фото запущено: action_id={action_id}")
            return action_id
        else:
            logger.error(f"Aidentika generate_photo ошибка: {r.status_code} {r.text[:200]}")
            return ""

async def aidentika_check_status(action_id: str) -> dict:
    """Проверяем статус генерации"""
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{AIDENTIKA_BASE}/status/{action_id}",
            headers=headers
        )
        if r.status_code == 200:
            return r.json()
        return {}

async def aidentika_download(action_id: str) -> bytes:
    """Скачиваем готовое изображение"""
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(
            f"{AIDENTIKA_BASE}/results/{action_id}/download",
            headers=headers
        )
        if r.status_code == 200:
            return r.content
        return b""

async def aidentika_wait_and_download(action_id: str, max_wait: int = 120) -> bytes:
    """Ждём завершения генерации и скачиваем результат"""
    await asyncio.sleep(20)  # первый запрос не раньше 20 сек
    waited = 20
    while waited < max_wait:
        status = await aidentika_check_status(action_id)
        if status.get("status") == "completed":
            return await aidentika_download(action_id)
        elif status.get("status") == "failed":
            logger.error(f"Aidentika генерация упала: {status}")
            return b""
        await asyncio.sleep(10)
        waited += 10
    logger.error(f"Aidentika timeout action_id={action_id}")
    return b""

async def aidentika_balance() -> int:
    """Проверяем баланс искр"""
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{AIDENTIKA_BASE}/balance", headers=headers)
        if r.status_code == 200:
            return r.json().get("available", 0)
        return -1

HEADERS_LIST = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36", "Accept-Language": "ru-RU,ru;q=0.9"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15", "Accept-Language": "en-US,en;q=0.9"},
]

def get_headers():
    return random.choice(HEADERS_LIST)

# ═══ ANTHROPIC ХЕЛПЕР ═══

async def anthropic_request(
    messages: list,
    system: str = "",
    model: str = None,
    max_tokens: int = 1500,
    image_b64: str = None,
    image_media: str = "image/jpeg"
) -> str:
    if model is None:
        model = ANTHROPIC_HAIKU

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    # Если есть картинка — вставляем в последнее сообщение
    if image_b64 and messages:
        last = messages[-1]
        if isinstance(last.get("content"), str):
            messages[-1] = {
                "role": last["role"],
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": image_media,
                        "data": image_b64
                    }},
                    {"type": "text", "text": last["content"]}
                ]
            }

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
        data = r.json()
        if "content" not in data:
            raise Exception(f"Anthropic error: {data}")
        return data["content"][0]["text"]

# ═══ СТИЛИ КАРТОЧЕК ═══

IMAGE_STYLES = {
    "studio": {
        "name": "🤍 Студийный",
        "desc": "Белый фон, профессиональная съёмка",
        "bg": (255, 255, 255),
        "accent": (67, 97, 238),
        "text": (20, 20, 40),
        "badge_bg": (67, 97, 238),
        "badge_text": (255, 255, 255),
    },
    "dark": {
        "name": "🖤 Тёмный",
        "desc": "Тёмный фон — премиум стиль",
        "bg": (18, 18, 30),
        "accent": (255, 165, 0),
        "text": (255, 255, 255),
        "badge_bg": (255, 165, 0),
        "badge_text": (20, 20, 20),
    },
    "hype": {
        "name": "🔥 Hype",
        "desc": "Яркий, молодёжный",
        "bg": (13, 13, 30),
        "accent": (255, 0, 110),
        "text": (255, 255, 255),
        "badge_bg": (131, 56, 236),
        "badge_text": (255, 255, 255),
    },
    "natural": {
        "name": "🌿 Natural",
        "desc": "Природный — для эко-товаров",
        "bg": (240, 247, 238),
        "accent": (45, 106, 79),
        "text": (30, 60, 40),
        "badge_bg": (64, 145, 108),
        "badge_text": (255, 255, 255),
    },
    "warm": {
        "name": "🧡 Тёплый",
        "desc": "Бежевый — уют и доверие",
        "bg": (253, 245, 235),
        "accent": (180, 90, 30),
        "text": (60, 30, 10),
        "badge_bg": (210, 120, 50),
        "badge_text": (255, 255, 255),
    },
}

# ═══ RSS ИСТОЧНИКИ ═══

CARD_RSS_FEEDS = [
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=3", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://problogger.com/jobs/feed/", "🌍 ProBlogger"),
    ("https://weworkremotely.com/remote-jobs.rss", "🌍 WWR"),
]

TG_CARD_CHANNELS = ["wb_help", "ozon_sellers_club", "kopiraiting_ru", "freelance_ru"]

CARD_KEYWORDS = [
    "карточка товара", "карточки товаров", "описание товара", "описание продукта",
    "wildberries", "вайлдберриз", "wb ", " вб ", "ozon", "озон",
    "яндекс маркет", "маркетплейс", "маркетплейсов",
    "rich контент", "инфографика товар",
    "наполнение карточек", "написать карточку", "заполнить карточку",
    "seo описание", "продающее описание",
    "написать текст", "написать статью", "написать описание",
    "копирайтинг", "копирайтер", "контент для",
    "текст для сайта", "тексты для", "наполнение сайта",
    "продающий текст", "рекламный текст",
    "статья", "пост для", "посты для",
    "перевод", "перевести", "редактура", "корректура",
    "product description", "marketplace content", "amazon listing",
    "product listing", "ecommerce", "product copywriting",
    "amazon seo", "etsy listing", "shopify product",
    "content writing", "copywriting", "article writing",
    "blog post", "translation", "proofreading",
]

CARD_BLACKLIST = [
    "разработка сайта", "программирование", "верстка", "дизайн логотип",
    "видеомонтаж", "анимация", "таргет", "реклама настройка",
    "мобильное приложение", "android", "ios",
    "допечатная", "фотозона", "широкоформатная печать",
    "indesign", "illustrator", "photoshop макет",
    "чертёж", "чертеж", "чертежник", "autocad", "solidworks",
    "написать работу", "курсовая", "дипломная", "реферат",
    "купить и отправить", "купить в городе", "доставить", "курьер",
    "отправить посылку", "пвз", "cdek", "сдэк",
]

# ═══ БАЗА ДАННЫХ ═══

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, description TEXT,
        budget TEXT, url TEXT, source TEXT,
        status TEXT DEFAULT 'found', result TEXT,
        created_at TEXT, updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seen_jobs (url TEXT PRIMARY KEY, seen_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT, amount_usd REAL, amount_rub REAL,
        date TEXT, description TEXT
    )''')
    conn.commit()
    conn.close()

def save_job(job):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO jobs
        (id, title, description, budget, url, source, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (job['id'], job['title'], job['description'], job['budget'],
         job['url'], job['source'], job['status'],
         job['created_at'], job['updated_at']))
    conn.commit()
    conn.close()

def update_job(job_id, status, result=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if result:
        c.execute('UPDATE jobs SET status=?, result=?, updated_at=? WHERE id=?',
                  (status, result, datetime.now().isoformat(), job_id))
    else:
        c.execute('UPDATE jobs SET status=?, updated_at=? WHERE id=?',
                  (status, datetime.now().isoformat(), job_id))
    conn.commit()
    conn.close()

def get_job(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id,title,description,budget,url,source,status,result FROM jobs WHERE id=?', (job_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(zip(['id','title','description','budget','url','source','status','result'], row))
    return None

def is_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_jobs WHERE url=?', (url,))
    r = c.fetchone()
    conn.close()
    return r is not None

def mark_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO seen_jobs (url, seen_at) VALUES (?, ?)',
              (url, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_earning(job_id, amount_usd, amount_rub, description):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO earnings (job_id, amount_usd, amount_rub, date, description) VALUES (?, ?, ?, ?, ?)',
              (job_id, amount_usd, amount_rub, datetime.now().isoformat(), description))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')
    by_status = dict(c.fetchall())
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0), COUNT(*) FROM earnings')
    earn = c.fetchone()
    conn.close()
    return {'by_status': by_status, 'earn_usd': earn[0], 'earn_rub': earn[1], 'earn_count': earn[2]}

def clean_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&amp;|&lt;|&gt;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def make_id(url):
    return str(abs(hash(url)) % (10**12))

def is_card_job(title, desc):
    text = (title + " " + desc).lower()
    for bad in CARD_BLACKLIST:
        if bad in text:
            return False
    return any(kw in text for kw in CARD_KEYWORDS)

# ═══ ПАРСЕРЫ ═══

async def parse_card_jobs(client) -> list:
    jobs = []
    for url, source in CARD_RSS_FEEDS:
        try:
            headers = get_headers()
            headers['Accept'] = 'application/rss+xml,application/xml,text/xml,*/*'
            r = await client.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.text)
            if not feed.entries:
                continue
            logger.info(f"{source}: {len(feed.entries)} записей")
            for e in feed.entries[:10]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc  = clean_html(e.get('summary', e.get('description', '')))
                budget_m = re.search(r'[\$₽€]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|\$|₽)', desc + title)
                budget = budget_m.group(0).strip() if budget_m else "Договорная"
                if is_card_job(title, desc):
                    jobs.append({
                        'id': make_id(link), 'title': title[:200],
                        'description': desc[:1200], 'budget': budget,
                        'url': link, 'source': f'🛍️ {source}',
                        'status': 'found',
                        'created_at': datetime.now().isoformat(),
                        'updated_at': datetime.now().isoformat()
                    })
                    mark_seen(link)
        except Exception as e:
            logger.error(f"❌ {source}: {e}")
    return jobs

async def parse_tg_card_channels(client) -> list:
    jobs = []
    for channel in TG_CARD_CHANNELS:
        try:
            r = await client.get(f"https://t.me/s/{channel}", headers=get_headers(), timeout=15)
            if r.status_code != 200:
                continue
            posts = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            for post_html in posts[:5]:
                text = clean_html(post_html)
                if len(text) < 40 or not is_card_job(text, ""):
                    continue
                post_url = f"https://t.me/{channel}/p_{abs(hash(text)) % 100000}"
                if is_seen(post_url):
                    continue
                budget_m = re.search(r'[\$₽]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|\$|₽)', text)
                budget = budget_m.group(0).strip() if budget_m else "Договорная"
                jobs.append({
                    'id': make_id(post_url), 'title': text[:60] + "...",
                    'description': text[:1000], 'budget': budget,
                    'url': f"https://t.me/{channel}",
                    'source': f'📱 TG @{channel}', 'status': 'found',
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(post_url)
        except Exception as e:
            logger.error(f"❌ TG {channel}: {e}")
    return jobs

# ═══ ПРОМПТЫ МАРКЕТПЛЕЙСОВ ═══

MARKETPLACE_PROMPTS = {
    "wb": """Создай продающую карточку для Wildberries. Верни ТОЛЬКО JSON без пояснений:
{"title": "заголовок до 100 символов с ключевыми словами", "description": "продающее описание 500-800 символов", "characteristics": ["характеристика 1", "характеристика 2", "характеристика 3", "характеристика 4", "характеристика 5"], "keywords": "ключевые слова через запятую 10-15 штук", "seo_tips": "краткий SEO совет", "badges": ["✅ Быстрая доставка", "⭐ Топ продаж", "🎁 Гарантия качества"]}""",

    "ozon": """Создай карточку для Ozon. Верни ТОЛЬКО JSON без пояснений:
{"title": "название до 200 символов", "description": "подробное описание 1000-2000 символов с буллетами", "rich_content": [{"heading": "Преимущества", "text": "текст"}], "attributes": ["атрибут 1", "атрибут 2", "атрибут 3", "атрибут 4", "атрибут 5"], "keywords": "ключевые слова", "badges": ["✅ Оригинал", "🚀 Быстро", "💎 Качество"]}""",

    "ym": """Создай карточку для Яндекс Маркет. Верни ТОЛЬКО JSON без пояснений:
{"title": "точное полное название товара", "description": "описание до 2000 символов", "specs": {"Материал": "значение", "Размер": "значение", "Цвет": "значение", "Вес": "значение"}, "tags": ["тег 1", "тег 2", "тег 3"], "category_tips": "совет по категории", "badges": ["✅ Сертифицировано", "🏆 Бестселлер", "🎯 Выгодно"]}""",

    "amazon": """Create an Amazon product listing. Return ONLY JSON without explanation:
{"title": "SEO title max 200 chars with main keywords", "bullet_points": ["benefit 1 with keyword", "benefit 2 with keyword", "benefit 3 with keyword", "benefit 4 with keyword", "benefit 5 with keyword"], "description": "detailed description 2000 chars", "keywords": "backend search terms", "badges": ["✅ Prime Ready", "⭐ Top Rated", "🎁 Gift Ready"]}""",

    "etsy": """Create an Etsy listing. Return ONLY JSON without explanation:
{"title": "handmade-focused title with keywords max 140 chars", "description": "story-driven description 2000 chars", "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10", "tag11", "tag12", "tag13"], "materials": ["material1", "material2"], "badges": ["🤝 Handmade", "💚 Eco-friendly", "⭐ Custom Orders"]}""",
}

# ═══ GEMINI ГЕНЕРАЦИЯ ФОТО ═══

async def generate_product_image_gemini(product_name: str, style_key: str = "studio") -> bytes | None:
    style = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
    prompt = (
        f"Create a professional product photo for marketplace listing. "
        f"Product: {product_name}. Style: clean commercial photography, {style['desc'].lower()}. "
        f"Show only the product, no people, no hands, no text overlay. High quality e-commerce photo."
    )
    GEMINI_MODELS = [
        "gemini-2.5-flash-preview-05-20",
        "gemini-2.0-flash-preview-image-generation",
    ]
    if not GEMINI_API_KEY:
        return None
    for model in GEMINI_MODELS:
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
                    }
                )
                if r.status_code == 200:
                    data = r.json()
                    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                        if "inlineData" in part:
                            img_data = part["inlineData"].get("data", "")
                            if img_data:
                                logger.info(f"✅ Gemini [{model}] — фото готово!")
                                return base64.b64decode(img_data)
        except Exception as e:
            logger.error(f"Gemini [{model}]: {e}")
    return None

# ═══ PILLOW ИНФОГРАФИКА ═══

def build_infographic(product_name, card_data, marketplace, style_key="studio", product_photo_bytes=None) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    style = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
    W, H = 900, 1200
    img  = Image.new('RGB', (W, H), color=style['bg'])
    draw = ImageDraw.Draw(img)

    bg       = style['bg']
    accent   = style['accent']
    txt_col  = style['text']
    badge_bg  = style['badge_bg']
    badge_txt = style['badge_text']

    # Градиент сверху
    for y in range(180):
        alpha = int(255 * (1 - y / 180))
        blended = tuple(
            int(min(accent[i]+40,255) * alpha/255 + bg[i] * (1-alpha/255))
            for i in range(3)
        )
        draw.line([(0, y), (W, y)], fill=blended)

    draw.rectangle([0, 0, W, 8], fill=accent)

    # Маркетплейс бейдж
    mp_labels = {"wb": "WILDBERRIES", "ozon": "OZON", "ym": "ЯНДЕКС МАРКЕТ", "amazon": "AMAZON", "etsy": "ETSY"}
    mp_colors = {"wb": (147,0,211), "ozon": (0,91,255), "ym": (255,204,0), "amazon": (255,153,0), "etsy": (235,94,40)}
    mp_color   = mp_colors.get(marketplace, accent)
    mp_txt_col = (255,255,255) if marketplace not in ("ym",) else (20,20,20)
    draw.rectangle([20, 18, 230, 52], fill=mp_color)
    draw.text((125, 35), mp_labels.get(marketplace, "МАРКЕТПЛЕЙС"), anchor="mm", fill=mp_txt_col)

    # Название
    title = card_data.get("title", product_name)
    for i, line in enumerate(textwrap.wrap(title, width=28)[:3]):
        draw.text((W//2, 70 + i*38), line, anchor="mm", fill=txt_col)

    # Зона фото
    px0, py0, px1, py1 = 60, 185, W-60, 620
    if product_photo_bytes:
        try:
            prod_img = Image.open(io.BytesIO(product_photo_bytes)).convert("RGBA")
            prod_img.thumbnail((px1-px0, py1-py0), Image.LANCZOS)
            px = px0 + (px1-px0-prod_img.width)//2
            py = py0 + (py1-py0-prod_img.height)//2
            draw.rectangle([px0, py0, px1, py1], fill=(255,255,255) if style_key=="studio" else tuple(min(c+30,255) for c in bg))
            draw.rectangle([px0, py0, px1, py1], outline=accent, width=2)
            img.paste(prod_img, (px, py), prod_img if prod_img.mode=='RGBA' else None)
        except Exception as e:
            logger.error(f"Фото вставка: {e}")
            _placeholder(draw, px0, py0, px1, py1, accent, bg, product_name)
    else:
        _placeholder(draw, px0, py0, px1, py1, accent, bg, product_name)

    # Бейджи
    badges = card_data.get("badges", ["✅ Доставка", "⭐ Топ продаж", "🎁 Гарантия"])
    by = py1 + 15
    bw = (W-60) // len(badges[:3])
    for i, badge in enumerate(badges[:3]):
        bx0, bx1 = 30 + i*bw, 30 + i*bw + bw - 10
        draw.rectangle([bx0, by, bx1, by+36], fill=badge_bg)
        draw.text(((bx0+bx1)//2, by+18), str(badge)[:22], anchor="mm", fill=badge_txt)

    # Характеристики
    cy = by + 55
    draw.rectangle([30, cy-5, W-30, cy+2], fill=accent)
    cy += 15
    chars = []
    if marketplace == "wb":
        chars = card_data.get("characteristics", [])
    elif marketplace == "ozon":
        chars = card_data.get("attributes", [])
    elif marketplace == "ym":
        chars = [f"{k}: {v}" for k, v in card_data.get("specs", {}).items()]
    elif marketplace == "amazon":
        chars = card_data.get("bullet_points", [])
    elif marketplace == "etsy":
        chars = [f"Материал: {m}" for m in card_data.get("materials", [])] + card_data.get("tags", [])[:4]

    for i, char in enumerate(chars[:6]):
        row_bg = tuple(max(c-10,0) if i%2==0 else c for c in bg)
        draw.rectangle([30, cy-4, W-30, cy+28], fill=row_bg)
        draw.rectangle([30, cy+4, 36, cy+20], fill=accent)
        draw.text((50, cy+12), str(char)[:55], anchor="lm", fill=txt_col)
        cy += 38

    # Ключевые слова
    kw_y = max(cy+15, 920)
    kw = card_data.get("keywords", "") or ", ".join(card_data.get("tags", [])[:5])
    if kw:
        draw.rectangle([30, kw_y, W-30, kw_y+35], fill=tuple(max(c-20,0) for c in bg))
        draw.text((W//2, kw_y+17), f"🔍 {str(kw)[:80]}", anchor="mm", fill=accent)

    # Нижняя панель
    draw.rectangle([0, H-110, W, H], fill=accent)
    desc = card_data.get("description", "")[:120].replace("\n", " ")
    for i, line in enumerate(textwrap.wrap(desc, width=60)[:2]):
        draw.text((W//2, H-90+i*28), line, anchor="mm", fill=(255,255,255))
    draw.text((W//2, H-25), "✅ SEO-оптимизировано  •  ✅ Готово для загрузки", anchor="mm", fill=(220,255,220))

    buf = io.BytesIO()
    img.save(buf, format='PNG', quality=95)
    buf.seek(0)
    return buf.read()

def _placeholder(draw, x0, y0, x1, y1, accent, bg, name):
    ph_bg = tuple(min(c+25,255) for c in bg)
    draw.rectangle([x0, y0, x1, y1], fill=ph_bg)
    draw.rectangle([x0, y0, x1, y1], outline=accent, width=2)
    cx, cy = (x0+x1)//2, (y0+y1)//2
    draw.text((cx, cy-40), "📦", anchor="mm", fill=accent)
    draw.text((cx, cy+20), name[:30], anchor="mm", fill=accent)
    draw.text((cx, cy+55), "Отправь фото товара", anchor="mm", fill=tuple(max(c-60,0) for c in accent))

async def generate_product_image(product_name, card_data, marketplace, style_key="studio", photo_bytes=None) -> bytes:
    if photo_bytes:
        return build_infographic(product_name, card_data, marketplace, style_key, photo_bytes)
    gemini_photo = await generate_product_image_gemini(product_name, style_key)
    return build_infographic(product_name, card_data, marketplace, style_key, gemini_photo)

# ═══ ГЕНЕРАЦИЯ КАРТОЧКИ — ANTHROPIC ═══

async def generate_single(product: str, marketplace: str, image_b64: str = None) -> dict:
    """Генерирует карточку через Anthropic Haiku — дёшево и качественно"""
    prompt = f"ТОВАР: {product}\n\n{MARKETPLACE_PROMPTS[marketplace]}"

    text = await anthropic_request(
        messages=[{"role": "user", "content": prompt}],
        model=ANTHROPIC_HAIKU,
        max_tokens=1500,
        image_b64=image_b64
    )

    # Чистим JSON
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    else:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1:
            text = text[start:end+1]
    return json.loads(text)

async def generate_card(product: str, marketplace: str, image_b64: str = None) -> dict:
    if marketplace == "all":
        results = {}
        for mp in ["wb", "ozon", "ym"]:
            results[mp] = await generate_single(product, mp, image_b64)
            await asyncio.sleep(0.5)
        return results
    return await generate_single(product, marketplace, image_b64)

async def execute_card_job(job: dict) -> str:
    """Выполняет заказ с биржи — Anthropic Haiku"""
    prompt = f"""Выполни заказ на написание карточек товаров профессионально.

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:800]}

Создай готовую карточку товара:
1. Продающий заголовок с ключевыми словами
2. SEO описание 500-800 символов
3. Список характеристик (5-7 пунктов)
4. Ключевые слова для поиска (10-15 штук)
5. Совет по оптимизации

Отвечай на языке заказа. Будь конкретным и профессиональным."""

    return await anthropic_request(
        messages=[{"role": "user", "content": prompt}],
        model=ANTHROPIC_HAIKU,
        max_tokens=2000
    )

async def redo_card_job(original: str, fix_instruction: str) -> str:
    """Правка карточки — Anthropic Haiku"""
    return await anthropic_request(
        messages=[{"role": "user", "content":
            f"Исправь карточку товара согласно инструкции.\n\n"
            f"ОРИГИНАЛ:\n{original[:2000]}\n\n"
            f"ИНСТРУКЦИЯ: {fix_instruction}\n\n"
            f"Верни полный исправленный текст."}],
        model=ANTHROPIC_HAIKU,
        max_tokens=2000
    )

# ═══ ОТПРАВКА ЛИЛЕ ═══

async def send_job_to_lilu(bot, job: dict):
    """
    Карточник сохраняет заказ в БД со статусом pending_lilu.
    Лила сама заберёт его через опрос БД каждые 30 сек.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        source = f"Карточник | {job.get('source', '')}"
        c.execute(
            "UPDATE jobs SET status='pending_lilu', source=?, updated_at=? WHERE id=?",
            (source[:200], datetime.now().isoformat(), job['id'])
        )
        conn.commit()
        conn.close()
        logger.info(f"📨 Карточник → БД (pending_lilu): {job.get('title','')[:50]}")
    except Exception as e:
        logger.error(f"❌ Ошибка записи в БД: {e}")

async def check_card_jobs(bot) -> int:
    count = 0
    async with httpx.AsyncClient() as client:
        jobs = await parse_card_jobs(client)
        jobs += await parse_tg_card_channels(client)
    for job in jobs:
        save_job(job)
        await send_job_to_lilu(bot, job)
        count += 1
        await asyncio.sleep(2)
    logger.info(f"📋 Карточник нашёл и отправил Лиле: {count} заказов")
    return count

# ═══ ФОРМАТИРОВАНИЕ КАРТОЧКИ ═══

def format_card(data: dict, marketplace: str) -> str:
    if marketplace == "wb":
        chars = "\n".join([f" • {c}" for c in data.get('characteristics', [])])
        return (f"🟣 *WILDBERRIES*\n\n📌 *Заголовок:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"📋 *Характеристики:*\n{chars}\n\n"
                f"🔍 *Ключевые слова:*\n`{data.get('keywords','')}`\n\n"
                f"💡 _{data.get('seo_tips','')}_")
    elif marketplace == "ozon":
        attrs = "\n".join([f" • {a}" for a in data.get('attributes', [])])
        rich  = "".join([f"\n*{s.get('heading','')}*\n{s.get('text','')}\n" for s in data.get('rich_content', [])])
        return (f"🔵 *OZON*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"🎨 *Rich-контент:*{rich}\n📋 *Атрибуты:*\n{attrs}\n\n"
                f"🔍 `{data.get('keywords','')}`")
    elif marketplace == "ym":
        specs = "\n".join([f" • {k}: {v}" for k, v in data.get('specs', {}).items()])
        tags  = ", ".join(data.get('tags', []))
        return (f"🟡 *ЯНДЕКС МАРКЕТ*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"⚙️ *Характеристики:*\n{specs}\n\n"
                f"🏷️ `{tags}`\n\n💡 _{data.get('category_tips','')}_")
    elif marketplace == "amazon":
        bullets = "\n".join([f" • {b}" for b in data.get('bullet_points', [])])
        return (f"🟠 *AMAZON*\n\n📌 *Title:*\n`{data.get('title','')}`\n\n"
                f"📝 *Description:*\n{data.get('description','')}\n\n"
                f"✅ *Bullet Points:*\n{bullets}\n\n"
                f"🔍 *Backend Keywords:*\n`{data.get('keywords','')}`")
    elif marketplace == "etsy":
        tags = ", ".join(data.get('tags', []))
        mats = ", ".join(data.get('materials', []))
        return (f"🟢 *ETSY*\n\n📌 *Title:*\n`{data.get('title','')}`\n\n"
                f"📝 *Description:*\n{data.get('description','')}\n\n"
                f"🏷️ *Tags:* `{tags}`\n\n🔧 *Materials:* {mats}")
    return str(data)

# ═══ STARS INVOICE ═══

async def send_stars_invoice(update, context, stars, title, description):
    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=title, description=description,
            payload=f"card_order_{stars}_{update.effective_user.id}",
            currency="XTR",
            prices=[{"label": title, "amount": stars}],
            provider_token=""
        )
    except Exception as e:
        logger.error(f"Stars invoice: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⭐ Оплата {stars} Stars — напиши нам и выставим счёт вручную."
        )

# ═══ ОТПРАВКА РЕЗУЛЬТАТА ═══

async def send_card_result(message, result, marketplace, product, bot, user_id=None, skip_image=False):
    """
    skip_image=True — когда уже отправили карточку через Aidentika,
    не надо запускать старый Gemini генератор
    """
    if marketplace == "all":
        for mp, data in result.items():
            await bot.send_message(chat_id=message.chat_id, text=format_card(data, mp)[:4000], parse_mode='Markdown')
            await asyncio.sleep(0.5)
        # Старый генератор запускаем только если нет Aidentika
        if not skip_image:
            mp0, data0 = list(result.keys())[0], list(result.values())[0]
            photo_b = user_sessions.get(user_id, {}).get("last_photo_bytes") if user_id else None
            img_bytes = await generate_product_image(product, data0, mp0, "studio", photo_b)
            if img_bytes:
                await bot.send_photo(
                    chat_id=message.chat_id, photo=img_bytes,
                    caption="🖼 *Инфографика готова!*\n\n💡 Выбери стиль:",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(_style_keyboard("studio", product[:15]))
                )
    else:
        await bot.send_message(chat_id=message.chat_id, text=format_card(result, marketplace)[:4000], parse_mode='Markdown')
        # Старый генератор запускаем только если нет Aidentika
        if not skip_image:
            photo_b = user_sessions.get(user_id, {}).get("last_photo_bytes") if user_id else None
            img_bytes = await generate_product_image(product, result, marketplace, "studio", photo_b)
            if img_bytes:
                await bot.send_photo(
                    chat_id=message.chat_id, photo=img_bytes,
                    caption=(f"🖼 *Студийный стиль*\n\n"
                             f"✅ {'На основе вашего фото!' if photo_b else 'Инфографика готова!'}\n\n"
                             f"💡 Выбери другой стиль:"),
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(_style_keyboard("studio", product[:15]))
                )

def _style_keyboard(current_style, product_short):
    rows = []
    for sk, sd in IMAGE_STYLES.items():
        emoji = "✅ " if sk == current_style else ""
        rows.append([InlineKeyboardButton(
            f"{emoji}{sd['name']} — {sd['desc']}",
            callback_data=f"style_{sk}_{product_short}"
        )])
    return rows

def _card_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟣 WB",      callback_data="mp_wb"),
         InlineKeyboardButton("🔵 Ozon",   callback_data="mp_ozon"),
         InlineKeyboardButton("🟡 ЯМ",     callback_data="mp_ym")],
        [InlineKeyboardButton("🟠 Amazon", callback_data="mp_amazon"),
         InlineKeyboardButton("🟢 Etsy",   callback_data="mp_etsy"),
         InlineKeyboardButton("🎯 Все RU", callback_data="mp_all")],
        [InlineKeyboardButton("🧠 Что умею",    callback_data="card_skills"),
         InlineKeyboardButton("🛍️ Наши кворки", callback_data="card_kwork")],
        [InlineKeyboardButton("💰 Прайс",       callback_data="card_price"),
         InlineKeyboardButton("📊 Статистика",   callback_data="card_stats_btn")],
    ])

# ═══ КНОПКИ ═══

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = update.effective_user.id

    # Выбор маркетплейса
    if data.startswith("mp_"):
        marketplace = data[3:]
        user_sessions[user_id] = {"marketplace": marketplace, "step": "waiting_product"}
        names = {"wb":"🟣 Wildberries","ozon":"🔵 Ozon","ym":"🟡 Яндекс Маркет",
                 "amazon":"🟠 Amazon","etsy":"🟢 Etsy","all":"🎯 Все RU"}
        await query.edit_message_text(
            f"*{names.get(marketplace,'?')}* выбран!\n\n"
            f"📸 Отправь *фото товара* (лучше) или *название*\n\n"
            f"_Фото товара → инфографика с реальным снимком_",
            parse_mode='Markdown'
        )

    # Смена стиля инфографики
    elif data.startswith("style_") or data.startswith("restyle_"):
        parts     = data.split("_", 2)
        style_key = parts[1]
        session   = user_sessions.get(user_id, {})
        product   = session.get("last_product", "товар")
        mp        = session.get("last_marketplace", "wb")
        card_data = session.get("last_card_data", {})
        photo_b   = session.get("last_photo_bytes")
        try:
            await query.edit_message_text("⏳ Применяю стиль...")
        except:
            pass
        try:
            img_bytes = await generate_product_image(product, card_data, mp, style_key, photo_b)
            await context.bot.send_photo(
                chat_id=update.effective_chat.id, photo=img_bytes,
                caption=(f"🖼 *{IMAGE_STYLES[style_key]['name']}*\n"
                         f"_{IMAGE_STYLES[style_key]['desc']}_\n\n"
                         f"✅ Готово!\n\n💡 Другой стиль:"),
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(_style_keyboard(style_key, product[:15]))
            )
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ {str(e)[:100]}")

    # Регенерация
    elif data.startswith("regen_"):
        parts   = data.split("_", 2)
        mp      = parts[1]
        product = parts[2] if len(parts) > 2 else ""
        await query.edit_message_text("⏳ Генерирую заново...")
        try:
            result = await generate_card(product, mp)
            await send_card_result(query.message, result, mp, product, context.bot, user_id)
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:100]}")

    elif data == "card_skills":
        await query.edit_message_text(
            "🛍️ *КАРТОЧНИК — ЧТО УМЕЮ*\n\n"
            "📦 *Маркетплейсы RU:*\n"
            " • Wildberries — карточки, SEO, ключи\n"
            " • Ozon — карточки, rich-контент\n"
            " • Яндекс Маркет — карточки, атрибуты\n\n"
            "🌍 *Маркетплейсы EN:*\n"
            " • Amazon — product listings, SEO\n"
            " • Etsy — handmade listings\n\n"
            "🖼 *Инфографика 5 стилей:*\n"
            " Студийный / Тёмный / Hype / Natural / Тёплый\n\n"
            "🤖 *Работа с заказами:*\n"
            " • Ищу заказы на биржах каждые 30 мин\n"
            " • Все заказы фильтрует *Лила* → лучшие тебе!\n"
            " • Выполняю и сдаю на проверку Лиле",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="card_back_main")]])
        )

    elif data == "card_kwork":
        await query.edit_message_text(
            "🛍️ *НАШИ КВОРКИ НА KWORK*\n\n"
            "📦 *Карточки WB/Ozon/ЯМ:*\n"
            " • Эконом (текст): 400₽\n"
            " • Стандарт (текст + SEO): 1200₽\n"
            " • Бизнес (текст + SEO + инфографика): 2000₽\n\n"
            "🌍 *Amazon/Etsy:*\n"
            " • от $8 за listing\n\n"
            f"🔗 [Все кворки на Kwork]({KWORK_URL})",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Открыть Kwork", url=KWORK_URL),
                InlineKeyboardButton("◀️ Назад", callback_data="card_back_main")
            ]])
        )

    elif data == "card_price":
        await query.edit_message_text(
            "💰 *ПРАЙС*\n\n"
            "🟢 Эконом — 1 карточка: $5 / 50⭐ / 400₽\n"
            "🔵 Стандарт — 5 карточек: $20 / 200⭐\n"
            "🟣 Бизнес — 10 карточек: $35 / 350⭐\n"
            "🌍 Amazon/Etsy — от $8 за listing",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 USDT",           callback_data="pay_usdt")],
                [InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars")],
                [InlineKeyboardButton("🇷🇺 Рубли",         callback_data="pay_rub")],
                [InlineKeyboardButton("◀️ Назад",          callback_data="card_back_main")],
            ])
        )

    elif data == "card_stats_btn":
        stats = get_stats()
        bs    = stats['by_status']
        await query.edit_message_text(
            f"📊 *СТАТИСТИКА*\n\n"
            f"🔍 Найдено: {bs.get('found',0)}\n"
            f"✅ Принято: {bs.get('accepted',0)}\n"
            f"✨ Выполнено: {bs.get('completed',0)}\n"
            f"💰 Закрыто: {bs.get('done',0)}\n"
            f"⏭ Пропущено: {bs.get('skipped',0)}\n\n"
            f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="card_back_main")]])
        )

    elif data == "card_back_main":
        await query.edit_message_text(
            "🛍️ *КарточникБот* — выбери действие:",
            parse_mode='Markdown',
            reply_markup=_card_main_keyboard()
        )

    elif data == "pay_usdt":
        await query.edit_message_text(
            "💎 *Оплата в USDT*\n\nВыбери пакет:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1 карточка — $5",   callback_data="invoice_5")],
                [InlineKeyboardButton("5 карточек — $20",  callback_data="invoice_20")],
                [InlineKeyboardButton("10 карточек — $35", callback_data="invoice_35")],
                [InlineKeyboardButton("50 карточек — $150",callback_data="invoice_150")],
                [InlineKeyboardButton("✏️ Своя сумма",     callback_data="invoice_custom")],
            ])
        )

    elif data == "pay_stars":
        await query.edit_message_text(
            "⭐ *Telegram Stars*\n\n"
            "50⭐ = 1 карточка\n200⭐ = 5 карточек\n"
            "350⭐ = 10 карточек\n1500⭐ = 50 карточек",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ 50 Stars",   callback_data="stars_50")],
                [InlineKeyboardButton("⭐ 200 Stars",  callback_data="stars_200")],
                [InlineKeyboardButton("⭐ 350 Stars",  callback_data="stars_350")],
                [InlineKeyboardButton("⭐ 1500 Stars", callback_data="stars_1500")],
            ])
        )

    elif data == "pay_rub":
        await query.edit_message_text(
            "🇷🇺 *Оплата в рублях*\n\n"
            "Напиши `/order 5 карточек для WB`\n\n"
            "Способы: СБП • ЮMoney • QIWI",
            parse_mode='Markdown'
        )

    elif data.startswith("invoice_"):
        amount_str = data[8:]
        if amount_str == "custom":
            context.user_data['awaiting_custom_amount'] = True
            await query.edit_message_text("✏️ Напиши сумму в USD:\n\nПример: `25`", parse_mode='Markdown')
        else:
            amount = float(amount_str)
            descriptions = {5:"1 product listing",20:"5 product listings",35:"10 product listings",150:"50 product listings"}
            desc = descriptions.get(amount, f"${amount} package")
            msg = (f"💎 *INVOICE / СЧЁТ*\n\n📋 Service: *{desc}*\n💰 Amount: *${amount:.2f} USDT*\n\n"
                   f"━━━━━━━━━━━━━━━━\n📲 *Payment via @wallet:*\n\n"
                   f"1️⃣ Open @wallet in Telegram\n2️⃣ Send → Crypto → USDT TRC20\n"
                   f"3️⃣ Paste address:\n`{USDT_WALLET}`\n"
                   f"4️⃣ Amount: `{amount}` USDT\n\n━━━━━━━━━━━━━━━━\n"
                   f"⚡ After payment tap button below")
            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Оплатил!", callback_data=f"payment_confirm_{amount}"),
                InlineKeyboardButton("❌ Отмена",   callback_data="card_back_main")
            ]]))

    elif data.startswith("payment_confirm_"):
        amount   = float(data[16:])
        user     = update.effective_user
        username = f"@{user.username}" if user.username else user.first_name
        await context.bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=f"💰 *ОПЛАТА!*\n\n👤 {username}\n💎 ${amount:.2f} USDT\n🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n⚡ Проверь кошелёк!",
            parse_mode='Markdown'
        )
        await query.edit_message_text(
            f"✅ *Спасибо!*\n\nОплата ${amount:.2f} USDT подтверждена.\nНачинаем через 5 мин!\n\n📱 Отправь данные товара",
            parse_mode='Markdown'
        )
        user_sessions[user.id] = {"step": "waiting_product", "marketplace": "all", "paid": True}

    elif data.startswith("stars_"):
        stars = int(data[6:])
        stars_map = {
            50:  ("1 карточка товара",    "Профессиональная карточка для WB, Ozon или Amazon"),
            200: ("5 карточек товаров",   "5 профессиональных карточек"),
            350: ("10 карточек товаров",  "10 карточек — скидка 30%"),
            1500:("50 карточек товаров",  "50 карточек — скидка 40%"),
        }
        title, description = stars_map.get(stars, ("Карточки", "Профессиональные карточки"))
        await send_stars_invoice(update, context, stars, title, description)

    elif data.startswith("take_"):
        job_id = data[5:]
        job    = get_job(job_id)
        if not job:
            await query.edit_message_text("❌ Заказ не найден")
            return
        update_job(job_id, 'accepted')
        await query.edit_message_text(f"✅ *Берём!*\n📌 {job['title'][:80]}\n\n⏳ Выполняю...", parse_mode='Markdown')
        try:
            result = await execute_card_job(job)
            update_job(job_id, 'completed', result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка",     callback_data=f"redo_{job_id}")
            ]]
            msg = (f"✨ *КАРТОЧКИ ГОТОВЫ!*\n\n📌 *{job['title'][:80]}*\n\n"
                   f"━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n*Лила, проверь — отправляем?*")
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=f"❌ Ошибка: {str(e)[:200]}")

    elif data == "skip_wishes":
        user_sessions[user_id]['wishes'] = ''
        user_sessions[user_id]['step'] = 'waiting_product'
        await query.edit_message_text("⚡ Генерирую без пожеланий...")
        await generate_and_send_cards(query, context, user_id)

    elif data.startswith("skip_") and data != "skip_wishes":
        update_job(data[5:], 'skipped')
        await query.edit_message_text("⏭ Пропустили")

    elif data == "feedback_ok":
        await query.edit_message_text(
            "🎉 *Отлично! Карточки готовы к публикации!*\n\n"
            "Если нужна ещё одна — просто пришли новое фото.",
            parse_mode='Markdown'
        )
        user_sessions[user_id]['step'] = 'waiting_product'

    elif data == "feedback_edit":
        await query.edit_message_text(
            "✏️ *Что именно изменить?*\n\n"
            "Напиши пожелания:\n\n"
            "• _тёмный фон_\n"
            "• _другой стиль текста_\n"
            "• _добавь цену_\n"
            "• _более агрессивный дизайн_",
            parse_mode='Markdown'
        )
        user_sessions[user_id]['step'] = 'waiting_wishes'

    elif data.startswith("done_"):
        job_id = data[5:]
        job    = get_job(job_id)
        update_job(job_id, 'done')
        if job:
            nums = re.findall(r'\d+', job.get('budget','0').replace(' ',''))
            amount = float(nums[0]) if nums else 0
            is_rub = '₽' in job.get('budget','') or 'руб' in job.get('budget','').lower()
            save_earning(job_id, amount/90 if is_rub else amount, amount if is_rub else amount*90, job['title'])
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКРЫТ!*\n\n✅ Выполнено: {stats['by_status'].get('done',0)}\n"
            f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}",
            parse_mode='Markdown'
        )

    elif data.startswith("redo_"):
        job_id = data[5:]
        job    = get_job(job_id)
        context.user_data['redo_job_id'] = job_id
        context.user_data['redo_result'] = job.get('result','') if job else ''
        await query.edit_message_text(
            "✏️ *Напиши что исправить:*\n\nПример: _сократи_, _добавь ключевые слова_",
            parse_mode='Markdown'
        )

# ═══ КОМАНДЫ ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛍️ *КарточникБот*\n\n"
        "Генерирую карточки + инфографику для маркетплейсов!\n\n"
        "📸 *Пришли фото товара* — сделаю карточку как у топов\n"
        "📝 *Или напиши название* — сгенерирую сам\n\n"
        "🔍 Ищу заказы каждые 30 мин → фильтрует *Лила* → лучшее тебе!\n\n"
        "Выбери маркетплейс:",
        parse_mode='Markdown',
        reply_markup=_card_main_keyboard()
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("🔍 Ищу заказы на карточки...")
    count = await check_card_jobs(context.application.bot)
    await msg.edit_text(
        f"✅ Найдено и отправлено Лиле: *{count}* заказов\n\n"
        f"{'Лила анализирует — лучшие придут тебе! 🚀' if count>0 else 'Пока 0 — попробуй /clear и снова'}",
        parse_mode='Markdown'
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM seen_jobs')
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Кэш очищен! Теперь /scan найдёт заново.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    bs    = stats['by_status']
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА КАРТОЧНИКА*\n\n"
        f"🔍 Найдено: {bs.get('found',0)}\n"
        f"✅ Принято: {bs.get('accepted',0)}\n"
        f"✨ Выполнено: {bs.get('completed',0)}\n"
        f"💰 Закрыто: {bs.get('done',0)}\n"
        f"⏭ Пропущено: {bs.get('skipped',0)}\n\n"
        f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
        f"📦 Всего выплат: {stats['earn_count']}",
        parse_mode='Markdown'
    )

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *ПРАЙС — КАРТОЧКИ ТОВАРОВ*\n\n"
        "🟢 *Эконом* — 1 карточка: $5 / 50⭐ / 400₽\n"
        "🔵 *Стандарт* — 5 карточек: $20 / 200⭐\n"
        "🟣 *Бизнес* — 10 карточек: $35 / 350⭐\n"
        "🌍 *Amazon/Etsy* — от $8 за listing\n\n"
        "Способ оплаты:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 USDT",           callback_data="pay_usdt")],
            [InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars")],
            [InlineKeyboardButton("🇷🇺 Рубли",         callback_data="pay_rub")],
        ])
    )

# ═══ ОБРАБОТЧИК СООБЩЕНИЙ ═══

async def generate_and_send_cards(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Генерирует classic + premium карточки и спрашивает обратную связь"""
    session      = user_sessions.get(user_id, {})
    product      = session.get('last_product', 'товар')
    marketplace  = session.get('last_marketplace', 'wb')
    photo_bytes  = session.get('last_photo_bytes')
    image_base64 = session.get('last_image_base64')
    wishes       = session.get('wishes', '')

    await update.message.reply_text(
        f"🎨 *Генерирую карточки...*\n\n"
        f"{'💬 Учитываю: _' + wishes[:60] + '_' if wishes else '⚡ Без пожеланий'}\n\n"
        f"⏳ Обычно 60-90 секунд — делаю classic и premium",
        parse_mode='Markdown'
    )

    try:
        if AIDENTIKA_API_KEY and image_base64:
            # Загружаем фото
            upload_id = await aidentika_upload(image_base64)
            if not upload_id:
                raise Exception("Не удалось загрузить фото")

            # Генерируем текст
            text_result = await generate_card(product, marketplace, image_base64)
            card_data = list(text_result.values())[0] if marketplace == "all" else text_result
            base_features = "\n".join(card_data.get("преимущества", [])[:5]) if isinstance(card_data, dict) else product
            features_text = f"{base_features}\n{wishes}" if wishes else base_features

            # Запускаем CLASSIC и PREMIUM параллельно
            action_classic, action_premium = await asyncio.gather(
                aidentika_generate_card(upload_id, product_name=product[:100], features=features_text, style="classic"),
                aidentika_generate_card(upload_id, product_name=product[:100], features=features_text, style="premium")
            )

            async def empty_bytes(): return b""

            img_classic, img_premium = await asyncio.gather(
                aidentika_wait_and_download(action_classic) if action_classic else empty_bytes(),
                aidentika_wait_and_download(action_premium) if action_premium else empty_bytes()
            )

            sent_any = False
            if img_classic:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=io.BytesIO(img_classic),
                    caption=f"🎨 *Вариант 1 — Классический*\n\n_{product[:60]}_",
                    parse_mode='Markdown'
                )
                sent_any = True

            if img_premium:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=io.BytesIO(img_premium),
                    caption=f"✨ *Вариант 2 — Премиум*\n\n_{product[:60]}_",
                    parse_mode='Markdown'
                )
                sent_any = True

            if sent_any:
                await send_card_result(update.message, text_result, marketplace, product, context.bot, user_id, skip_image=True)

                # Проверяем баланс
                balance = await aidentika_balance()
                balance_msg = f"\n\n⚠️ Осталось {balance} искр — пополни баланс!" if 0 <= balance < 8 else ""

                # Спрашиваем обратную связь
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Всё отлично!", callback_data="feedback_ok"),
                    InlineKeyboardButton("✏️ Хочу изменить", callback_data="feedback_edit")
                ]])
                await update.message.reply_text(
                    f"👆 *Два варианта готовы!*\n\n"
                    f"Выбери который нравится и скажи — всё устраивает?{balance_msg}",
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
                user_sessions[user_id]['step'] = 'waiting_feedback'
                return

        # Fallback — только текст без Aidentika
        result = await generate_card(product, marketplace, image_base64)
        await send_card_result(update.message, result, marketplace, product, context.bot, user_id)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Всё отлично!", callback_data="feedback_ok"),
            InlineKeyboardButton("✏️ Хочу изменить", callback_data="feedback_edit")
        ]])
        await update.message.reply_text(
            "📝 *Карточка готова!*\n\nВсё устраивает?",
            parse_mode='Markdown',
            reply_markup=keyboard
        )
        user_sessions[user_id]['step'] = 'waiting_feedback'

    except Exception as e:
        logger.error(f"generate_and_send_cards ошибка: {e}")
        await update.message.reply_text(f"❌ Ошибка генерации: {str(e)[:100]}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Режим правки
    if context.user_data.get('redo_job_id'):
        job_id   = context.user_data['redo_job_id']
        original = context.user_data.get('redo_result', '')
        fix      = update.message.text
        await update.message.reply_text("⏳ Исправляю...")
        try:
            new_result = await redo_card_job(original, fix)
            update_job(job_id, 'completed', new_result)
            context.user_data['redo_result'] = new_result
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Ещё правка", callback_data=f"redo_{job_id}")
            ]]
            msg = f"✨ *ИСПРАВЛЕНО!*\n\n━━━━━━━━━━\n{new_result[:2500]}\n━━━━━━━━━━\n\n*Лила, проверь — отправляем?*"
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
            context.user_data.pop('redo_job_id', None)
            context.user_data.pop('redo_result', None)
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
        return

    # Не выбран маркетплейс — показываем стартовый экран
    if user_id not in user_sessions or user_sessions[user_id].get('step') not in ('waiting_product', 'waiting_wishes', 'waiting_feedback'):
        await update.message.reply_text(
            "🛍️ *КарточникБот*\n\n"
            "Генерирую карточки + инфографику для маркетплейсов!\n\n"
            "📸 *Пришли фото товара* — сделаю карточку как у топов\n"
            "📝 *Или напиши название* — сгенерирую сам\n\n"
            "🎨 Визуал через *Aidentika* — профессиональный результат\n\n"
            "👇 Выбери маркетплейс:",
            parse_mode='Markdown',
            reply_markup=_card_main_keyboard()
        )
        return

    step = user_sessions[user_id].get('step')

    # ─── Шаг 2: клиент ответил на пожелания ───
    if step == 'waiting_wishes':
        wishes = update.message.text or ""
        user_sessions[user_id]['wishes'] = wishes
        user_sessions[user_id]['step'] = 'waiting_product'
        # Запускаем генерацию с пожеланиями
        await generate_and_send_cards(update, context, user_id)
        return

    # ─── Шаг 3: клиент ответил на вопрос "всё устраивает?" ───
    if step == 'waiting_feedback':
        feedback = update.message.text.lower() if update.message.text else ""
        if any(w in feedback for w in ['нет', 'измени', 'правка', 'переделай', 'не нравится', 'плохо']):
            user_sessions[user_id]['step'] = 'waiting_product'
            await update.message.reply_text(
                "✏️ *Что именно изменить?*\n\n"
                "Напиши пожелания — и сгенерирую новые варианты:\n\n"
                "Например: _тёмный фон_, _больше текста_, _другой стиль_",
                parse_mode='Markdown'
            )
            user_sessions[user_id]['step'] = 'waiting_wishes'
        else:
            await update.message.reply_text(
                "🎉 *Отлично! Карточка готова к публикации!*\n\n"
                "Если нужна ещё одна — просто пришли новое фото или название товара.",
                parse_mode='Markdown',
                reply_markup=_card_main_keyboard()
            )
            user_sessions[user_id]['step'] = 'waiting_product'
        return

    marketplace  = user_sessions[user_id]['marketplace']
    image_base64 = None
    photo_bytes  = None
    product      = ""

    if update.message.photo:
        photo      = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await photo_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                photo_bytes  = f.read()
                image_base64 = base64.b64encode(photo_bytes).decode()
            os.unlink(tmp.name)
        product = update.message.caption or "товар на фото"

    elif update.message.text:
        product = update.message.text

    else:
        await update.message.reply_text("Отправь текст или фото товара!")
        return

    # Сохраняем данные сессии
    session_mp = marketplace if marketplace != "all" else "wb"
    user_sessions[user_id].update({
        'last_product': product,
        'last_marketplace': session_mp,
        'last_photo_bytes': photo_bytes,
        'last_image_base64': image_base64,
        'wishes': ''
    })

    # Спрашиваем пожелания перед генерацией
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Пропустить — генерировать сразу", callback_data="skip_wishes")
    ]])

    await update.message.reply_text(
        f"📦 *Товар:* _{product[:60]}_\n\n"
        f"💬 *Есть пожелания к карточке?*\n\n"
        f"Например:\n"
        f"• _тёмный фон, агрессивный стиль_\n"
        f"• _минимализм, белый фон, для женщин_\n"
        f"• _яркие цвета, молодёжная аудитория_\n"
        f"• _премиум, золотые акценты_\n\n"
        f"Или нажми кнопку чтобы генерировать сразу:",
        parse_mode='Markdown',
        reply_markup=keyboard
    )
    user_sessions[user_id]['step'] = 'waiting_wishes'



# ═══ АВТОСКАНИРОВАНИЕ ЧЕРЕЗ ASYNCIO (без JobQueue!) ═══

async def auto_scan_loop(bot):
    """Бесконечный цикл — каждые 30 минут. Без APScheduler."""
    await asyncio.sleep(90)
    while True:
        logger.info("🔄 Карточник: автосканирование...")
        try:
            count = await check_card_jobs(bot)
            if count > 0 and YOUR_CHAT_ID:
                await bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🛍️ *Карточник нашёл {count} заказов* — отправил Лиле на проверку!",
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"❌ Автосканирование: {e}")
        await asyncio.sleep(1800)

# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan",  scan_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))

    async def post_init(application):
        asyncio.create_task(auto_scan_loop(application.bot))
        logger.info("✅ Автосканирование запущено (asyncio)")
        try:
            if YOUR_CHAT_ID:
                await application.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        "🛍️ *Карточник запущен!*\n\n"
                        "⚡️ Работаю на Anthropic Claude Haiku\n"
                        "✅ Автосканирование каждые 30 мин\n"
                        "📨 Заказы идут через Лилу\n\n"
                        "/scan — найти сейчас\n"
                        "/price — прайс\n"
                        "/stats — статистика"
                    ),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"post_init: {e}")

    app.post_init = post_init

    logger.info("🛍️ Карточник запущен на Anthropic!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
