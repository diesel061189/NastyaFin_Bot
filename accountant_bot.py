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

# ═══ НАСТРОЙКИ ═══
TELEGRAM_TOKEN = os.getenv("ACCOUNTANT_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID = int(os.getenv("LILU_CHAT_ID", "0"))      # ID чата с Лилой
LILU_BOT_TOKEN = os.getenv("LILU_BOT_TOKEN", "")        # Токен Лилы
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")
USDT_WALLET = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")

# Курс USD/RUB (обновляется автоматически)
USD_RATE = 90.0

async def get_usd_rate() -> float:
    """Получаем актуальный курс USD/RUB"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.exchangerate-api.com/v4/latest/USD")
            data = r.json()
            return data["rates"].get("RUB", 90.0)
    except:
        return 90.0

# ═══ БАЗА ДАННЫХ ═══
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Таблица заработка
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT,
        amount_usd REAL,
        amount_rub REAL,
        source TEXT,
        description TEXT,
        status TEXT DEFAULT 'pending',
        date TEXT,
        paid_date TEXT
    )''')
    
    # Таблица расходов
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount_usd REAL,
        amount_rub REAL,
        category TEXT,
        description TEXT,
        date TEXT
    )''')
    
    # Таблица целей
    c.execute('''CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        target_usd REAL,
        deadline TEXT,
        created_at TEXT
    )''')
    
    conn.commit()
    conn.close()

def add_earning(amount_usd: float, source: str, description: str, amount_rub: float = 0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if amount_rub == 0:
        amount_rub = amount_usd * USD_RATE
    c.execute('''INSERT INTO earnings (amount_usd, amount_rub, source, description, status, date)
                 VALUES (?, ?, ?, ?, "pending", ?)''',
              (amount_usd, amount_rub, source, description, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def add_expense(amount: float, category: str, description: str, is_rub: bool = False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if is_rub:
        amount_rub = amount
        amount_usd = amount / USD_RATE
    else:
        amount_usd = amount
        amount_rub = amount * USD_RATE
    c.execute('''INSERT INTO expenses (amount_usd, amount_rub, category, description, date)
                 VALUES (?, ?, ?, ?, ?)''',
              (amount_usd, amount_rub, category, description, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_stats(period: str = "month") -> dict:
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
    
    # Доходы
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0), COUNT(*) FROM earnings WHERE date >= ?', (date_from,))
    earn = c.fetchone()
    
    # Расходы
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0) FROM expenses WHERE date >= ?', (date_from,))
    exp = c.fetchone()
    
    # По источникам
    c.execute('SELECT source, COUNT(*), COALESCE(SUM(amount_usd),0) FROM earnings WHERE date >= ? GROUP BY source', (date_from,))
    by_source = c.fetchall()
    
    # Последние 5 операций
    c.execute('SELECT description, amount_usd, source, date FROM earnings ORDER BY date DESC LIMIT 5')
    recent = c.fetchall()
    
    # Всего за всё время
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COUNT(*) FROM earnings WHERE status="paid"')
    total_paid = c.fetchone()
    
    conn.close()
    
    profit_usd = earn[0] - exp[0]
    profit_rub = earn[1] - exp[1]
    
    return {
        'earn_usd': earn[0], 'earn_rub': earn[1], 'earn_count': earn[2],
        'exp_usd': exp[0], 'exp_rub': exp[1],
        'profit_usd': profit_usd, 'profit_rub': profit_rub,
        'by_source': by_source, 'recent': recent,
        'total_paid_usd': total_paid[0], 'total_paid_count': total_paid[1]
    }

def get_pending_earnings():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, description, amount_usd, source, date FROM earnings WHERE status="pending" ORDER BY date DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def mark_paid(earning_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE earnings SET status="paid", paid_date=? WHERE id=?',
              (datetime.now().isoformat(), earning_id))
    conn.commit()
    conn.close()

# ═══ AI ОТЧЁТ ═══
async def generate_report_for_lilu(stats: dict) -> str:
    prompt = f"""Ты бот-бухгалтер. Составь краткий финансовый отчёт для Лилы (генерального директора) на русском языке.

ДАННЫЕ ЗА МЕСЯЦ:
- Заработано: ${stats['earn_usd']:.2f} (₽{stats['earn_rub']:.0f})
- Расходы: ${stats['exp_usd']:.2f} (₽{stats['exp_rub']:.0f})
- Прибыль: ${stats['profit_usd']:.2f} (₽{stats['profit_rub']:.0f})
- Выполнено заказов: {stats['earn_count']}
- Всего выплачено за всё время: ${stats['total_paid_usd']:.2f}

По источникам: {stats['by_source']}

Напиши отчёт в 3-4 предложения. Деловой тон. Укажи что идёт хорошо и что нужно улучшить."""

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 300}
        )
        return r.json()["choices"][0]["message"]["content"].strip()

# ═══ ОТПРАВКА ОТЧЁТА ЛИЛЕ ═══
async def send_report_to_lilu(bot, stats: dict):
    """Отправляет финансовый отчёт Лиле"""
    if not LILU_CHAT_ID:
        return
    
    ai_summary = await generate_report_for_lilu(stats)
    
    msg = f"""📊 *ФИНАНСОВЫЙ ОТЧЁТ — БУХГАЛТЕР*
_{datetime.now().strftime('%d.%m.%Y %H:%M')}_

━━━━━━━━━━━━━━━━
💰 *ДОХОДЫ (месяц)*
├ USD: ${stats['earn_usd']:.2f}
├ RUB: ₽{stats['earn_rub']:.0f}
└ Заказов: {stats['earn_count']}

💸 *РАСХОДЫ*
├ USD: ${stats['exp_usd']:.2f}
└ RUB: ₽{stats['exp_rub']:.0f}

📈 *ПРИБЫЛЬ*
├ USD: ${stats['profit_usd']:.2f}
└ RUB: ₽{stats['profit_rub']:.0f}

🏆 *ВСЕГО ВЫПЛАЧЕНО*: ${stats['total_paid_usd']:.2f}

💳 *USDT кошелёк:*
`{USDT_WALLET[:20]}...`

━━━━━━━━━━━━━━━━
🤖 *Анализ:*
{ai_summary}"""

    await bot.send_message(
        chat_id=LILU_CHAT_ID,
        text=msg,
        parse_mode='Markdown'
    )

# ═══ КОМАНДЫ ═══
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *Бот-Бухгалтер запущен!*\n\n"
        "Команды:\n"
        "/stats — статистика за месяц\n"
        "/today — статистика за сегодня\n"
        "/week — статистика за неделю\n"
        "/all — за всё время\n"
        "/pending — ожидают оплаты\n"
        "/add — добавить доход вручную\n"
        "/expense — добавить расход\n"
        "/report — отправить отчёт Лиле\n"
        "/goals — финансовые цели",
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "month")

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "today")

async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "week")

async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update, "all")

async def show_stats(update: Update, period: str):
    global USD_RATE
    USD_RATE = await get_usd_rate()
    stats = get_stats(period)
    
    period_names = {"today": "СЕГОДНЯ", "week": "НЕДЕЛЮ", "month": "МЕСЯЦ", "all": "ВСЁ ВРЕМЯ"}
    period_name = period_names.get(period, "МЕСЯЦ")
    
    # По источникам
    sources_text = ""
    for src, cnt, amt in stats['by_source']:
        sources_text += f"  {src}: {cnt} зак. / ${amt:.2f}\n"
    
    # Последние операции
    recent_text = ""
    for desc, amt, src, date in stats['recent']:
        d = date[:10] if date else "?"
        recent_text += f"  • {d} +${amt:.2f} — {desc[:30]}\n"
    
    profit_emoji = "📈" if stats['profit_usd'] >= 0 else "📉"
    
    msg = f"""💰 *СТАТИСТИКА ЗА {period_name}*
_Курс: 1 USD = ₽{USD_RATE:.0f}_

━━━━━━━━━━━━━━━━
✅ *Доходы:* ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}
❌ *Расходы:* ${stats['exp_usd']:.2f} / ₽{stats['exp_rub']:.0f}
{profit_emoji} *Прибыль:* ${stats['profit_usd']:.2f} / ₽{stats['profit_rub']:.0f}
📦 *Заказов:* {stats['earn_count']}

📡 *По источникам:*
{sources_text if sources_text else '  Пока нет данных'}
🕐 *Последние операции:*
{recent_text if recent_text else '  Пока нет операций'}
🏆 *Всего выплачено:* ${stats['total_paid_usd']:.2f} ({stats['total_paid_count']} заказов)"""

    keyboard = [[
        InlineKeyboardButton("📊 Отправить Лиле", callback_data="send_lilu"),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_{period}")
    ]]
    
    await update.message.reply_text(
        msg, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = get_pending_earnings()
    if not pending:
        await update.message.reply_text("✅ Нет ожидающих оплат!")
        return
    
    msg = "⏳ *ОЖИДАЮТ ОПЛАТЫ:*\n\n"
    keyboard = []
    
    for row in pending:
        eid, desc, amt, source, date = row
        d = date[:10] if date else "?"
        msg += f"• {d} — {desc[:40]} — ${amt:.2f} ({source})\n"
        keyboard.append([InlineKeyboardButton(
            f"✅ Оплачен: {desc[:20]} ${amt:.2f}",
            callback_data=f"paid_{eid}"
        )])
    
    await update.message.reply_text(
        msg, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💵 *Добавить доход*\n\n"
        "Напиши в формате:\n"
        "`/add 25 Guru.com Написал статью про SEO`\n\n"
        "Или:\n"
        "`/add 2000rub FL.ru Перевод документа`",
        parse_mode='Markdown'
    )

async def expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💸 *Добавить расход*\n\n"
        "Напиши в формате:\n"
        "`/expense 5 Подписка Railway`\n"
        "`/expense 500rub Интернет`",
        parse_mode='Markdown'
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats("month")
    await update.message.reply_text("📊 Готовлю отчёт для Лилы...")
    await send_report_to_lilu(context.application.bot, stats)
    await update.message.reply_text("✅ Отчёт отправлен Лиле!")

async def goals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats("month")
    targets = [
        ("🥉 Первый доллар", 1),
        ("🥈 $100 за месяц", 100),
        ("🥇 $500 за месяц", 500),
        ("💎 $1000 за месяц", 1000),
    ]
    
    msg = "🎯 *ФИНАНСОВЫЕ ЦЕЛИ*\n\n"
    for name, target in targets:
        earned = stats['earn_usd']
        pct = min(100, int(earned / target * 100))
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        status = "✅" if earned >= target else "⏳"
        msg += f"{status} *{name}*\n"
        msg += f"  [{bar}] {pct}%\n"
        msg += f"  ${earned:.2f} / ${target}\n\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых команд"""
    text = update.message.text.strip()
    
    # /add 25 Guru.com Описание
    if text.startswith('/add '):
        parts = text[5:].split(' ', 2)
        if len(parts) >= 2:
            try:
                amount_str = parts[0].lower()
                is_rub = 'rub' in amount_str
                amount = float(amount_str.replace('rub', ''))
                source = parts[1] if len(parts) > 1 else "Вручную"
                desc = parts[2] if len(parts) > 2 else "Без описания"
                
                if is_rub:
                    add_earning(amount / USD_RATE, source, desc, amount)
                    await update.message.reply_text(f"✅ Добавлено: ₽{amount:.0f} от {source}")
                else:
                    add_earning(amount, source, desc)
                    await update.message.reply_text(f"✅ Добавлено: ${amount:.2f} от {source}")
            except:
                await update.message.reply_text("❌ Формат: /add 25 Guru.com Описание")
    
    # /expense 5 Описание
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
                await update.message.reply_text(f"✅ Расход записан: {sym}{amount:.2f} — {desc}")
            except:
                await update.message.reply_text("❌ Формат: /expense 5 Описание")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "send_lilu":
        stats = get_stats("month")
        await send_report_to_lilu(context.application.bot, stats)
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(chat_id=YOUR_CHAT_ID, text="✅ Отчёт отправлен Лиле!")
    
    elif data.startswith("refresh_"):
        period = data[8:]
        stats = get_stats(period)
        await query.answer("Обновлено!")
    
    elif data.startswith("paid_"):
        earning_id = int(data[5:])
        mark_paid(earning_id)
        await query.edit_message_text("✅ Отмечено как оплаченное!")

# ═══ ЕЖЕДНЕВНЫЙ ОТЧЁТ ═══
async def daily_report(app):
    """Каждый день в 9:00 шлёт отчёт"""
    while True:
        now = datetime.now()
        # Следующие 9:00
        next_9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= next_9:
            next_9 += timedelta(days=1)
        wait_seconds = (next_9 - now).total_seconds()
        
        await asyncio.sleep(wait_seconds)
        
        try:
            global USD_RATE
            USD_RATE = await get_usd_rate()
            stats = get_stats("today")
            
            # Утренний дайджест тебе
            msg = (f"🌅 *ДОБРОЕ УТРО! Итоги вчера:*\n\n"
                   f"💰 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
                   f"📦 Заказов: {stats['earn_count']}\n"
                   f"💸 Расходы: ${stats['exp_usd']:.2f}\n"
                   f"📈 Прибыль: ${stats['profit_usd']:.2f}")
            
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown')
            
            # Недельный отчёт по пятницам
            if datetime.now().weekday() == 4:
                stats_week = get_stats("week")
                await send_report_to_lilu(app.bot, stats_week)
                
        except Exception as e:
            logger.error(f"Ошибка дейли отчёта: {e}")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("all", all_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("goals", goals_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application):
        asyncio.create_task(daily_report(application))
    app.post_init = post_init

    logger.info("💰 Бот-Бухгалтер запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
