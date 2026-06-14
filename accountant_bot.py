import os
import json
import logging
import asyncio
import sqlite3
import httpx
import random
import pytz
import re
import nastya_brain  # мозг учёта (живая БД jobs+orders, потолок НПД)
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.getenv("ACCOUNTANT_BOT_TOKEN")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
YOUR_CHAT_ID      = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID      = int(os.getenv("LILU_CHAT_ID", "0"))
DB_PATH           = os.getenv("DB_PATH", "/opt/bots/data/freelance.db")
USDT_WALLET       = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
AIDENTIKA_API_KEY = os.getenv("AIDENTIKA_API_KEY", "")
USD_RATE          = 90.0

ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_HAIKU = "claude-haiku-4-5-20251001"

GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama-3.1-70b-versatile",
    "mixtral-8x7b-32768",
]
_groq_model_index = 0

def msk_now() -> datetime:
    return datetime.now(pytz.timezone('Europe/Moscow'))

def msk_time_str() -> str:
    return msk_now().strftime("%d.%m.%Y %H:%M МСК")

def msk_hour() -> int:
    return msk_now().hour

PLATFORMS = {
    "guru":      "🟠 Guru.com",
    "pph":       "🔵 PeoplePerHour",
    "fl":        "🇷🇺 FL.ru",
    "weblancer": "🇷🇺 Weblancer",
    "freelance": "🇷🇺 Freelance.ru",
    "paypal":    "💳 PayPal",
    "wallet":    "📱 TG Wallet (USDT)",
    "card":      "💳 Карта РФ",
    "other":     "💼 Другое",
    "kwork":     "🟢 Kwork",
}

# ═══ НИЖНЯЯ КЛАВИАТУРА ═══
KB_LABELS = {"🧠 Отчёт", "💰 Доход", "🏦 Кошельки", "📊 Система",
             "🔮 Прогноз", "🔋 Балансы", "📈 Стата", "📤 Лиле"}

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🧠 Отчёт", "💰 Доход"],
        ["🏦 Кошельки", "📊 Система"],
        ["🔮 Прогноз", "🔋 Балансы"],
        ["📈 Стата", "📤 Лиле"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

async def groq_request_smart(messages, max_tokens=300):
    global _groq_model_index
    for attempt in range(len(GROQ_MODELS)):
        model = GROQ_MODELS[_groq_model_index]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": max_tokens}
                )
                if r.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"⚠️ Rate limit [{model}] → переключаю, жду {wait:.1f}с")
                    _groq_model_index = (_groq_model_index + 1) % len(GROQ_MODELS)
                    await asyncio.sleep(wait)
                    continue
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            _groq_model_index = (_groq_model_index + 1) % len(GROQ_MODELS)
            await asyncio.sleep(2)
    return "⚠️ Все модели временно недоступны."

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT, amount_usd REAL, amount_rub REAL,
        source TEXT, description TEXT,
        status TEXT DEFAULT 'pending',
        date TEXT, paid_date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount_usd REAL, amount_rub REAL,
        category TEXT, description TEXT, date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wallets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT UNIQUE,
        balance_usd REAL DEFAULT 0,
        balance_rub REAL DEFAULT 0,
        total_earned_usd REAL DEFAULT 0,
        total_withdrawn_usd REAL DEFAULT 0,
        updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS wallet_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT, type TEXT,
        amount_usd REAL, amount_rub REAL,
        description TEXT, date TEXT
    )''')
    conn.commit()
    conn.close()

def get_wallets():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT platform, balance_usd, balance_rub, total_earned_usd, total_withdrawn_usd FROM wallets ORDER BY balance_usd DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def update_wallet(platform, amount_usd, amount_rub, tx_type, description):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO wallets (platform, balance_usd, balance_rub, total_earned_usd, total_withdrawn_usd, updated_at) VALUES (?, 0, 0, 0, 0, ?)',
              (platform, datetime.now().isoformat()))
    if tx_type == 'earn':
        c.execute('UPDATE wallets SET balance_usd=balance_usd+?, balance_rub=balance_rub+?, total_earned_usd=total_earned_usd+?, updated_at=? WHERE platform=?',
                  (amount_usd, amount_rub, amount_usd, datetime.now().isoformat(), platform))
    elif tx_type == 'withdraw':
        c.execute('UPDATE wallets SET balance_usd=MAX(0,balance_usd-?), balance_rub=MAX(0,balance_rub-?), total_withdrawn_usd=total_withdrawn_usd+?, updated_at=? WHERE platform=?',
                  (amount_usd, amount_rub, amount_usd, datetime.now().isoformat(), platform))
    c.execute('INSERT INTO wallet_transactions (platform, type, amount_usd, amount_rub, description, date) VALUES (?, ?, ?, ?, ?, ?)',
              (platform, tx_type, amount_usd, amount_rub, description, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_wallet_history(platform=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if platform:
        c.execute('SELECT platform, type, amount_usd, amount_rub, description, date FROM wallet_transactions WHERE platform=? ORDER BY date DESC LIMIT 10', (platform,))
    else:
        c.execute('SELECT platform, type, amount_usd, amount_rub, description, date FROM wallet_transactions ORDER BY date DESC LIMIT 10')
    rows = c.fetchall()
    conn.close()
    return rows

def add_earning(amount_usd, source, description, amount_rub=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if amount_rub == 0:
        amount_rub = amount_usd * USD_RATE
    c.execute('INSERT INTO earnings (amount_usd, amount_rub, source, description, status, date) VALUES (?, ?, ?, ?, "pending", ?)',
              (amount_usd, amount_rub, source, description, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    update_wallet(source.lower().split('.')[0], amount_usd, amount_rub, 'earn', description)

def add_expense(amount, category, description, is_rub=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if is_rub:
        amount_rub = amount
        amount_usd = amount / USD_RATE
    else:
        amount_usd = amount
        amount_rub = amount * USD_RATE
    c.execute('INSERT INTO expenses (amount_usd, amount_rub, category, description, date) VALUES (?, ?, ?, ?, ?)',
              (amount_usd, amount_rub, category, description, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_pending_earnings():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, description, amount_usd, source, date FROM earnings WHERE status="pending" ORDER BY date DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def mark_paid(earning_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE earnings SET status="paid", paid_date=? WHERE id=?',
              (datetime.now().isoformat(), earning_id))
    conn.commit()
    conn.close()

def get_stats(period="month"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if period == "today":
        date_from = datetime.now().strftime("%Y-%m-%d")
    elif period == "week":
        date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "month":
        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    else:
        date_from = "2000-01-01"
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0), COUNT(*) FROM earnings WHERE date >= ?', (date_from,))
    earn = c.fetchone()
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0) FROM expenses WHERE date >= ?', (date_from,))
    exp = c.fetchone()
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COUNT(*) FROM earnings WHERE status="paid"')
    total_paid = c.fetchone()
    conn.close()
    return {
        'earn_usd': earn[0], 'earn_rub': earn[1], 'earn_count': earn[2],
        'exp_usd': exp[0], 'exp_rub': exp[1],
        'profit_usd': earn[0] - exp[0], 'profit_rub': earn[1] - exp[1],
        'total_paid_usd': total_paid[0], 'total_paid_count': total_paid[1]
    }

def get_system_analytics(period_days: int = 7) -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        date_from = (datetime.now() - timedelta(days=period_days)).isoformat()
        c.execute('SELECT COUNT(*) FROM jobs WHERE source LIKE ? AND created_at >= ?', ('%Полифан%', date_from))
        poly_found = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM jobs WHERE source LIKE ? AND status IN ("accepted","done","completed") AND created_at >= ?', ('%Полифан%', date_from))
        poly_taken = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM jobs WHERE source LIKE ? AND status="done" AND created_at >= ?', ('%Полифан%', date_from))
        poly_done = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM jobs WHERE source LIKE ? AND created_at >= ?', ('%Карточник%', date_from))
        card_found = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM jobs WHERE source LIKE ? AND status IN ("accepted","done","completed") AND created_at >= ?', ('%Карточник%', date_from))
        card_taken = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM jobs WHERE source LIKE ? AND status="done" AND created_at >= ?', ('%Карточник%', date_from))
        card_done = c.fetchone()[0]
        try:
            c.execute('SELECT COUNT(*) FROM filtered_jobs WHERE date >= ?', (date_from,))
            filtered_ai = c.fetchone()[0]
        except:
            filtered_ai = 0
        c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0), COUNT(*) FROM earnings WHERE date >= ?', (date_from,))
        earn = c.fetchone()
        c.execute('SELECT COALESCE(SUM(amount_usd),0) FROM expenses WHERE date >= ?', (date_from,))
        exp = c.fetchone()
        prev_from = (datetime.now() - timedelta(days=period_days*2)).isoformat()
        c.execute('SELECT COALESCE(SUM(amount_usd),0) FROM earnings WHERE date >= ? AND date < ?', (prev_from, date_from))
        prev_earn = c.fetchone()[0]
        conn.close()
        poly_conv  = round(poly_taken / poly_found * 100) if poly_found > 0 else 0
        card_conv  = round(card_taken / card_found * 100) if card_found > 0 else 0
        earn_delta = earn[0] - prev_earn
        earn_pct   = round(earn_delta / prev_earn * 100) if prev_earn > 0 else 0
        return {
            'poly_found': poly_found, 'poly_taken': poly_taken,
            'poly_done': poly_done, 'poly_conv': poly_conv,
            'card_found': card_found, 'card_taken': card_taken,
            'card_done': card_done, 'card_conv': card_conv,
            'filtered_ai': filtered_ai,
            'earn_usd': earn[0], 'earn_rub': earn[1], 'earn_count': earn[2],
            'exp_usd': exp[0],
            'profit_usd': earn[0] - exp[0],
            'earn_delta': earn_delta, 'earn_pct': earn_pct,
            'period_days': period_days,
        }
    except Exception as e:
        logger.error(f"get_system_analytics: {e}")
        return {}

def format_system_analytics(data: dict) -> str:
    if not data:
        return "❌ Нет данных"
    poly_status = "🟢" if data['poly_conv'] >= 10 else ("🟡" if data['poly_conv'] >= 5 else "🔴")
    card_status = "🟢" if data['card_conv'] >= 15 else ("🟡" if data['card_conv'] >= 8 else "🔴")
    delta_emoji = "📈" if data['earn_delta'] >= 0 else "📉"
    delta_sign  = "+" if data['earn_delta'] >= 0 else ""
    msg = (
        f"📊 *АНАЛИТИКА СИСТЕМЫ — {data['period_days']} дней*\n"
        f"_{msk_time_str()}_\n\n"
        f"🤖 *ПОЛИФАН*\n"
        f"├ Найдено: {data['poly_found']} заказов\n"
        f"├ Взято: {data['poly_taken']} | Закрыто: {data['poly_done']}\n"
        f"├ Конверсия: {poly_status} {data['poly_conv']}%\n"
        f"└ Отфильтровано AI: {data['filtered_ai']}\n\n"
        f"🛍️ *КАРТОЧНИК*\n"
        f"├ Найдено: {data['card_found']} заказов\n"
        f"├ Взято: {data['card_taken']} | Закрыто: {data['card_done']}\n"
        f"└ Конверсия: {card_status} {data['card_conv']}%\n\n"
        f"💰 *ФИНАНСЫ*\n"
        f"├ Доход: ${data['earn_usd']:.2f} / ₽{data['earn_rub']:.0f}\n"
        f"├ Расходы: ${data['exp_usd']:.2f}\n"
        f"├ Прибыль: ${data['profit_usd']:.2f}\n"
        f"└ {delta_emoji} К прошлому периоду: {delta_sign}{data['earn_pct']}%\n"
    )
    bottlenecks = []
    if data['poly_conv'] < 5:
        bottlenecks.append("⚠️ Полифан — низкая конверсия")
    if data['poly_found'] == 0:
        bottlenecks.append("🔴 Полифан не нашёл заказов!")
    if data['card_found'] == 0:
        bottlenecks.append("🔴 Карточник не нашёл заказов!")
    if data['profit_usd'] < 0:
        bottlenecks.append("🔴 Расходы превышают доходы!")
    if bottlenecks:
        msg += "\n🚨 *УЗКИЕ МЕСТА:*\n" + "\n".join(bottlenecks)
    return msg

async def get_usd_rate():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.exchangerate-api.com/v4/latest/USD")
            return r.json()["rates"].get("RUB", 90.0)
    except:
        return 90.0

async def get_aidentika_balance() -> dict:
    if not AIDENTIKA_API_KEY:
        return {"available": -1, "status": "no_key"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.aidentika.com/api/v1/public/balance",
                headers={"Authorization": f"Bearer {AIDENTIKA_API_KEY}"}
            )
            if r.status_code == 200:
                data = r.json()
                return {"available": data.get("available", 0), "total": data.get("total", 0), "status": "ok"}
    except Exception as e:
        logger.error(f"Aidentika balance error: {e}")
    return {"available": -1, "status": "error"}

async def check_all_balances() -> str:
    results = await asyncio.gather(get_aidentika_balance(), get_usd_rate(), return_exceptions=True)
    aidentika = results[0] if not isinstance(results[0], Exception) else {"available": -1, "status": "error"}
    usd_rate  = results[1] if not isinstance(results[1], Exception) else 90.0
    msg = f"🔋 *БАЛАНСЫ СЕРВИСОВ*\n_{msk_time_str()}_\n\n"
    if aidentika["status"] == "ok":
        sparks = aidentika["available"]
        cards_left = sparks // 8
        emoji = "🟢" if sparks >= 40 else ("🟡" if sparks >= 16 else "🔴")
        msg += f"{emoji} *Aidentika:* {sparks} искр (~{cards_left} карточек)\n"
        if sparks < 16:
            msg += f"   ⚠️ *Пополни! Мало осталось*\n"
    elif aidentika["status"] == "no_key":
        msg += "⚫ *Aidentika:* ключ не настроен\n"
    else:
        msg += "🔴 *Aidentika:* ошибка проверки\n"
    msg += "\n🟢 *Groq API:* ротация 4 моделей\n"
    msg += "   └ Rate limit защита ✅\n"
    if ANTHROPIC_API_KEY:
        msg += "\n🟢 *Anthropic API:* ключ настроен\n"
    else:
        msg += "\n⚫ *Anthropic API:* ключ не настроен\n"
    msg += "\n🟢 *VPS:* 132.243.228.167 (Франкфурт)\n"
    msg += "   └ Ubuntu 24.04, 2GB/2CPU\n"
    msg += f"\n💱 *Курс USD:* ₽{usd_rate:.0f}\n"
    warnings = []
    if aidentika.get("available", 0) < 16 and aidentika["status"] == "ok":
        warnings.append("⚠️ Пополни Aidentika — мало искр!")
    if warnings:
        msg += "\n━━━━━━━━━━\n🚨 *ТРЕБУЕТ ВНИМАНИЯ:*\n" + "\n".join(warnings)
    return msg

async def parse_expense_natural(text: str) -> dict | None:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        prompt = f"""Из текста извлеки данные о расходе.
Текст: "{text}"

Ответь ТОЛЬКО JSON без лишнего:
{{"amount": число, "currency": "rub" или "usd", "description": "краткое описание", "category": "категория"}}

Категории: vps, api, software, marketing, other
Примеры:
"потратил 390р на айдентику" -> {{"amount": 390, "currency": "rub", "description": "Aidentika подписка", "category": "api"}}
"заплатил 5 долларов за домен" -> {{"amount": 5, "currency": "usd", "description": "домен", "category": "other"}}

Если это НЕ расход — верни null"""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": ANTHROPIC_HAIKU, "max_tokens": 100,
                      "messages": [{"role": "user", "content": prompt}]}
            )
            text_resp = r.json()["content"][0]["text"].strip()
            if text_resp.lower() == "null":
                return None
            return json.loads(text_resp)
    except Exception as e:
        logger.error(f"parse_expense_natural error: {e}")
        return None

async def alerts_check(bot, chat_id: int):
    try:
        data = get_system_analytics(1)
        alerts = []
        if data.get('poly_found', 0) == 0:
            alerts.append("🔴 Полифан не нашёл ни одного заказа за 24 часа!")
        if data.get('card_found', 0) == 0:
            alerts.append("🔴 Карточник не нашёл ни одного заказа за 24 часа!")
        if data.get('earn_usd', 0) == 0 and msk_hour() >= 18:
            alerts.append("⚠️ Нет дохода сегодня — стоит проверить систему")
        aidentika = await get_aidentika_balance()
        if aidentika.get('status') == 'ok' and aidentika.get('available', 99) < 8:
            alerts.append(f"🔴 Aidentika: осталось {aidentika['available']} искр! Пополни!")
        if alerts:
            msg = f"🚨 *АЛЕРТЫ АНАСТАСИИ*\n_{msk_time_str()}_\n\n" + "\n".join(alerts)
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            logger.info(f"⚠️ Анастасия отправила {len(alerts)} алертов")
    except Exception as e:
        logger.error(f"alerts_check: {e}")

async def generate_report_ai(stats, wallets, data_system=None):
    wallet_text = "\n".join([f"{PLATFORMS.get(p, p)}: ${b:.2f}" for p, b, _, _, _ in wallets]) if wallets else "Кошельки пусты"
    system_info = ""
    if data_system:
        system_info = (
            f"\nАналитика системы:\n"
            f"Полифан: {data_system.get('poly_found',0)} заказов, конверсия {data_system.get('poly_conv',0)}%\n"
            f"Карточник: {data_system.get('card_found',0)} заказов, конверсия {data_system.get('card_conv',0)}%\n"
        )
    prompt = (
        f"Составь краткий финансовый отчёт для Лилы (генерального директора).\n\n"
        f"ДАННЫЕ:\n"
        f"Доходы за месяц: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
        f"Расходы: ${stats['exp_usd']:.2f}\n"
        f"Прибыль: ${stats['profit_usd']:.2f}\n"
        f"Заказов: {stats['earn_count']}\n"
        f"Остатки по биржам:\n{wallet_text}\n"
        f"{system_info}\n"
        f"Напиши отчёт 3-4 предложения. Деловой тон. Что хорошо и что улучшить."
    )
    return await groq_request_smart(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300
    )

async def send_report_to_lilu(bot, stats):
    if not LILU_CHAT_ID:
        return
    wallets      = get_wallets()
    data_system  = get_system_analytics(7)
    ai_summary   = await generate_report_ai(stats, wallets, data_system)
    wallet_lines = ""
    total_balance = 0
    for platform, bal_usd, bal_rub, earned, withdrawn in wallets:
        name = PLATFORMS.get(platform, platform)
        wallet_lines += f"├ {name}: ${bal_usd:.2f} / ₽{bal_rub:.0f}\n"
        total_balance += bal_usd
    system_block = ""
    if data_system:
        system_block = (
            f"\n━━━━━━━━━━━━━━━━\n"
            f"🤖 *АНАЛИТИКА СИСТЕМЫ (7 дней):*\n"
            f"├ Полифан: {data_system['poly_found']} найдено, конверсия {data_system['poly_conv']}%\n"
            f"└ Карточник: {data_system['card_found']} найдено, конверсия {data_system['card_conv']}%\n"
        )
    msg = (
        f"📊 *ФИНАНСОВЫЙ ОТЧЁТ — АНАСТАСИЯ*\n"
        f"_{msk_time_str()}_\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 *ДОХОДЫ (месяц)*\n"
        f"├ USD: ${stats['earn_usd']:.2f}\n"
        f"├ RUB: ₽{stats['earn_rub']:.0f}\n"
        f"└ Заказов: {stats['earn_count']}\n\n"
        f"💸 *РАСХОДЫ:* ${stats['exp_usd']:.2f}\n"
        f"📈 *ПРИБЫЛЬ:* ${stats['profit_usd']:.2f} / ₽{stats['profit_rub']:.0f}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏦 *ОСТАТКИ ПО БИРЖАМ:*\n"
        f"{wallet_lines or '  Пусто\n'}"
        f"💎 *ИТОГО:* ${total_balance:.2f}\n\n"
        f"💳 *USDT:* `{USDT_WALLET[:25]}...`\n"
        f"{system_block}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🤖 *Анализ Анастасии:*\n"
        f"{ai_summary}"
    )
    await bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown')

async def weekly_system_report(bot, chat_id: int):
    data    = get_system_analytics(7)
    stats   = get_stats("week")
    wallets = get_wallets()
    system_msg  = format_system_analytics(data)
    ai_summary  = await generate_report_ai(stats, wallets, data)
    wallet_lines = "\n".join([
        f"  {PLATFORMS.get(p, p)}: ${b:.2f}"
        for p, b, _, _, _ in wallets if b > 0
    ]) or "  Кошельки пусты"
    full_msg = (
        f"📊 *ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ*\n"
        f"_{msk_time_str()}_\n\n"
        f"{system_msg}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏦 *На биржах:*\n{wallet_lines}\n\n"
        f"🤖 *Анализ:*\n_{ai_summary}_"
    )
    await bot.send_message(chat_id=chat_id, text=full_msg, parse_mode='Markdown')
    if LILU_CHAT_ID and LILU_CHAT_ID != chat_id:
        await bot.send_message(chat_id=LILU_CHAT_ID, text=full_msg, parse_mode='Markdown')

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💰 *Анастасия v2.1 — Финансовый аналитик*\n\n"
        f"🕐 {msk_time_str()}\n\n"
        f"Слежу за деньгами и аналитикой системы!\n"
        f"⚡ Groq: ротация 4 моделей — работаю без остановок\n\n"
        f"/stats — статистика за месяц\n"
        f"/system — аналитика всей системы\n"
        f"/forecast — прогноз дохода\n"
        f"/wallets — остатки по биржам\n"
        f"/balances — балансы всех сервисов\n"
        f"/withdraw — записать вывод денег\n"
        f"/add — добавить доход\n"
        f"/expense — добавить расход\n"
        f"/pending — ожидают оплаты\n"
        f"/report — отчёт Лиле\n"
        f"/goals — финансовые цели\n"
        f"/history — история транзакций\n"
        f"/brain — точный отчёт по живой базе\n"
        f"/income — записать реальный доход (потолок НПД)\n\n"
        f"💬 *Или напиши обычным текстом:*\n"
        f"_«потратил 390р на айдентику»_",
        parse_mode='Markdown',
        reply_markup=MAIN_KB
    )

async def system_analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days = 7
    if args:
        try:
            days = int(args[0])
        except:
            pass
    await update.message.reply_text(f"📊 Собираю аналитику за {days} дней...")
    data = get_system_analytics(days)
    msg  = format_system_analytics(data)
    try:
        summary = await groq_request_smart(
            messages=[{"role": "user", "content":
                f"Ты Анастасия — финансовый аналитик. Дай краткий анализ за {days} дней:\n"
                f"Полифан: {data.get('poly_found',0)} заказов, конверсия {data.get('poly_conv',0)}%\n"
                f"Карточник: {data.get('card_found',0)} заказов, конверсия {data.get('card_conv',0)}%\n"
                f"Доход: ${data.get('earn_usd',0):.2f}, прибыль: ${data.get('profit_usd',0):.2f}\n"
                f"3 предложения: что хорошо, где проблема, рекомендация."}],
            max_tokens=200
        )
        msg += f"\n\n🤖 *Анализ:*\n_{summary}_"
    except:
        pass
    keyboard = [[
        InlineKeyboardButton("📅 7 дней",  callback_data="analytics_7"),
        InlineKeyboardButton("📅 30 дней", callback_data="analytics_30"),
        InlineKeyboardButton("📊 Лиле",    callback_data="analytics_lilu"),
    ]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats      = get_stats("month")
    data_7     = get_system_analytics(7)
    days_passed = msk_now().day
    daily_rate  = stats['earn_usd'] / days_passed if days_passed > 0 else 0
    forecast    = daily_rate * 30
    weekly_proj = data_7.get('earn_usd', 0) * 4
    remaining   = max(0, 100 - stats['earn_usd'])
    msg = (
        f"🔮 *ПРОГНОЗ ДОХОДА*\n_{msk_time_str()}_\n\n"
        f"📅 *Текущий месяц:*\n"
        f"├ Прошло дней: {days_passed}\n"
        f"├ Заработано: ${stats['earn_usd']:.2f}\n"
        f"├ Темп: ${daily_rate:.2f}/день\n"
        f"└ Прогноз на месяц: *${forecast:.2f}*\n\n"
        f"📈 *На основе последней недели:*\n"
        f"└ Проекция x4: *${weekly_proj:.2f}*\n\n"
        f"🎯 *До цели $100/мес:* "
    )
    if remaining == 0:
        msg += "✅ *ДОСТИГНУТА!*"
    else:
        days_left = round(remaining / daily_rate) if daily_rate > 0 else 999
        msg += f"осталось ${remaining:.2f} (~{days_left} дней)"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def balances_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Проверяю балансы...")
    msg = await check_all_balances()
    keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_balances")]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global USD_RATE
    USD_RATE = await get_usd_rate()
    wallets = get_wallets()
    if not wallets:
        await update.message.reply_text("🏦 Кошельки пусты — добавь первый доход через /add")
        return
    total_usd = sum(w[1] for w in wallets)
    total_rub = sum(w[2] for w in wallets)
    msg = f"🏦 *ОСТАТКИ ПО БИРЖАМ*\n_{msk_time_str()}_\n_Курс: 1 USD = ₽{USD_RATE:.0f}_\n\n"
    for platform, bal_usd, bal_rub, earned, withdrawn in wallets:
        name    = PLATFORMS.get(platform, platform)
        bar_pct = min(10, int(bal_usd / max(total_usd, 1) * 10))
        bar     = "█" * bar_pct + "░" * (10 - bar_pct)
        msg += f"{name}\n  [{bar}] ${bal_usd:.2f} / ₽{bal_rub:.0f}\n"
        msg += f"  Заработано: ${earned:.2f} | Выведено: ${withdrawn:.2f}\n\n"
    msg += f"━━━━━━━━━━\n💎 *ИТОГО:* ${total_usd:.2f} / ₽{total_rub:.0f}"
    keyboard = [[
        InlineKeyboardButton("💸 Вывести",    callback_data="withdraw_start"),
        InlineKeyboardButton("📊 Отчёт Лиле", callback_data="send_lilu"),
        InlineKeyboardButton("🔋 Сервисы",    callback_data="refresh_balances")
    ]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = get_wallets()
    if not wallets:
        await update.message.reply_text("Нет активных кошельков. Сначала /add")
        return
    keyboard = []
    for platform, bal_usd, bal_rub, _, _ in wallets:
        if bal_usd > 0:
            name = PLATFORMS.get(platform, platform)
            keyboard.append([InlineKeyboardButton(f"{name} — ${bal_usd:.2f}", callback_data=f"withdraw_{platform}")])
    if not keyboard:
        await update.message.reply_text("Все кошельки пусты 🤷")
        return
    await update.message.reply_text("💸 *Вывод денег*\n\nС какой биржи?", parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = get_wallet_history()
    if not history:
        await update.message.reply_text("История пуста")
        return
    msg = f"📋 *ИСТОРИЯ ТРАНЗАКЦИЙ*\n_{msk_time_str()}_\n\n"
    for platform, tx_type, usd, rub, desc, date in history:
        name  = PLATFORMS.get(platform, platform)
        emoji = "➕" if tx_type == 'earn' else "➖"
        d     = date[:10] if date else "?"
        msg  += f"{emoji} {d} | {name}\n   ${usd:.2f} — {desc[:40]}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "month")

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "today")

async def show_stats(update, period):
    global USD_RATE
    USD_RATE = await get_usd_rate()
    stats = get_stats(period)
    names = {"today": "СЕГОДНЯ", "week": "НЕДЕЛЮ", "month": "МЕСЯЦ", "all": "ВСЁ ВРЕМЯ"}
    emoji = "📈" if stats['profit_usd'] >= 0 else "📉"
    msg = (
        f"💰 *СТАТИСТИКА ЗА {names.get(period, 'МЕСЯЦ')}*\n"
        f"_{msk_time_str()}_\n"
        f"_Курс: 1 USD = ₽{USD_RATE:.0f}_\n\n"
        f"✅ Доходы: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
        f"❌ Расходы: ${stats['exp_usd']:.2f} / ₽{stats['exp_rub']:.0f}\n"
        f"{emoji} Прибыль: ${stats['profit_usd']:.2f} / ₽{stats['profit_rub']:.0f}\n"
        f"📦 Заказов: {stats['earn_count']}\n"
        f"🏆 Всего выплачено: ${stats['total_paid_usd']:.2f}"
    )
    keyboard = [[
        InlineKeyboardButton("🏦 Кошельки",   callback_data="show_wallets"),
        InlineKeyboardButton("📊 Система",     callback_data="analytics_7"),
        InlineKeyboardButton("📊 Лиле",        callback_data="send_lilu")
    ]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = get_pending_earnings()
    if not pending:
        await update.message.reply_text("✅ Нет ожидающих оплат!")
        return
    msg = "⏳ *ОЖИДАЮТ ОПЛАТЫ:*\n\n"
    keyboard = []
    for eid, desc, amt, source, date in pending:
        d    = date[:10] if date else "?"
        msg += f"• {d} — {desc[:40]} — ${amt:.2f} ({source})\n"
        keyboard.append([InlineKeyboardButton(f"✅ Оплачен: ${amt:.2f}", callback_data=f"paid_{eid}")])
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats("month")
    await update.message.reply_text("📊 Готовлю отчёт для Лилы...")
    await send_report_to_lilu(context.application.bot, stats)
    await update.message.reply_text("✅ Отчёт отправлен Лиле!")

async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats   = get_stats("month")
    targets = [("🥉 Первый доллар", 1), ("🥈 $100/месяц", 100),
               ("🥇 $500/месяц", 500), ("💎 $1000/месяц", 1000)]
    msg = f"🎯 *ФИНАНСОВЫЕ ЦЕЛИ*\n_{msk_time_str()}_\n\n"
    for name, target in targets:
        earned = stats['earn_usd']
        pct    = min(100, int(earned / target * 100))
        bar    = "█" * (pct // 10) + "░" * (10 - pct // 10)
        status = "✅" if earned >= target else "⏳"
        msg   += f"{status} *{name}*\n[{bar}] {pct}%\n${earned:.2f} / ${target}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "send_lilu":
        stats = get_stats("month")
        await send_report_to_lilu(context.application.bot, stats)
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(chat_id=YOUR_CHAT_ID, text="✅ Отчёт отправлен Лиле!")
    elif data == "show_wallets":
        wallets = get_wallets()
        if not wallets:
            await query.answer("Кошельки пусты")
            return
        msg = "🏦 *ОСТАТКИ:*\n\n"
        for platform, bal_usd, bal_rub, earned, withdrawn in wallets:
            name  = PLATFORMS.get(platform, platform)
            msg  += f"{name}: ${bal_usd:.2f} / ₽{bal_rub:.0f}\n"
        await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown')
    elif data == "refresh_balances":
        await query.edit_message_text("🔄 Проверяю балансы...")
        msg = await check_all_balances()
        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_balances")]]
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "analytics_7":
        d   = get_system_analytics(7)
        msg = format_system_analytics(d)
        keyboard = [[
            InlineKeyboardButton("📅 30 дней", callback_data="analytics_30"),
            InlineKeyboardButton("📊 Лиле",    callback_data="analytics_lilu"),
        ]]
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "analytics_30":
        d   = get_system_analytics(30)
        msg = format_system_analytics(d)
        keyboard = [[
            InlineKeyboardButton("📅 7 дней", callback_data="analytics_7"),
            InlineKeyboardButton("📊 Лиле",   callback_data="analytics_lilu"),
        ]]
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "analytics_lilu":
        stats = get_stats("week")
        await send_report_to_lilu(context.application.bot, stats)
        await query.answer("Отчёт отправлен Лиле!")
    elif data == "withdraw_start":
        wallets  = get_wallets()
        keyboard = []
        for platform, bal_usd, _, _, _ in wallets:
            if bal_usd > 0:
                name = PLATFORMS.get(platform, platform)
                keyboard.append([InlineKeyboardButton(f"{name} — ${bal_usd:.2f}", callback_data=f"withdraw_{platform}")])
        await query.edit_message_text("💸 С какой биржи?", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("withdraw_"):
        platform = data[9:]
        name     = PLATFORMS.get(platform, platform)
        context.user_data['withdraw_platform'] = platform
        await query.edit_message_text(
            f"💸 *Вывод с {name}*\n\nНапиши сумму:\n"
            f"`/withdraw_amount 50` — в USD\n"
            f"`/withdraw_amount 4500rub` — в рублях",
            parse_mode='Markdown'
        )
    elif data.startswith("paid_"):
        mark_paid(int(data[5:]))
        await query.edit_message_text("✅ Отмечено как оплаченное!")

async def handle_kb_button(update: Update, context: ContextTypes.DEFAULT_TYPE, label: str):
    if label == "🧠 Отчёт":
        try:
            await update.message.reply_text(
                nastya_brain.build_report(24), parse_mode='Markdown', reply_markup=MAIN_KB
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Мозг споткнулся: {e}", reply_markup=MAIN_KB)
    elif label == "💰 Доход":
        context.user_data['awaiting_income'] = True
        await update.message.reply_text(
            "💰 Сколько пришло? Напиши *сумма описание*\n"
            "Например: `15000 ниши WB`\n\n"
            "_Любая другая кнопка — отмена._",
            parse_mode='Markdown', reply_markup=MAIN_KB
        )
    elif label == "🏦 Кошельки":
        await wallets_command(update, context)
    elif label == "📊 Система":
        await system_analytics_command(update, context)
    elif label == "🔮 Прогноз":
        await forecast_command(update, context)
    elif label == "🔋 Балансы":
        await balances_command(update, context)
    elif label == "📈 Стата":
        await stats_command(update, context)
    elif label == "📤 Лиле":
        await update.message.reply_text("📊 Готовлю отчёт для Лилы...", reply_markup=MAIN_KB)
        stats = get_stats("month")
        await send_report_to_lilu(context.application.bot, stats)
        await update.message.reply_text("✅ Отчёт отправлен Лиле!", reply_markup=MAIN_KB)


async def _record_income_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    m = re.match(r"\s*(\d[\d \u00a0]*)\s*(.*)", raw)
    if not m:
        await update.message.reply_text("❌ Нужна сумма. Например: `15000 ниши WB`", parse_mode='Markdown', reply_markup=MAIN_KB)
        return
    amount = int(re.sub(r"[^\d]", "", m.group(1)))
    desc = m.group(2).strip() or "без описания"
    if not nastya_brain.record_income(amount, desc):
        await update.message.reply_text("❌ Не смогла записать доход.", reply_markup=MAIN_KB)
        return
    mny = nastya_brain.money(24)
    amt = f"{amount:,}".replace(",", " ")
    turn = f"{mny['turnover_12m']:,}".replace(",", " ")
    t = (f"✅ *Доход зафиксирован:* {amt}₽\n_{desc}_\n\n"
         f"🏛 Оборот 12 мес: {turn}₽ = {mny['ceiling_pct']}%")
    if mny["ceiling_alert"]:
        t += "\n" + mny["ceiling_alert"]
    await update.message.reply_text(t, parse_mode='Markdown', reply_markup=MAIN_KB)
    if LILU_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=LILU_CHAT_ID,
                text=f"💰 *Настя:* доход {amt}₽ — {desc}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"income kb -> Лиле: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # ждём сумму дохода (после кнопки «💰 Доход»)?
    if context.user_data.get('awaiting_income'):
        if text in KB_LABELS:
            context.user_data.pop('awaiting_income', None)
            await handle_kb_button(update, context, text)
            return
        context.user_data.pop('awaiting_income', None)
        if text and re.search(r"\d", text):
            await _record_income_from_text(update, context, text)
        else:
            await update.message.reply_text("Отменила запись дохода.", reply_markup=MAIN_KB)
        return

    # нажата кнопка нижней клавиатуры?
    if text in KB_LABELS:
        await handle_kb_button(update, context, text)
        return

    if text.startswith('/add '):
        parts = text[5:].split(' ', 2)
        if len(parts) >= 2:
            try:
                amount_str = parts[0].lower()
                is_rub     = 'rub' in amount_str or 'р' in amount_str
                amount     = float(amount_str.replace('rub','').replace('р',''))
                source     = parts[1]
                desc       = parts[2] if len(parts) > 2 else "Без описания"
                if is_rub:
                    add_earning(amount / USD_RATE, source, desc, amount)
                    await update.message.reply_text(f"✅ Доход: ₽{amount:.0f} от {PLATFORMS.get(source, source)}")
                else:
                    add_earning(amount, source, desc)
                    await update.message.reply_text(f"✅ Доход: ${amount:.2f} от {PLATFORMS.get(source, source)}")
            except:
                await update.message.reply_text("❌ Формат: `/add 25 guru Описание`", parse_mode='Markdown')

    elif text.startswith('/expense '):
        parts = text[9:].split(' ', 1)
        try:
            amount_str = parts[0].lower()
            is_rub     = 'rub' in amount_str or 'р' in amount_str or int(float(amount_str.replace('rub','').replace('р',''))) > 100
            amount     = float(amount_str.replace('rub','').replace('р',''))
            desc       = parts[1] if len(parts) > 1 else "Без описания"
            add_expense(amount, "Расход", desc, is_rub)
            sym = "₽" if is_rub else "$"
            await update.message.reply_text(f"✅ Расход: {sym}{amount:.0f} — {desc}")
        except:
            await update.message.reply_text("❌ Формат: `/expense 390 Aidentika`", parse_mode='Markdown')

    elif text.startswith('/withdraw_amount '):
        platform = context.user_data.get('withdraw_platform')
        if not platform:
            await update.message.reply_text("Сначала выбери биржу через /withdraw")
            return
        try:
            amount_str = text[17:].strip().lower()
            is_rub     = 'rub' in amount_str
            amount     = float(amount_str.replace('rub', ''))
            name       = PLATFORMS.get(platform, platform)
            if is_rub:
                update_wallet(platform, amount / USD_RATE, amount, 'withdraw', "Вывод на карту")
                await update.message.reply_text(f"💸 Вывод: ₽{amount:.0f} с {name}")
            else:
                update_wallet(platform, amount, amount * USD_RATE, 'withdraw', f"Вывод ${amount:.2f}")
                await update.message.reply_text(f"💸 Вывод: ${amount:.2f} с {name}")
            if LILU_CHAT_ID:
                await context.bot.send_message(
                    chat_id=LILU_CHAT_ID,
                    text=f"💸 *Анастасия:* Артём вывел {'₽'+str(int(amount)) if is_rub else '$'+str(amount)} с {name}",
                    parse_mode='Markdown'
                )
            context.user_data.pop('withdraw_platform', None)
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    else:
        expense_data = await parse_expense_natural(text)
        if expense_data:
            amount   = expense_data["amount"]
            is_rub   = expense_data["currency"] == "rub"
            desc     = expense_data["description"]
            category = expense_data.get("category", "other")
            add_expense(amount, category, desc, is_rub)
            sym = "₽" if is_rub else "$"
            await update.message.reply_text(
                f"✅ *Расход записан автоматически!*\n\n"
                f"💸 {sym}{amount:.0f} — {desc}\n"
                f"📁 Категория: {category}\n\n"
                f"_Ошиблась? Используй /expense_",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"Привет! Я Анастасия 💰\n"
                f"_{msk_time_str()}_\n\n"
                f"Команды: /stats /system /forecast /wallets /balances\n\n"
                f"Или напиши: _«потратил 390р на айдентику»_",
                parse_mode='Markdown'
            )

async def daily_report(app):
    while True:
        now     = msk_now()
        next_9  = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= next_9:
            next_9 = next_9 + timedelta(days=1)
        await asyncio.sleep((next_9 - now).total_seconds())
        try:
            global USD_RATE
            USD_RATE = await get_usd_rate()
            stats    = get_stats("today")
            wallets  = get_wallets()
            total_bal = sum(w[1] for w in wallets)

            aidentika   = await get_aidentika_balance()
            balance_warn = ""
            if aidentika["status"] == "ok" and aidentika["available"] < 16:
                balance_warn = f"\n\n⚠️ *Aidentika:* осталось {aidentika['available']} искр — пополни!"

            wallet_lines = "\n".join([
                f"  {PLATFORMS.get(p, p)}: ${b:.2f}"
                for p, b, _, _, _ in wallets if b > 0
            ])
            msg = (
                f"🌅 *Доброе утро! Итоги вчера:*\n"
                f"_{msk_time_str()}_\n\n"
                f"💰 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
                f"📦 Заказов: {stats['earn_count']}\n"
                f"💸 Расходы: ${stats['exp_usd']:.2f}\n"
                f"📈 Прибыль: ${stats['profit_usd']:.2f}\n\n"
                f"🏦 На биржах: ${total_bal:.2f}\n{wallet_lines}"
                f"{balance_warn}"
            )
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown')

            # ── точный отчёт из мозга: кто нашёл, воронка, потолок НПД (живая БД) ──
            try:
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=nastya_brain.build_report(24),
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"brain report ошибка: {e}")

            await alerts_check(app.bot, YOUR_CHAT_ID)

            if msk_now().weekday() == 6:
                await weekly_system_report(app.bot, YOUR_CHAT_ID)

        except Exception as e:
            logger.error(f"daily_report ошибка: {e}")

# ═══ МОЗГ: ТОЧНЫЙ ОТЧЁТ + ФИКСАЦИЯ ДОХОДА ═══

async def brain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точный отчёт по живой базе (jobs+orders): кто нашёл, воронка, потолок НПД.
       /brain  или  /brain 48  (часы)"""
    hours = 24
    if context.args:
        try:
            hours = int(context.args[0])
        except ValueError:
            pass
    try:
        await update.message.reply_text(
            nastya_brain.build_report(hours), parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Мозг споткнулся: {e}")


async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фиксация РЕАЛЬНОГО дохода (рубли) -> копит оборот для потолка НПД 2.4 млн.
       Формат: /income 15000 описание заказа"""
    if not context.args:
        await update.message.reply_text(
            "💰 *Фиксация реального дохода*\n\n"
            "Формат: `/income 15000 ниши WB для клиента`\n"
            "Сумма в рублях — копит оборот для потолка НПД.",
            parse_mode='Markdown'
        )
        return
    try:
        amount = int(re.sub(r"[^\d]", "", context.args[0]))
        if amount <= 0:
            raise ValueError("сумма должна быть > 0")
    except Exception:
        await update.message.reply_text(
            "❌ Формат: `/income 15000 описание`", parse_mode='Markdown'
        )
        return
    desc = " ".join(context.args[1:]) or "без описания"
    if not nastya_brain.record_income(amount, desc):
        await update.message.reply_text("❌ Не нашла колонок в earnings для записи.")
        return
    m = nastya_brain.money(24)
    amt  = f"{amount:,}".replace(",", " ")
    turn = f"{m['turnover_12m']:,}".replace(",", " ")
    text = (
        f"✅ *Доход зафиксирован:* {amt}₽\n"
        f"_{desc}_\n\n"
        f"🏛 Оборот за 12 мес: {turn}₽ = {m['ceiling_pct']}%"
    )
    if m["ceiling_alert"]:
        text += "\n" + m["ceiling_alert"]
    await update.message.reply_text(text, parse_mode='Markdown')
    if LILU_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=LILU_CHAT_ID,
                text=f"💰 *Настя:* доход {amt}₽ — {desc}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"income -> Лиле: {e}")


# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    start_command))
    app.add_handler(CommandHandler("stats",    stats_command))
    app.add_handler(CommandHandler("today",    today_command))
    app.add_handler(CommandHandler("system",   system_analytics_command))
    app.add_handler(CommandHandler("forecast", forecast_command))
    app.add_handler(CommandHandler("wallets",  wallets_command))
    app.add_handler(CommandHandler("balances", balances_command))
    app.add_handler(CommandHandler("withdraw", withdraw_command))
    app.add_handler(CommandHandler("history",  history_command))
    app.add_handler(CommandHandler("pending",  pending_command))
    app.add_handler(CommandHandler("report",   report_command))
    app.add_handler(CommandHandler("goals",    goals_command))
    app.add_handler(CommandHandler("brain",    brain_command))
    app.add_handler(CommandHandler("income",   income_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application):
        asyncio.create_task(daily_report(application))
        try:
            if YOUR_CHAT_ID:
                await application.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        f"💰 *Анастасия v2.1 запущена!*\n\n"
                        f"🕐 {msk_time_str()}\n\n"
                        f"✅ Groq — ротация 4 моделей\n"
                        f"✅ Rate limit защита\n"
                        f"✅ Время МСК везде\n"
                        f"✅ Отчёт в 9:00 МСК (было 6:00 UTC)\n"
                        f"✅ Алерты если что-то не так\n\n"
                        f"⌨️ Кнопки снизу — жми, не печатай 👇"
                    ),
                    parse_mode='Markdown',
                    reply_markup=MAIN_KB
                )
        except Exception as e:
            logger.error(f"post_init: {e}")

    app.post_init = post_init
    logger.info("💰 Анастасия v2.1 запущена!")
    app.run_polling()

if __name__ == "__main__":
    main()
