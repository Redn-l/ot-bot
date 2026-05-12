import os
import logging
import threading
import time
from datetime import date, timedelta

import psycopg as psycopg2
import requests
import schedule

os.environ["PGCLIENTENCODING"] = "UTF8"
os.environ["PYTHONIOENCODING"] = "utf-8"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
)
log = logging.getLogger(__name__)

# Конфиг — берётся из переменных окружения (Railway) или захардкожен локально
BOT_TOKEN  = os.getenv("BOT_TOKEN",  "8351287651:AAGzfWmo_hfU8bEtZEzkOZdxBrvfNzDwevM")
CHAT_ID    = int(os.getenv("CHAT_ID", "-5175454015"))
ADMIN_TAG  = os.getenv("ADMIN_TAG",  "@Redn_l")
DAILY_TIME = os.getenv("DAILY_TIME", "09:00")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5434"))
DB_NAME = os.getenv("DB_NAME", "ot_monitoring")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "1")

# Названия СИЗ как в БД
PPE_TYPES = {
    "Каска защитная":           "ppe_helmet_days",
    "Страховочная система":     "ppe_harness_days",
    "Перчатки диэлектрические": "ppe_gloves_days",
    "Пояс монтажный":           "ppe_helmet_days",  # срок как у каски
}


def get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
        options="-c client_encoding=UTF8"
    )


def load_settings(cur):
    cur.execute("SELECT param_name, param_value::int FROM settings")
    return {r[0]: r[1] for r in cur.fetchall()}


def send(text, chat_id=None):
    if chat_id is None:
        chat_id = CHAT_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            log.error("TG %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("send error: %s", e)


def send_parts(blocks, chat_id=None):
    if not blocks:
        return
    msg = ""
    for block in blocks:
        chunk = block + "\n\n"
        if len(msg) + len(chunk) > 3800:
            send(msg.rstrip(), chat_id)
            msg = chunk
        else:
            msg += chunk
    if msg.strip():
        send(msg.rstrip(), chat_id)


def days_str(days):
    if days < 0:
        return f"просрочено {abs(days)} дн. назад"
    if days == 0:
        return "истекает сегодня"
    return f"через {days} дн."


def icon(days):
    return "🔴" if days < 0 else "🟡"


def get_ppe_validity(ppe_type, cfg):
    key = PPE_TYPES.get(ppe_type, "ppe_helmet_days")
    return cfg.get(key, 365)


def trainings_expiring(cur, cfg, horizon):
    v = cfg.get("training_validity_days", 365)
    today = date.today()
    cur.execute("""
        SELECT e.full_name, t.training_type,
               MAX(t.training_date),
               (MAX(t.training_date) + %(v)s * INTERVAL '1 day')::date
        FROM employees e
        JOIN trainings t ON e.employee_id = t.employee_id
        GROUP BY e.employee_id, e.full_name, t.training_type
        HAVING (MAX(t.training_date) + %(v)s * INTERVAL '1 day')::date <= %(lim)s
        ORDER BY 4
    """, {"v": v, "lim": today + timedelta(days=horizon)})
    rows = []
    for name, ttype, last, expiry in cur.fetchall():
        rows.append((name, f"Инструктаж — {ttype}", last, expiry,
                     (expiry - today).days))
    return rows


def medical_expiring(cur, cfg, horizon):
    v = cfg.get("medical_validity_days", 365)
    today = date.today()
    cur.execute("""
        SELECT e.full_name,
               MAX(m.medical_date),
               (MAX(m.medical_date) + %(v)s * INTERVAL '1 day')::date
        FROM employees e
        JOIN medical m ON e.employee_id = m.employee_id
        GROUP BY e.employee_id, e.full_name
        HAVING (MAX(m.medical_date) + %(v)s * INTERVAL '1 day')::date <= %(lim)s
        ORDER BY 3
    """, {"v": v, "lim": today + timedelta(days=horizon)})
    rows = []
    for name, last, expiry in cur.fetchall():
        rows.append((name, "Медосмотр", last, expiry, (expiry - today).days))
    return rows


def ppe_expiring(cur, cfg, horizon):
    today = date.today()
    # Строим CASE динамически из словаря PPE_TYPES
    cur.execute("""
        SELECT e.full_name, p.ppe_type,
               MAX(p.issue_date),
               (MAX(p.issue_date) + CASE p.ppe_type
                   WHEN 'Каска защитная'           THEN %(h)s  * INTERVAL '1 day'
                   WHEN 'Страховочная система'      THEN %(ha)s * INTERVAL '1 day'
                   WHEN 'Перчатки диэлектрические'  THEN %(g)s  * INTERVAL '1 day'
                   WHEN 'Пояс монтажный'            THEN %(h)s  * INTERVAL '1 day'
                   ELSE 365 * INTERVAL '1 day' END)::date
        FROM employees e
        JOIN ppe p ON e.employee_id = p.employee_id
        GROUP BY e.employee_id, e.full_name, p.ppe_type
        HAVING (MAX(p.issue_date) + CASE p.ppe_type
                   WHEN 'Каска защитная'           THEN %(h)s  * INTERVAL '1 day'
                   WHEN 'Страховочная система'      THEN %(ha)s * INTERVAL '1 day'
                   WHEN 'Перчатки диэлектрические'  THEN %(g)s  * INTERVAL '1 day'
                   WHEN 'Пояс монтажный'            THEN %(h)s  * INTERVAL '1 day'
                   ELSE 365 * INTERVAL '1 day' END)::date <= %(lim)s
        ORDER BY 4
    """, {
        "h":  cfg.get("ppe_helmet_days",  365),
        "ha": cfg.get("ppe_harness_days", 365),
        "g":  cfg.get("ppe_gloves_days",  180),
        "lim": today + timedelta(days=horizon),
    })
    rows = []
    for name, ptype, last, expiry in cur.fetchall():
        rows.append((name, f"СИЗ — {ptype}", last, expiry, (expiry - today).days))
    return rows


def summary_counts(cur, cfg):
    vt = cfg.get("training_validity_days", 365)
    vm = cfg.get("medical_validity_days",  365)
    h  = cfg.get("ppe_helmet_days",  365)
    ha = cfg.get("ppe_harness_days", 365)
    g  = cfg.get("ppe_gloves_days",  180)
    wd = cfg.get("expiring_notification_days", 30)
    today = date.today()
    cur.execute("""
        WITH t_pr AS (
            SELECT employee_id,
                MAX(CASE
                    WHEN (MAX(training_date) + %(vt)s * INTERVAL '1 day')::date < %(today)s THEN 3
                    WHEN (MAX(training_date) + %(vt)s * INTERVAL '1 day')::date <= %(warn)s  THEN 2
                    ELSE 1 END) AS pr
            FROM trainings GROUP BY employee_id
        ),
        m_pr AS (
            SELECT employee_id,
                CASE
                    WHEN (MAX(medical_date) + %(vm)s * INTERVAL '1 day')::date < %(today)s THEN 3
                    WHEN (MAX(medical_date) + %(vm)s * INTERVAL '1 day')::date <= %(warn)s  THEN 2
                    ELSE 1 END AS pr
            FROM medical GROUP BY employee_id
        ),
        p_pr AS (
            SELECT employee_id,
                MAX(CASE
                    WHEN (issue_date + CASE ppe_type
                        WHEN 'Каска защитная'           THEN %(h)s  * INTERVAL '1 day'
                        WHEN 'Страховочная система'      THEN %(ha)s * INTERVAL '1 day'
                        WHEN 'Перчатки диэлектрические'  THEN %(g)s  * INTERVAL '1 day'
                        WHEN 'Пояс монтажный'            THEN %(h)s  * INTERVAL '1 day'
                        ELSE 365 * INTERVAL '1 day' END)::date < %(today)s THEN 3
                    WHEN (issue_date + CASE ppe_type
                        WHEN 'Каска защитная'           THEN %(h)s  * INTERVAL '1 day'
                        WHEN 'Страховочная система'      THEN %(ha)s * INTERVAL '1 day'
                        WHEN 'Перчатки диэлектрические'  THEN %(g)s  * INTERVAL '1 day'
                        WHEN 'Пояс монтажный'            THEN %(h)s  * INTERVAL '1 day'
                        ELSE 365 * INTERVAL '1 day' END)::date <= %(warn)s  THEN 2
                    ELSE 1 END) AS pr
            FROM ppe GROUP BY employee_id
        ),
        fin AS (
            SELECT e.employee_id,
                GREATEST(COALESCE(t.pr,1), COALESCE(m.pr,1), COALESCE(p.pr,1)) AS fp
            FROM employees e
            LEFT JOIN t_pr t ON e.employee_id = t.employee_id
            LEFT JOIN m_pr m ON e.employee_id = m.employee_id
            LEFT JOIN p_pr p ON e.employee_id = p.employee_id
        )
        SELECT COUNT(*),
               COUNT(*) FILTER(WHERE fp=3),
               COUNT(*) FILTER(WHERE fp=2),
               COUNT(*) FILTER(WHERE fp=1)
        FROM fin
    """, {"vt": vt, "vm": vm, "h": h, "ha": ha, "g": g,
          "today": today, "warn": today + timedelta(days=wd)})
    total, bad, warn_cnt, ok = cur.fetchone()
    return int(total), int(bad), int(warn_cnt), int(ok)


def employee_card(cur, cfg, last_name, first_name):
    cur.execute("""
        SELECT employee_id, full_name, department, position
        FROM employees
        WHERE lower(full_name) LIKE lower(%(pat)s)
        LIMIT 3
    """, {"pat": f"{last_name} {first_name}%"})
    found = cur.fetchall()
    if not found:
        return []

    today = date.today()
    result = []
    for eid, name, dept, pos in found:
        records = []

        cur.execute("""
            SELECT training_type, MAX(training_date),
                   (MAX(training_date) + %(v)s * INTERVAL '1 day')::date
            FROM trainings WHERE employee_id = %(eid)s
            GROUP BY training_type ORDER BY 3
        """, {"eid": eid, "v": cfg.get("training_validity_days", 365)})
        for ttype, last_d, expiry in cur.fetchall():
            records.append((f"Инструктаж — {ttype}", last_d, expiry,
                            (expiry - today).days))

        cur.execute("""
            SELECT MAX(medical_date),
                   (MAX(medical_date) + %(v)s * INTERVAL '1 day')::date
            FROM medical WHERE employee_id = %(eid)s
        """, {"eid": eid, "v": cfg.get("medical_validity_days", 365)})
        row = cur.fetchone()
        if row and row[0]:
            records.append(("Медосмотр", row[0], row[1], (row[1] - today).days))

        h  = cfg.get("ppe_helmet_days",  365)
        ha = cfg.get("ppe_harness_days", 365)
        g  = cfg.get("ppe_gloves_days",  180)
        cur.execute("""
            SELECT ppe_type, MAX(issue_date),
                   (MAX(issue_date) + CASE ppe_type
                       WHEN 'Каска защитная'           THEN %(h)s  * INTERVAL '1 day'
                       WHEN 'Страховочная система'      THEN %(ha)s * INTERVAL '1 day'
                       WHEN 'Перчатки диэлектрические'  THEN %(g)s  * INTERVAL '1 day'
                       WHEN 'Пояс монтажный'            THEN %(h)s  * INTERVAL '1 day'
                       ELSE 365 * INTERVAL '1 day' END)::date
            FROM ppe WHERE employee_id = %(eid)s
            GROUP BY ppe_type ORDER BY 3
        """, {"eid": eid, "h": h, "ha": ha, "g": g})
        for ptype, last_d, expiry in cur.fetchall():
            records.append((f"СИЗ — {ptype}", last_d, expiry,
                            (expiry - today).days))

        result.append({
            "name": name, "dept": dept, "pos": pos, "records": records
        })
    return result


def fmt_record(label, last_d, expiry, days):
    ic = icon(days) if days <= 30 else "🟢"
    return (
        f"{ic} {label}\n"
        f"   выдано/пройдено: {last_d.strftime('%d.%m.%Y')} "
        f"/ действует до {expiry.strftime('%d.%m.%Y')}\n"
        f"   {days_str(days)}"
    )


def daily_report():
    log.info("ежедневный отчёт")
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cfg  = load_settings(cur)
        total, bad, warn_cnt, ok = summary_counts(cur, cfg)

        today_str = date.today().strftime("%d.%m.%Y")
        text = (
            f"<b>Охрана труда — {today_str}</b>\n\n"
            f"Всего сотрудников: {total}\n"
            f"🔴 Не допущены: {bad}\n"
            f"🟡 Требует внимания: {warn_cnt}\n"
            f"🟢 Допущены: {ok}"
        )
        if bad > 0:
            text += f"\n\n{ADMIN_TAG} есть недопущенные сотрудники"
        send(text)

        if bad == 0 and warn_cnt == 0:
            send("Нарушений нет, все документы в порядке.")
            cur.close()
            conn.close()
            return

        horizon = cfg.get("expiring_notification_days", 30)
        all_rows = (
            trainings_expiring(cur, cfg, horizon) +
            medical_expiring(cur, cfg, horizon) +
            ppe_expiring(cur, cfg, horizon)
        )
        cur.close()
        conn.close()

        if not all_rows:
            return

        expired  = [r for r in all_rows if r[4] < 0]
        expiring = [r for r in all_rows if r[4] >= 0]

        blocks = []
        if expired:
            blocks.append(f"<b>Просрочено — {len(expired)} записей</b>")
            for name, label, last_d, expiry, days in expired:
                blocks.append(
                    f"🔴 {name}\n"
                    f"   {label}\n"
                    f"   до {expiry.strftime('%d.%m.%Y')} — {days_str(days)}"
                )

        if expiring:
            blocks.append(f"<b>Истекает в ближайшие {horizon} дн. — {len(expiring)} записей</b>")
            for name, label, last_d, expiry, days in expiring:
                blocks.append(
                    f"🟡 {name}\n"
                    f"   {label}\n"
                    f"   до {expiry.strftime('%d.%m.%Y')} — {days_str(days)}"
                )

        send_parts(blocks)

    except Exception as e:
        log.error("daily_report: %s", e)
        send(f"Ошибка при формировании отчёта: {e}")


def cmd_today(chat_id, cfg_override=None):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cfg  = load_settings(cur)
        horizon = cfg.get("expiring_notification_days", 30)
        rows = (
            [r for r in trainings_expiring(cur, cfg, horizon) if r[4] >= 0] +
            [r for r in medical_expiring(cur, cfg, horizon)   if r[4] >= 0] +
            [r for r in ppe_expiring(cur, cfg, horizon)       if r[4] >= 0]
        )
        cur.close()
        conn.close()
        if not rows:
            send("Всё в порядке, в ближайшие 30 дней ничего не истекает.", chat_id)
            return
        blocks = [f"<b>Истекает в ближайшие {horizon} дней</b>"]
        for name, label, last_d, expiry, days in rows:
            blocks.append(
                f"🟡 {name}\n"
                f"   {label} — до {expiry.strftime('%d.%m.%Y')} ({days_str(days)})"
            )
        send_parts(blocks, chat_id)
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


def cmd_all(chat_id):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cfg  = load_settings(cur)
        rows = (
            trainings_expiring(cur, cfg, 90) +
            medical_expiring(cur, cfg, 90) +
            ppe_expiring(cur, cfg, 90)
        )
        cur.close()
        conn.close()
        if not rows:
            send("Нарушений нет.", chat_id)
            return
        expired  = [r for r in rows if r[4] < 0]
        expiring = [r for r in rows if r[4] >= 0]
        blocks = []
        if expired:
            blocks.append(f"<b>Просрочено ({len(expired)})</b>")
            for name, label, _, expiry, days in expired:
                blocks.append(f"🔴 {name} — {label} ({days_str(days)})")
        if expiring:
            blocks.append(f"<b>Истекает скоро ({len(expiring)})</b>")
            for name, label, _, expiry, days in expiring:
                blocks.append(
                    f"🟡 {name} — {label}, до {expiry.strftime('%d.%m.%Y')} ({days_str(days)})"
                )
        send_parts(blocks, chat_id)
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


def cmd_status(chat_id, last_name, first_name):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cfg  = load_settings(cur)
        data = employee_card(cur, cfg, last_name, first_name)
        cur.close()
        conn.close()

        if not data:
            send(f"Сотрудник {last_name} {first_name} не найден.", chat_id)
            return

        for emp in data:
            records = emp["records"]
            if not records:
                send(f"{emp['name']} — нет данных по документам.", chat_id)
                continue

            min_days = min(r[3] for r in records)
            if min_days < 0:
                overall = "🔴 Не допущен"
            elif min_days <= 30:
                overall = "🟡 Требует внимания"
            else:
                overall = "🟢 Допущен"

            blocks = [
                f"<b>{emp['name']}</b>\n"
                f"{emp['pos']}, {emp['dept']}\n"
                f"Статус: {overall}"
            ]
            for label, last_d, expiry, days in records:
                blocks.append(fmt_record(label, last_d, expiry, days))

            send_parts(blocks, chat_id)
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


def get_updates(offset):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35
        )
        if r.ok:
            return r.json().get("result", [])
    except Exception as e:
        log.warning("getUpdates: %s", e)
    return []


def handle(text, chat_id):
    text = text.strip()
    low  = text.lower()

    if low.startswith("/today"):
        cmd_today(chat_id)

    elif low.startswith("/all"):
        cmd_all(chat_id)

    elif low.startswith("/status"):
        after = text[len("/status"):].strip()
        words = after.split()
        if len(words) < 2:
            send(
                "Формат: /status Фамилия Имя\n"
                "Пример: /status Морозов Александр",
                chat_id
            )
            return
        cmd_status(chat_id, words[0], words[1])

    elif low.startswith("/start") or low.startswith("/help"):
        send(
            "<b>Команды:</b>\n\n"
            "/today — что истекает в ближайшие 30 дней\n"
            "/all — все нарушения за 90 дней\n"
            "/status Фамилия Имя — карточка сотрудника\n\n"
            f"Ежедневный отчёт приходит в {DAILY_TIME}",
            chat_id
        )

    else:
        send("Неизвестная команда. /help — список команд.", chat_id)


def run_scheduler():
    schedule.every().day.at(DAILY_TIME).do(daily_report)
    log.info("планировщик запущен, отчёт в %s", DAILY_TIME)
    while True:
        schedule.run_pending()
        time.sleep(30)


def main():
    log.info("бот запущен")
    threading.Thread(target=run_scheduler, daemon=True).start()

    send(f"Бот запущен. Ежедневный отчёт в {DAILY_TIME}. /help — команды.")

    offset = 0
    while True:
        updates = get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            text    = msg.get("text", "")
            chat_id = msg["chat"]["id"]
            if text.startswith("/"):
                log.info("команда %s от %s", text, chat_id)
                handle(text, chat_id)
        time.sleep(1)


if __name__ == "__main__":
    main()
