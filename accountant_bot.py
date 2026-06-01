import os
import json
import logging
import asyncio
import sqlite3
import httpx
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.getenv("ACCOUNTANT_BOT_TOKEN")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
YOUR_CHAT_ID      = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID      = int(os.getenv("LILU_CHAT_ID", "0"))
DB_PATH           = os.getenv("DB_PATH", "/tmp/freelance.db")
USDT_WALLET       = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
AIDENTIKA_API_KEY = os.getenv("AIDENTIKA_API_KEY", "")
USD_RATE          = 90.0

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

# ═══ БД ═══
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

# ═══ БАЛАНСЫ СЕРВИСОВ ═══
async def get_usd_rate():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.exchangerate-api.com/v4/latest/USD")
            return r.json()["rates"].get("RUB", 90.0)
    except:
        return 90.0

async def get_aidentika_balance() -> dict:
    """Проверяем баланс искр Aidentika"""
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

async def get_anthropic_balance() -> dict:
    """Проверяем баланс Anthropic"""
    if not ANTHROPIC_API_KEY:
        return {"credits": -1, "status": "no_key"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/organizations/usage",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01"
                }
            )
            if r.status_code == 200:
                return {"status": "ok", "data": r.json()}
    except Exception as e:
        logger.error(f"Anthropic balance error: {e}")
    return {"credits": -1, "status": "error"}

async def check_all_balances() -> str:
    """Проверяем все балансы сервисов"""
    results = await asyncio.gather(
        get_aidentika_balance(),
        get_usd_rate(),
        return_exceptions=True
    )
    aidentika = results[0] if not isinstance(results[0], Exception) else {"available": -1, "status": "error"}
    usd_rate   = results[1] if not isinstance(results[1], Exception) else 90.0

    msg = "🔋 *БАЛАНСЫ СЕРВИСОВ*\n\n"

    # Aidentika
    if aidentika["status"] == "ok":
        sparks = aidentika["available"]
        cards_left = sparks // 8  # 4 искры classic + 4 premium
        if sparks >= 40:
            emoji = "🟢"
        elif sparks >= 16:
            emoji = "🟡"
        else:
            emoji = "🔴"
        msg += f"{emoji} *Aidentika:* {sparks} искр\n"
        msg += f"   └ ~{cards_left} двойных карточек осталось\n"
        if sparks < 16:
            msg += f"   ⚠️ *Пополни! Осталось мало*\n"
    elif aidentika["status"] == "no_key":
        msg += "⚫ *Aidentika:* ключ не настроен\n"
    else:
        msg += "🔴 *Aidentika:* ошибка проверки\n"

    # Groq (бесплатный — просто статус)
    msg += "\n🟢 *Groq API:* бесплатный тариф\n"
    msg += "   └ llama-3.3-70b-versatile\n"

    # Anthropic
    if ANTHROPIC_API_KEY:
        msg += "\n🟢 *Anthropic API:* ключ настроен\n"
        msg += "   └ Claude Haiku 3.5 (Карточник)\n"
    else:
        msg += "\n⚫ *Anthropic API:* ключ не настроен\n"

    # VPS
    msg += "\n🟢 *VPS:* 132.243.228.167\n"
    msg += "   └ Оплачен до 30.07.2026\n"

    msg += f"\n💱 *Курс USD:* ₽{usd_rate:.0f}\n"

    # Предупреждения
    warnings = []
    if aidentika.get("available", 0) < 16 and aidentika["status"] == "ok":
        warnings.append("⚠️ Пополни Aidentika — мало искр!")

    if warnings:
        msg += "\n━━━━━━━━━━━━━━━━\n"
        msg += "🚨 *ТРЕБУЕТ ВНИМАНИЯ:*\n"
        for w in warnings:
            msg += f"{w}\n"

    return msg

# ═══ РАСХОДЫ ═══
async def parse_expense_natural(text: str) -> dict | None:
    """Парсим расход из обычного текста через Anthropic"""
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
"расход 349 рублей aidentika" -> {{"amount": 349, "currency": "rub", "description": "Aidentika", "category": "api"}}

Если это НЕ расход — верни null"""

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5", "max_tokens": 100, "messages": [{"role": "user", "content": prompt}]}
            )
            text_resp = r.json()["content"][0]["text"].strip()
            if text_resp.lower() == "null":
                return None
            return json.loads(text_resp)
    except Exception as e:
        logger.error(f"parse_expense_natural error: {e}")
        return None

# ═══ ОТЧЁТЫ ═══
async def generate_report_for_lilu(stats, wallets):
    wallet_text = "\n".join([f"{PLATFORMS.get(p, p)}: ${b:.2f}" for p, b, _, _, _ in wallets]) if wallets else "Кошельки пусты"
    prompt = (
        f"Составь краткий финансовый отчёт для Лилы (генерального директора).\n\n"
        f"ДАННЫЕ:\n"
        f"Доходы за месяц: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
        f"Расходы: ${stats['exp_usd']:.2f}\n"
        f"Прибыль: ${stats['profit_usd']:.2f}\n"
        f"Заказов: {stats['earn_count']}\n"
        f"Всего выплачено: ${stats['total_paid_usd']:.2f}\n\n"
        f"Остатки по биржам:\n{wallet_text}\n\n"
        f"Напиши отчёт 3-4 предложения. Деловой тон. Скажи что хорошо и что нужно улучшить."
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 300}
        )
        return r.json()["choices"][0]["message"]["content"].strip()

async def send_report_to_lilu(bot, stats):
    if not LILU_CHAT_ID:
        return
    wallets = get_wallets()
    ai_summary = await generate_report_for_lilu(stats, wallets)
    wallet_lines = ""
    total_balance = 0
    for platform, bal_usd, bal_rub, earned, withdrawn in wallets:
        name = PLATFORMS.get(platform, platform)
        wallet_lines += f"├ {name}: ${bal_usd:.2f} / ₽{bal_rub:.0f}\n"
        total_balance += bal_usd

    msg = (
        f"📊 *ФИНАНСОВЫЙ ОТЧЁТ — АНАСТАСИЯ*\n"
        f"_{datetime.now().strftime('%d.%m.%Y %H:%M')}_\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 *ДОХОДЫ (месяц)*\n"
        f"├ USD: ${stats['earn_usd']:.2f}\n"
        f"├ RUB: ₽{stats['earn_rub']:.0f}\n"
        f"└ Заказов: {stats['earn_count']}\n\n"
        f"💸 *РАСХОДЫ:* ${stats['exp_usd']:.2f}\n"
        f"📈 *ПРИБЫЛЬ:* ${stats['profit_usd']:.2f} / ₽{stats['profit_rub']:.0f}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏦 *ОСТАТКИ ПО БИРЖАМ:*\n"
        f"{wallet_lines if wallet_lines else '  Пусто\n'}"
        f"💎 *ИТОГО НА БИРЖАХ:* ${total_balance:.2f}\n\n"
        f"💳 *USDT кошелёк:*\n"
        f"`{USDT_WALLET[:25]}...`\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🤖 *Анализ Анастасии:*\n"
        f"{ai_summary}"
    )
    await bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown')

# ═══ КОМАНДЫ ═══
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *Анастасия — Бот-Бухгалтер*\n\n"
        "Слежу за деньгами и балансами!\n\n"
        "/stats — статистика за месяц\n"
        "/wallets — остатки по биржам\n"
        "/balances — балансы всех сервисов\n"
        "/withdraw — записать вывод денег\n"
        "/add — добавить доход\n"
        "/expense — добавить расход\n"
        "/pending — ожидают оплаты\n"
        "/report — отчёт Лиле\n"
        "/goals — финансовые цели\n"
        "/history — история транзакций\n\n"
        "💬 *Или напиши обычным текстом:*\n"
        "_«потратил 390р на айдентику»_\n"
        "_«заработал 500 рублей на FL»_",
        parse_mode='Markdown'
    )

async def balances_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показываем балансы всех сервисов"""
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
    msg = f"🏦 *ОСТАТКИ ПО БИРЖАМ*\n_Курс: 1 USD = ₽{USD_RATE:.0f}_\n\n"
    for platform, bal_usd, bal_rub, earned, withdrawn in wallets:
        name = PLATFORMS.get(platform, platform)
        bar_pct = min(10, int(bal_usd / max(total_usd, 1) * 10))
        bar = "█" * bar_pct + "░" * (10 - bar_pct)
        msg += f"{name}\n"
        msg += f"  [{bar}] ${bal_usd:.2f} / ₽{bal_rub:.0f}\n"
        msg += f"  Заработано: ${earned:.2f} | Выведено: ${withdrawn:.2f}\n\n"
    msg += f"━━━━━━━━━━━━━━━━\n"
    msg += f"💎 *ИТОГО:* ${total_usd:.2f} / ₽{total_rub:.0f}"
    keyboard = [[
        InlineKeyboardButton("💸 Вывести", callback_data="withdraw_start"),
        InlineKeyboardButton("📊 Отчёт Лиле", callback_data="send_lilu"),
        InlineKeyboardButton("🔋 Сервисы", callback_data="refresh_balances")
    ]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = get_wallets()
    if not wallets:
        await update.message.reply_text("Нет активных кошельков. Сначала добавь доход через /add")
        return
    keyboard = []
    for platform, bal_usd, bal_rub, _, _ in wallets:
        if bal_usd > 0:
            name = PLATFORMS.get(platform, platform)
            keyboard.append([InlineKeyboardButton(f"{name} — ${bal_usd:.2f}", callback_data=f"withdraw_{platform}")])
    if not keyboard:
        await update.message.reply_text("Все кошельки пусты 🤷")
        return
    await update.message.reply_text("💸 *Вывод денег*\n\nС какой биржи выводишь?", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = get_wallet_history()
    if not history:
        await update.message.reply_text("История пуста")
        return
    msg = "📋 *ИСТОРИЯ ТРАНЗАКЦИЙ*\n\n"
    for platform, tx_type, usd, rub, desc, date in history:
        name = PLATFORMS.get(platform, platform)
        emoji = "➕" if tx_type == 'earn' else "➖"
        d = date[:10] if date else "?"
        msg += f"{emoji} {d} | {name}\n   ${usd:.2f} — {desc[:40]}\n\n"
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
        f"_Курс: 1 USD = ₽{USD_RATE:.0f}_\n\n"
        f"✅ Доходы: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
        f"❌ Расходы: ${stats['exp_usd']:.2f} / ₽{stats['exp_rub']:.0f}\n"
        f"{emoji} Прибыль: ${stats['profit_usd']:.2f} / ₽{stats['profit_rub']:.0f}\n"
        f"📦 Заказов: {stats['earn_count']}\n"
        f"🏆 Всего выплачено: ${stats['total_paid_usd']:.2f}"
    )
    keyboard = [[
        InlineKeyboardButton("🏦 Кошельки", callback_data="show_wallets"),
        InlineKeyboardButton("🔋 Сервисы", callback_data="refresh_balances"),
        InlineKeyboardButton("📊 Лиле", callback_data="send_lilu")
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
        d = date[:10] if date else "?"
        msg += f"• {d} — {desc[:40]} — ${amt:.2f} ({source})\n"
        keyboard.append([InlineKeyboardButton(f"✅ Оплачен: ${amt:.2f}", callback_data=f"paid_{eid}")])
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats("month")
    await update.message.reply_text("📊 Готовлю отчёт для Лилы...")
    await send_report_to_lilu(context.application.bot, stats)
    await update.message.reply_text("✅ Отчёт отправлен Лиле!")

async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats("month")
    targets = [("🥉 Первый доллар", 1), ("🥈 $100/месяц", 100), ("🥇 $500/месяц", 500), ("💎 $1000/месяц", 1000)]
    msg = "🎯 *ФИНАНСОВЫЕ ЦЕЛИ*\n\n"
    for name, target in targets:
        earned = stats['earn_usd']
        pct = min(100, int(earned / target * 100))
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        status = "✅" if earned >= target else "⏳"
        msg += f"{status} *{name}*\n[{bar}] {pct}%\n${earned:.2f} / ${target}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

# ═══ КНОПКИ ═══
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

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
            name = PLATFORMS.get(platform, platform)
            msg += f"{name}: ${bal_usd:.2f} / ₽{bal_rub:.0f}\n"
        await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown')

    elif data == "refresh_balances":
        await query.edit_message_text("🔄 Проверяю балансы...")
        msg = await check_all_balances()
        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_balances")]]
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "withdraw_start":
        wallets = get_wallets()
        keyboard = []
        for platform, bal_usd, _, _, _ in wallets:
            if bal_usd > 0:
                name = PLATFORMS.get(platform, platform)
                keyboard.append([InlineKeyboardButton(f"{name} — ${bal_usd:.2f}", callback_data=f"withdraw_{platform}")])
        await query.edit_message_text("💸 С какой биржи выводишь?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("withdraw_"):
        platform = data[9:]
        name = PLATFORMS.get(platform, platform)
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

# ═══ ОБРАБОТКА СООБЩЕНИЙ (умный парсинг) ═══
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Команды с параметрами
    if text.startswith('/add '):
        parts = text[5:].split(' ', 2)
        if len(parts) >= 2:
            try:
                amount_str = parts[0].lower()
                is_rub = 'rub' in amount_str or 'р' in amount_str
                amount = float(amount_str.replace('rub','').replace('р',''))
                source = parts[1]
                desc = parts[2] if len(parts) > 2 else "Без описания"
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
            is_rub = 'rub' in amount_str or 'р' in amount_str or int(float(amount_str.replace('rub','').replace('р',''))) > 100
            amount = float(amount_str.replace('rub','').replace('р',''))
            desc = parts[1] if len(parts) > 1 else "Без описания"
            add_expense(amount, "Расход", desc, is_rub)
            sym = "₽" if is_rub else "$"
            await update.message.reply_text(f"✅ Расход записан: {sym}{amount:.0f} — {desc}")
        except:
            await update.message.reply_text("❌ Формат: `/expense 390 Aidentika`", parse_mode='Markdown')

    elif text.startswith('/withdraw_amount '):
        platform = context.user_data.get('withdraw_platform')
        if not platform:
            await update.message.reply_text("Сначала выбери биржу через /withdraw")
            return
        try:
            amount_str = text[17:].strip().lower()
            is_rub = 'rub' in amount_str
            amount = float(amount_str.replace('rub', ''))
            name = PLATFORMS.get(platform, platform)
            if is_rub:
                update_wallet(platform, amount / USD_RATE, amount, 'withdraw', "Вывод на карту")
                await update.message.reply_text(f"💸 Вывод записан: ₽{amount:.0f} с {name}")
            else:
                update_wallet(platform, amount, amount * USD_RATE, 'withdraw', f"Вывод ${amount:.2f}")
                await update.message.reply_text(f"💸 Вывод записан: ${amount:.2f} с {name}")
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
        # ─── Умный парсинг обычного текста ───
        expense_data = await parse_expense_natural(text)
        if expense_data:
            amount = expense_data["amount"]
            is_rub = expense_data["currency"] == "rub"
            desc = expense_data["description"]
            category = expense_data.get("category", "other")
            add_expense(amount, category, desc, is_rub)
            sym = "₽" if is_rub else "$"
            await update.message.reply_text(
                f"✅ *Расход записан автоматически!*\n\n"
                f"💸 {sym}{amount:.0f} — {desc}\n"
                f"📁 Категория: {category}\n\n"
                f"_Написала неправильно? Используй /expense_",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "Привет! Я Анастасия — слежу за деньгами 💰\n\n"
                "Используй команды:\n"
                "/stats /wallets /balances /add /expense\n\n"
                "Или напиши обычным текстом:\n"
                "_«потратил 390р на айдентику»_",
                parse_mode='Markdown'
            )

# ═══ ЕЖЕДНЕВНЫЙ ОТЧЁТ ═══
async def daily_report(app):
    while True:
        now = datetime.now()
        next_9 = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= next_9:
            next_9 += timedelta(days=1)
        await asyncio.sleep((next_9 - now).total_seconds())
        try:
            global USD_RATE
            USD_RATE = await get_usd_rate()
            stats = get_stats("today")
            wallets = get_wallets()
            total_bal = sum(w[1] for w in wallets)

            # Проверяем балансы сервисов
            aidentika = await get_aidentika_balance()
            balance_warn = ""
            if aidentika["status"] == "ok" and aidentika["available"] < 16:
                balance_warn = f"\n\n⚠️ *Aidentika:* осталось {aidentika['available']} искр — пополни!"

            wallet_lines = "\n".join([
                f"  {PLATFORMS.get(p, p)}: ${b:.2f}"
                for p, b, _, _, _ in wallets if b > 0
            ])
            msg = (
                f"🌅 *Доброе утро! Итоги вчера:*\n\n"
                f"💰 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
                f"📦 Заказов: {stats['earn_count']}\n"
                f"💸 Расходы: ${stats['exp_usd']:.2f}\n"
                f"📈 Прибыль: ${stats['profit_usd']:.2f}\n\n"
                f"🏦 На биржах: ${total_bal:.2f}\n{wallet_lines}"
                f"{balance_warn}"
            )
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown')

            # По пятницам — отчёт Лиле
            if datetime.now().weekday() == 4:
                stats_week = get_stats("week")
                await send_report_to_lilu(app.bot, stats_week)
        except Exception as e:
            logger.error(f"Ошибка дейли: {e}")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    start_command))
    app.add_handler(CommandHandler("stats",    stats_command))
    app.add_handler(CommandHandler("today",    today_command))
    app.add_handler(CommandHandler("wallets",  wallets_command))
    app.add_handler(CommandHandler("balances", balances_command))
    app.add_handler(CommandHandler("withdraw", withdraw_command))
    app.add_handler(CommandHandler("history",  history_command))
    app.add_handler(CommandHandler("pending",  pending_command))
    app.add_handler(CommandHandler("report",   report_command))
    app.add_handler(CommandHandler("goals",    goals_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application):
        asyncio.create_task(daily_report(application))
    app.post_init = post_init

    logger.info("💰 Анастасия запущена!")
    app.run_polling()

if __name__ == "__main__":
    main()
