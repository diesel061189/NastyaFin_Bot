# -*- coding: utf-8 -*-
"""
НАСТЯ — МОЗГ УЧЁТА (nastya_brain.py)
=====================================
Самодостаточный модуль. Считает реальную картину по живой базе.
"""

import os
import re
import sqlite3
from datetime import datetime, timedelta

# ─────────────────────────── КОНФИГ ───────────────────────────
DB_PATH       = os.getenv("DB_PATH", "/opt/bots/data/freelance.db")
NPD_CEILING   = 2_400_000
USD_RATE      = float(os.getenv("USD_RATE", "80"))
SILENCE_HOURS = 24

EXPECTED_BOTS = ["Полифан", "Карточник", "Охотник"]


# ─────────────────────────── БАЗА ───────────────────────────
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _table_cols(c, table):
    try:
        return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


# ─────────────────────────── ВРЕМЯ ───────────────────────────
def parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    s = s.split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ─────────────────────────── ДЕНЬГИ ИЗ ТЕКСТА ───────────────────────────
def money_from_text(s):
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        return int(s)
    nums = re.findall(r"\d[\d \u00a0]*", str(s))
    nums = [int(re.sub(r"[ \u00a0]", "", n)) for n in nums if re.sub(r"[ \u00a0]", "", n)]
    nums = [n for n in nums if n >= 100]
    return nums[0] if nums else 0


# ─────────────────────────── НОРМАЛИЗАЦИЯ ИСТОЧНИКА ───────────────────────────
def normalize(source, table):
    raw = (source or "").strip()
    bot, exch = None, raw

    if "|" in raw:
        prefix, exch = raw.split("|", 1)
        prefix, exch = prefix.strip(), exch.strip()
        if "Полифан" in prefix:
            bot = "Полифан"
        elif "Карточник" in prefix:
            bot = "Карточник"
        elif "Охот" in prefix:
            bot = "Охотник"
        else:
            bot = prefix or None

    if bot is None:
        if table == "orders":
            bot = "Охотник"
        else:
            bot = "Полифан"

    exch = exch.strip() or "—"
    return bot, exch


# ─────────────────────────── СБОР СТРОК ───────────────────────────
def _pull(c, table, since):
    cols = _table_cols(c, table)
    if not cols or "created_at" not in cols:
        return []
    price_col = "price" if "price" in cols else ("budget" if "budget" in cols else None)
    status_col = "status" if "status" in cols else None
    sel = ["source", "created_at"]
    if price_col:
        sel.append(price_col)
    if status_col:
        sel.append(status_col)
    rows = []
    for r in c.execute(f"SELECT {', '.join(sel)} FROM {table}"):
        ts = parse_ts(r["created_at"])
        if ts is None or ts < since:
            continue
        rows.append({
            "table": table,
            "source": r["source"] if "source" in r.keys() else "",
            "ts": ts,
            "money": money_from_text(r[price_col]) if price_col else 0,
            "status": (r[status_col] if status_col else "") or "",
        })
    return rows


def collect(hours=24):
    since = datetime.now() - timedelta(hours=hours)
    with _conn() as c:
        rows = _pull(c, "jobs", since) + _pull(c, "orders", since)
    return rows


# ─────────────────────────── АНАЛИТИКА ───────────────────────────
def activity(hours=24):
    rows = collect(hours)
    by_bot, by_exch, total_money = {}, {}, 0
    for r in rows:
        bot, exch = normalize(r["source"], r["table"])
        by_bot.setdefault(bot, {"count": 0, "money": 0})
        by_bot[bot]["count"] += 1
        by_bot[bot]["money"] += r["money"]
        by_exch.setdefault(exch, 0)
        by_exch[exch] += 1
        total_money += r["money"]
    return {"rows": rows, "by_bot": by_bot, "by_exch": by_exch,
            "total": len(rows), "money": total_money}


def funnel(hours=24):
    rows = collect(hours)
    f = {"найдено": 0, "отклик/принято": 0, "сдано": 0}
    for r in rows:
        st = r["status"].lower()
        if st in ("done", "completed", "сдано"):
            f["сдано"] += 1
        elif st in ("accepted", "отклик", "принято", "in_progress"):
            f["отклик/принято"] += 1
        else:
            f["найдено"] += 1
    return f


def silent_bots(hours=SILENCE_HOURS):
    seen = set(activity(hours)["by_bot"].keys())
    return [b for b in EXPECTED_BOTS if b not in seen]


def money(hours=24):
    out = {"earn_rub": 0, "earn_cnt": 0, "exp_rub": 0,
           "turnover_12m": 0, "ceiling_pct": 0.0, "ceiling_alert": None}
    since = datetime.now() - timedelta(hours=hours)
    y12   = datetime.now() - timedelta(days=365)
    with _conn() as c:
        ecols = _table_cols(c, "earnings")
        edate = "created_at" if "created_at" in ecols else ("date" if "date" in ecols else None)
        if "amount_rub" in ecols and edate:
            for r in c.execute(f"SELECT amount_rub, {edate} AS d FROM earnings"):
                ts = parse_ts(r["d"])
                if ts is None:
                    continue
                amt = r["amount_rub"] or 0
                if ts >= since:
                    out["earn_rub"] += amt
                    out["earn_cnt"] += 1
                if ts >= y12:
                    out["turnover_12m"] += amt
        xcols = _table_cols(c, "expenses")
        date_field = "date" if "date" in xcols else ("created_at" if "created_at" in xcols else None)
        if "amount_rub" in xcols and date_field:
            for r in c.execute(f"SELECT amount_rub, {date_field} AS d FROM expenses"):
                ts = parse_ts(r["d"])
                if ts and ts >= since:
                    out["exp_rub"] += (r["amount_rub"] or 0)

    out["ceiling_pct"] = round(out["turnover_12m"] / NPD_CEILING * 100, 1)
    p = out["ceiling_pct"]
    if p >= 95:
        out["ceiling_alert"] = "🚨 95%+ — СТОП крупным сделкам через НПД, срочно переводи поток на ИП УСН!"
    elif p >= 85:
        out["ceiling_alert"] = "🔴 85%+ — оформляй ИП УСН на этой неделе."
    elif p >= 70:
        out["ceiling_alert"] = "🟡 70%+ — начинай готовить документы на ИП УСН."
    return out


# ─────────────────────────── ОБЩИЙ ПРИЁМНИК СОБЫТИЙ ───────────────────────────
def _ensure_events(c):
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT, kind TEXT, amount_rub INTEGER DEFAULT 0,
        meta TEXT, created_at TEXT)""")


def log_event(project, kind, amount_rub=0, meta=""):
    with _conn() as c:
        _ensure_events(c)
        c.execute("INSERT INTO events(project,kind,amount_rub,meta,created_at) VALUES(?,?,?,?,?)",
                  (project, kind, int(amount_rub or 0), str(meta), datetime.now().isoformat()))
        c.commit()


def record_income(amount_rub, description="", source="manual"):
    now = datetime.now().isoformat()
    payload = {
        "amount_rub": int(amount_rub),
        "amount_usd": round(int(amount_rub) / USD_RATE, 2),
        "description": description,
        "source": source,
        "status": "received",
        "created_at": now,
        "date": now,
    }
    with _conn() as c:
        cols = _table_cols(c, "earnings")
        use = [k for k in payload if k in cols]
        if not use:
            return False
        c.execute(f"INSERT INTO earnings({','.join(use)}) VALUES({','.join('?'*len(use))})",
                  [payload[k] for k in use])
        c.commit()
    log_event("фриланс", "income", amount_rub, description)
    return True


# ─────────────────────────── СБОРКА ОТЧЁТА ───────────────────────────
def build_report(hours=24):
    a = activity(hours)
    f = funnel(hours)
    m = money(hours)
    silent = silent_bots(hours)

    L = []
    L.append(f"🌅 *НАСТЯ — ИТОГИ ЗА {hours}ч*")
    L.append(f"_{datetime.now():%d.%m.%Y %H:%M}_\n")

    L.append("🤖 *Кто сколько нашёл:*")
    if a["by_bot"]:
        for bot, d in sorted(a["by_bot"].items(), key=lambda x: -x[1]["count"]):
            pot = f" (~{d['money']:,}₽ потенциал)".replace(",", " ") if d["money"] else ""
            L.append(f"  • {bot}: {d['count']}{pot}")
    else:
        L.append("  • тишина — никто ничего не принёс")

    if silent:
        L.append("\n⚠️ *Молчат " + str(hours) + "ч:* " + ", ".join(silent))
        if "Охотник" in silent:
            L.append("  └ Охотник = Tampermonkey в браузере; молчит когда браузер закрыт")

    if a["by_exch"]:
        L.append("\n📡 *По биржам:*")
        for exch, cnt in sorted(a["by_exch"].items(), key=lambda x: -x[1]):
            L.append(f"  • {exch}: {cnt}")

    L.append("\n🔻 *Воронка:*")
    L.append(f"  найдено {f['найдено']} → отклик/принято {f['отклик/принято']} → сдано {f['сдано']}")

    L.append(f"\n💼 *Найдено всего:* {a['total']} заказов")
    if a["money"]:
        L.append(f"   потенциал бюджетов: ~{a['money']:,}₽".replace(",", " ") + " _(запрошено клиентами, не доход)_")

    L.append("\n💰 *Реальные деньги:*")
    L.append(f"  доход: {m['earn_rub']:,}₽ ({m['earn_cnt']} шт)".replace(",", " "))
    L.append(f"  расход: {m['exp_rub']:,}₽".replace(",", " "))
    L.append(f"  прибыль: {(m['earn_rub']-m['exp_rub']):,}₽".replace(",", " "))
    if m["earn_rub"] == 0:
        L.append("  _(ноль — пока не фиксируешь оплаты. Команда: /income 15000 заказ Х)_")

    L.append("\n🏛 *Потолок самозанятого (2.4 млн / 12 мес):*")
    L.append(f"  оборот за год: {m['turnover_12m']:,}₽ = {m['ceiling_pct']}%".replace(",", " "))
    if m["ceiling_alert"]:
        L.append("  " + m["ceiling_alert"])

    return "\n".join(L)


if __name__ == "__main__":
    print(build_report(24))
