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

TELEGRAM_TOKEN = os.getenv("ACCOUNTANT_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID = int(os.getenv("LILU_CHAT_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")
USDT_WALLET = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
USD_RATE = 90.0

PLATFORMS = {
    "guru": "🟠 Guru.com",
    "pph": "🔵 PeoplePerHour",
    "fl": "🇷🇺 FL.ru",
    "weblancer": "🇷🇺 Weblancer",
    "freelance": "🇷🇺 Freelance.ru",
    "paypal": "💳 PayPal",
    "wallet": "📱 TG Wallet (USDT)",
    "card": "💳 Карта РФ",
    "other": "💼 Другое",
}

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

async def get_usd_rate():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.exchangerate-api.com/v4/latest/USD")
            return r.json()["rates"].get("RUB", 90.0)
    except:
        return 90.0

async def generate_report_for_lilu(stats, wallets):
    wallet_text = "\n".join([f"{PLATFORMS.get(p, p)}: ${b:.2f}" for p, b, _, _, _ in wallets]) if wallets else "Кошельки пусты"
    prompt = (
        "Составь краткий финансовый отчёт для Лилы (генерального директора).\n\n"
        "ДАННЫЕ:\n"
        "Доходы за месяц: ${:.2f} / ₽{:.0f}\n"
        "Расходы: ${:.2f}\n"
        "Прибыль: ${:.2f}\n"
        "Заказов: {}\n"
        "Всего выплачено: ${:.2f}\n\n"
        "Остатки по биржам:\n{}\n\n"
        "Напиши отчёт 3-4 предложения. Деловой тон. Скажи что хорошо и что нужно улучшить."
    ).format(
        stats['earn_usd'], stats['earn_rub'],
        stats['exp_usd'], stats['profit_usd'],
        stats['earn_count'], stats['total_paid_usd'],
        wallet_text
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + GROQ_API_KEY, "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 300}
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
        wallet_lines += "├ {}: ${:.2f} / ₽{:.0f}\n".format(name, bal_usd, bal_rub)
        total_balance += bal_usd

    msg = (
        "📊 *ФИНАНСОВЫЙ ОТЧЁТ — АНАСТАСИЯ*\n"
        "_{}_\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "💰 *ДОХОДЫ (месяц)*\n"
        "├ USD: ${:.2f}\n"
        "├ RUB: ₽{:.0f}\n"
        "└ Заказов: {}\n\n"
        "💸 *РАСХОДЫ:* ${:.2f}\n"
        "📈 *ПРИБЫЛЬ:* ${:.2f} / ₽{:.0f}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🏦 *ОСТАТКИ ПО БИРЖАМ:*\n"
        "{}"
        "💎 *ИТОГО НА БИРЖАХ:* ${:.2f}\n\n"
        "💳 *USDT кошелёк:*\n"
        "`{}...`\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🤖 *Анализ Анастасии:*\n"
        "{}"
    ).format(
        datetime.now().strftime('%d.%m.%Y %H:%M'),
        stats['earn_usd'], stats['earn_rub'], stats['earn_count'],
        stats['exp_usd'],
        stats['profit_usd'], stats['profit_rub'],
        wallet_lines if wallet_lines else "  Пусто\n",
        total_balance,
        USDT_WALLET[:25],
        ai_summary
    )
    await bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown')

# ═══ КОМАНДЫ ═══
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *Анастасия — Бот-Бухгалтер*\n\n"
        "Слежу за деньгами на всех биржах!\n\n"
        "/stats — статистика за месяц\n"
        "/wallets — остатки по биржам\n"
        "/withdraw — записать вывод денег\n"
        "/add — добавить доход\n"
        "/expense — добавить расход\n"
        "/pending — ожидают оплаты\n"
        "/report — отчёт Лиле\n"
        "/goals — финансовые цели\n"
        "/history — история транзакций",
        parse_mode='Markdown'
    )

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global USD_RATE
    USD_RATE = await get_usd_rate()
    wallets = get_wallets()
    if not wallets:
        await update.message.reply_text("🏦 Кошельки пусты — добавь первый доход через /add")
        return
    total_usd = sum(w[1] for w in wallets)
    total_rub = sum(w[2] for w in wallets)
    msg = "🏦 *ОСТАТКИ ПО БИРЖАМ*\n_Курс: 1 USD = ₽{:.0f}_\n\n".format(USD_RATE)
    for platform, bal_usd, bal_rub, earned, withdrawn in wallets:
        name = PLATFORMS.get(platform, platform)
        bar_pct = min(10, int(bal_usd / max(total_usd, 1) * 10))
        bar = "█" * bar_pct + "░" * (10 - bar_pct)
        msg += "{}\n".format(name)
        msg += "  [{}] ${:.2f} / ₽{:.0f}\n".format(bar, bal_usd, bal_rub)
        msg += "  Заработано: ${:.2f} | Выведено: ${:.2f}\n\n".format(earned, withdrawn)
    msg += "━━━━━━━━━━━━━━━━\n"
    msg += "💎 *ИТОГО:* ${:.2f} / ₽{:.0f}".format(total_usd, total_rub)
    keyboard = [[
        InlineKeyboardButton("💸 Вывести", callback_data="withdraw_start"),
        InlineKeyboardButton("📊 Отчёт Лиле", callback_data="send_lilu")
    ]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    wallets = get_wallets()
    if not wallets:
        await update.message.reply_text("Нет активных кошельков. Сначала добавь доход через /add")
        return
    for platform, bal_usd, bal_rub, _, _ in wallets:
        if bal_usd > 0:
            name = PLATFORMS.get(platform, platform)
            keyboard.append([InlineKeyboardButton(
                "{} — ${:.2f}".format(name, bal_usd),
                callback_data="withdraw_" + platform
            )])
    if not keyboard:
        await update.message.reply_text("Все кошельки пусты 🤷")
        return
    await update.message.reply_text(
        "💸 *Вывод денег*\n\nС какой биржи выводишь?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
        msg += "{} {} | {}\n   ${:.2f} — {}\n\n".format(emoji, d, name, usd, desc[:40])
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "month")

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "today")

async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "week")

async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "all")

async def show_stats(update, period):
    global USD_RATE
    USD_RATE = await get_usd_rate()
    stats = get_stats(period)
    names = {"today": "СЕГОДНЯ", "week": "НЕДЕЛЮ", "month": "МЕСЯЦ", "all": "ВСЁ ВРЕМЯ"}
    emoji = "📈" if stats['profit_usd'] >= 0 else "📉"
    msg = (
        "💰 *СТАТИСТИКА ЗА {}*\n"
        "_Курс: 1 USD = ₽{:.0f}_\n\n"
        "✅ Доходы: ${:.2f} / ₽{:.0f}\n"
        "❌ Расходы: ${:.2f}\n"
        "{} Прибыль: ${:.2f} / ₽{:.0f}\n"
        "📦 Заказов: {}\n"
        "🏆 Всего выплачено: ${:.2f}"
    ).format(
        names.get(period, 'МЕСЯЦ'), USD_RATE,
        stats['earn_usd'], stats['earn_rub'],
        stats['exp_usd'],
        emoji, stats['profit_usd'], stats['profit_rub'],
        stats['earn_count'],
        stats['total_paid_usd']
    )
    keyboard = [[
        InlineKeyboardButton("🏦 Кошельки", callback_data="show_wallets"),
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
        msg += "• {} — {} — ${:.2f} ({})\n".format(d, desc[:40], amt, source)
        keyboard.append([InlineKeyboardButton(
            "✅ Оплачен: ${:.2f}".format(amt),
            callback_data="paid_{}".format(eid)
        )])
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats("month")
    await update.message.reply_text("📊 Готовлю отчёт для Лилы...")
    await send_report_to_lilu(context.application.bot, stats)
    await update.message.reply_text("✅ Отчёт отправлен Лиле!")

async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats("month")
    targets = [
        ("🥉 Первый доллар", 1),
        ("🥈 $100/месяц", 100),
        ("🥇 $500/месяц", 500),
        ("💎 $1000/месяц", 1000)
    ]
    msg = "🎯 *ФИНАНСОВЫЕ ЦЕЛИ*\n\n"
    for name, target in targets:
        earned = stats['earn_usd']
        pct = min(100, int(earned / target * 100))
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        status = "✅" if earned >= target else "⏳"
        msg += "{} *{}*\n[{}] {}%\n${:.2f} / ${}\n\n".format(
            status, name, bar, pct, earned, target
        )
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
            msg += "{}: ${:.2f} / ₽{:.0f}\n".format(name, bal_usd, bal_rub)
        await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown')

    elif data == "withdraw_start":
        wallets = get_wallets()
        keyboard = []
        for platform, bal_usd, _, _, _ in wallets:
            if bal_usd > 0:
                name = PLATFORMS.get(platform, platform)
                keyboard.append([InlineKeyboardButton(
                    "{} — ${:.2f}".format(name, bal_usd),
                    callback_data="withdraw_" + platform
                )])
        await query.edit_message_text(
            "💸 С какой биржи выводишь?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("withdraw_"):
        platform = data[9:]
        name = PLATFORMS.get(platform, platform)
        context.user_data['withdraw_platform'] = platform
        await query.edit_message_text(
            "💸 *Вывод с {}*\n\n"
            "Напиши сумму:\n"
            "`/withdraw_amount 50` — в USD\n"
            "`/withdraw_amount 4500rub` — в рублях".format(name),
            parse_mode='Markdown'
        )

    elif data.startswith("paid_"):
        mark_paid(int(data[5:]))
        await query.edit_message_text("✅ Отмечено как оплаченное!")

# ═══ ТЕКСТОВЫЕ КОМАНДЫ ═══
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.startswith('/add '):
        parts = text[5:].split(' ', 2)
        if len(parts) >= 2:
            try:
                amount_str = parts[0].lower()
                is_rub = 'rub' in amount_str
                amount = float(amount_str.replace('rub', ''))
                source = parts[1] if len(parts) > 1 else "other"
                desc = parts[2] if len(parts) > 2 else "Без описания"
                if is_rub:
                    add_earning(amount / USD_RATE, source, desc, amount)
                    await update.message.reply_text(
                        "✅ Добавлено: ₽{:.0f} от {}".format(amount, PLATFORMS.get(source, source))
                    )
                else:
                    add_earning(amount, source, desc)
                    await update.message.reply_text(
                        "✅ Добавлено: ${:.2f} от {}\n\nОстаток на бирже обновлён 🏦".format(
                            amount, PLATFORMS.get(source, source)
                        )
                    )
            except Exception:
                await update.message.reply_text(
                    "❌ Формат: `/add 25 guru Описание`\n\nБиржи: guru, pph, fl, weblancer, freelance",
                    parse_mode='Markdown'
                )

    elif text.startswith('/expense '):
        parts = text[9:].split(' ', 1)
        if len(parts) >= 1:
            try:
                amount_str = parts[0].lower()
                is_rub = 'rub' in amount_str
                amount = float(amount_str.replace('rub', ''))
                desc = parts[1] if len(parts) > 1 else "Без описания"
                add_expense(amount, "Расход", desc, is_rub)
                sym = "₽" if is_rub else "$"
                await update.message.reply_text("✅ Расход: {}{:.2f} — {}".format(sym, amount, desc))
            except Exception:
                await update.message.reply_text(
                    "❌ Формат: `/expense 5 Railway`",
                    parse_mode='Markdown'
                )

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
                await update.message.reply_text(
                    "💸 *Вывод записан!*\n\nС биржи: {}\nСумма: ₽{:.0f}\n\nЛила уведомлена ✅".format(
                        name, amount
                    ),
                    parse_mode='Markdown'
                )
                # ИСПРАВЛЕНная строка — без вложенных кавычек
                lilu_text = "💸 *Анастасия докладывает:*\n\nАртём вывел ₽{:.0f} с {}\n\nОстатки обновлены в базе 📊".format(
                    amount, name
                )
            else:
                update_wallet(platform, amount, amount * USD_RATE, 'withdraw', "Вывод ${:.2f}".format(amount))
                await update.message.reply_text(
                    "💸 *Вывод записан!*\n\nС биржи: {}\nСумма: ${:.2f} / ₽{:.0f}\n\nЛила уведомлена ✅".format(
                        name, amount, amount * USD_RATE
                    ),
                    parse_mode='Markdown'
                )
                # ИСПРАВЛЕНная строка — без вложенных кавычек
                lilu_text = "💸 *Анастасия докладывает:*\n\nАртём вывел ${:.2f} с {}\n\nОстатки обновлены в базе 📊".format(
                    amount, name
                )

            if LILU_CHAT_ID:
                await context.bot.send_message(
                    chat_id=LILU_CHAT_ID,
                    text=lilu_text,
                    parse_mode='Markdown'
                )
            context.user_data.pop('withdraw_platform', None)

        except Exception as e:
            await update.message.reply_text(
                "❌ Ошибка: {}\n\nФормат: `/withdraw_amount 50` или `/withdraw_amount 4500rub`".format(str(e)),
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
            wallet_lines = "\n".join([
                "  {}: ${:.2f}".format(PLATFORMS.get(p, p), b)
                for p, b, _, _, _ in wallets if b > 0
            ])
            msg = (
                "🌅 *Доброе утро! Итоги вчера:*\n\n"
                "💰 Заработано: ${:.2f} / ₽{:.0f}\n"
                "📦 Заказов: {}\n"
                "💸 Расходы: ${:.2f}\n"
                "📈 Прибыль: ${:.2f}\n\n"
                "🏦 На биржах: ${:.2f}\n{}"
            ).format(
                stats['earn_usd'], stats['earn_rub'],
                stats['earn_count'],
                stats['exp_usd'],
                stats['profit_usd'],
                total_bal, wallet_lines
            )
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown')
            if datetime.now().weekday() == 4:
                stats_week = get_stats("week")
                await send_report_to_lilu(app.bot, stats_week)
        except Exception as e:
            logger.error("Ошибка дейли: {}".format(e))

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("stats",   stats_command))
    app.add_handler(CommandHandler("today",   today_command))
    app.add_handler(CommandHandler("week",    week_command))
    app.add_handler(CommandHandler("all",     all_command))
    app.add_handler(CommandHandler("wallets", wallets_command))
    app.add_handler(CommandHandler("withdraw", withdraw_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("report",  report_command))
    app.add_handler(CommandHandler("goals",   goals_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application):
        asyncio.create_task(daily_report(application))
    app.post_init = post_init

    logger.info("💰 Анастасия запущена!")
    app.run_polling()

if __name__ == "__main__":
    main()
