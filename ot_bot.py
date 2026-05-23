import os
import logging
import threading
import time
import json
from datetime import date, timedelta, datetime

import psycopg2
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

BOT_TOKEN  = os.getenv("BOT_TOKEN",  "8351287651:AAGzfWmo_hfU8bEtZEzkOZdxBrvfNzDwevM")
CHAT_ID    = int(os.getenv("CHAT_ID", "-5175454015"))
ADMIN_TAG  = os.getenv("ADMIN_TAG",  "@Redn_l")
DAILY_TIME = os.getenv("DAILY_TIME", "09:00")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5434"))
DB_NAME = os.getenv("DB_NAME", "ot_monitoring")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "1")

# Инструктажи которые проводятся ОДИН РАЗ при приёме — срок не истекает
# По ГОСТ 12.0.004-2015
ONE_TIME_TRAININGS = {"Вводный", "Первичный"}

# Типы СИЗ → параметр срока в settings
PPE_DAYS = {
    "Каска защитная":           "ppe_helmet_days",
    "Страховочная система":     "ppe_harness_days",
    "Перчатки диэлектрические": "ppe_gloves_days",
    "Пояс монтажный":           "ppe_helmet_days",
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


# ── Telegram API ──────────────────────────────────────────────────────────────

def send(text, chat_id=None, keyboard=None):
    """Отправить сообщение, опционально с inline-клавиатурой."""
    if chat_id is None:
        chat_id = CHAT_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            log.error("TG %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("send error: %s", e)


def answer_callback(callback_query_id, text=""):
    """Ответить на нажатие кнопки (убрать анимацию загрузки)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=5)
    except Exception as e:
        log.warning("answerCallback: %s", e)


def send_parts(blocks, chat_id=None, keyboard=None):
    """Разбить длинный список на сообщения по 3800 символов."""
    if not blocks:
        return
    msg = ""
    last_idx = len(blocks) - 1
    for i, block in enumerate(blocks):
        chunk = block + "\n\n"
        if len(msg) + len(chunk) > 3800:
            send(msg.rstrip(), chat_id)
            msg = chunk
        else:
            msg += chunk
    if msg.strip():
        # Клавиатуру добавляем только к последнему сообщению
        send(msg.rstrip(), chat_id, keyboard=keyboard)


def days_str(days):
    if days < 0:
        return f"просрочено {abs(days)} дн. назад"
    if days == 0:
        return "истекает сегодня"
    return f"через {days} дн."


def icon(days):
    return "🔴" if days < 0 else "🟡"


# ── Запросы к БД ──────────────────────────────────────────────────────────────

def trainings_expiring(cur, cfg, horizon, ref_date=None):
    """
    Инструктажи истёкшие или истекающие в пределах horizon дней от ref_date.
    Исключает одноразовые инструктажи (Вводный, Первичный).
    """
    if ref_date is None:
        ref_date = date.today()
    v = cfg.get("training_validity_days", 365)

    # Формируем placeholders для исключённых типов
    excl = tuple(ONE_TIME_TRAININGS)
    excl_ph = ",".join(["%s"] * len(excl))

    cur.execute(f"""
        SELECT e.full_name, t.training_type,
               MAX(t.training_date),
               (MAX(t.training_date) + %(v)s * INTERVAL '1 day')::date
        FROM employees e
        JOIN trainings t ON e.employee_id = t.employee_id
        WHERE t.training_type NOT IN ({excl_ph})
        GROUP BY e.employee_id, e.full_name, t.training_type
        HAVING (MAX(t.training_date) + %(v)s * INTERVAL '1 day')::date <= %(lim)s
        ORDER BY 4
    """, {"v": v, "lim": ref_date + timedelta(days=horizon), **{}} )

    # psycopg2 не поддерживает смешанный стиль — передаём как список
    cur.execute(f"""
        SELECT e.full_name, t.training_type,
               MAX(t.training_date),
               (MAX(t.training_date) + %s * INTERVAL '1 day')::date
        FROM employees e
        JOIN trainings t ON e.employee_id = t.employee_id
        WHERE t.training_type NOT IN ({excl_ph})
        GROUP BY e.employee_id, e.full_name, t.training_type
        HAVING (MAX(t.training_date) + %s * INTERVAL '1 day')::date <= %s
        ORDER BY 4
    """, [v] + list(excl) + [v, ref_date + timedelta(days=horizon)])

    rows = []
    for name, ttype, last, expiry in cur.fetchall():
        rows.append((name, f"Инструктаж — {ttype}", last, expiry,
                     (expiry - ref_date).days))
    return rows


def medical_expiring(cur, cfg, horizon, ref_date=None):
    if ref_date is None:
        ref_date = date.today()
    v = cfg.get("medical_validity_days", 365)
    cur.execute("""
        SELECT e.full_name,
               MAX(m.medical_date),
               (MAX(m.medical_date) + %s * INTERVAL '1 day')::date
        FROM employees e
        JOIN medical m ON e.employee_id = m.employee_id
        GROUP BY e.employee_id, e.full_name
        HAVING (MAX(m.medical_date) + %s * INTERVAL '1 day')::date <= %s
        ORDER BY 3
    """, [v, v, ref_date + timedelta(days=horizon)])
    rows = []
    for name, last, expiry in cur.fetchall():
        rows.append((name, "Медосмотр", last, expiry, (expiry - ref_date).days))
    return rows


def ppe_expiring(cur, cfg, horizon, ref_date=None):
    if ref_date is None:
        ref_date = date.today()
    h  = cfg.get("ppe_helmet_days",  365)
    ha = cfg.get("ppe_harness_days", 365)
    g  = cfg.get("ppe_gloves_days",  180)
    cur.execute("""
        SELECT e.full_name, p.ppe_type,
               MAX(p.issue_date),
               (MAX(p.issue_date) + CASE p.ppe_type
                   WHEN 'Каска защитная'           THEN %s * INTERVAL '1 day'
                   WHEN 'Страховочная система'      THEN %s * INTERVAL '1 day'
                   WHEN 'Перчатки диэлектрические'  THEN %s * INTERVAL '1 day'
                   WHEN 'Пояс монтажный'            THEN %s * INTERVAL '1 day'
                   ELSE 365 * INTERVAL '1 day' END)::date
        FROM employees e
        JOIN ppe p ON e.employee_id = p.employee_id
        GROUP BY e.employee_id, e.full_name, p.ppe_type
        HAVING (MAX(p.issue_date) + CASE p.ppe_type
                   WHEN 'Каска защитная'           THEN %s * INTERVAL '1 day'
                   WHEN 'Страховочная система'      THEN %s * INTERVAL '1 day'
                   WHEN 'Перчатки диэлектрические'  THEN %s * INTERVAL '1 day'
                   WHEN 'Пояс монтажный'            THEN %s * INTERVAL '1 day'
                   ELSE 365 * INTERVAL '1 day' END)::date <= %s
        ORDER BY 4
    """, [h, ha, g, h, h, ha, g, h, ref_date + timedelta(days=horizon)])
    rows = []
    for name, ptype, last, expiry in cur.fetchall():
        rows.append((name, f"СИЗ — {ptype}", last, expiry, (expiry - ref_date).days))
    return rows


def summary_counts(cur, cfg, ref_date=None):
    """Сводка: total / не допущены / внимание / допущены на указанную дату."""
    if ref_date is None:
        ref_date = date.today()
    vt = cfg.get("training_validity_days", 365)
    vm = cfg.get("medical_validity_days",  365)
    h  = cfg.get("ppe_helmet_days",  365)
    ha = cfg.get("ppe_harness_days", 365)
    g  = cfg.get("ppe_gloves_days",  180)
    wd = cfg.get("expiring_notification_days", 30)
    warn = ref_date + timedelta(days=wd)

    excl = tuple(ONE_TIME_TRAININGS)
    excl_ph = ",".join(["%s"] * len(excl))

    cur.execute(f"""
        WITH t_max AS (
            SELECT employee_id, training_type,
                   MAX(training_date) AS last_date
            FROM trainings
            WHERE training_type NOT IN ({excl_ph})
            GROUP BY employee_id, training_type
        ),
        t_pr AS (
            SELECT employee_id,
                MAX(CASE
                    WHEN (last_date + %s * INTERVAL '1 day')::date < %s THEN 3
                    WHEN (last_date + %s * INTERVAL '1 day')::date <= %s THEN 2
                    ELSE 1 END) AS pr
            FROM t_max GROUP BY employee_id
        ),
        m_max AS (
            SELECT employee_id, MAX(medical_date) AS last_date
            FROM medical GROUP BY employee_id
        ),
        m_pr AS (
            SELECT employee_id,
                CASE
                    WHEN (last_date + %s * INTERVAL '1 day')::date < %s THEN 3
                    WHEN (last_date + %s * INTERVAL '1 day')::date <= %s THEN 2
                    ELSE 1 END AS pr
            FROM m_max
        ),
        p_max AS (
            SELECT employee_id, ppe_type, MAX(issue_date) AS last_date
            FROM ppe GROUP BY employee_id, ppe_type
        ),
        p_pr AS (
            SELECT employee_id,
                MAX(CASE
                    WHEN (last_date + CASE ppe_type
                        WHEN 'Каска защитная'           THEN %s * INTERVAL '1 day'
                        WHEN 'Страховочная система'      THEN %s * INTERVAL '1 day'
                        WHEN 'Перчатки диэлектрические'  THEN %s * INTERVAL '1 day'
                        WHEN 'Пояс монтажный'            THEN %s * INTERVAL '1 day'
                        ELSE 365 * INTERVAL '1 day' END)::date < %s THEN 3
                    WHEN (last_date + CASE ppe_type
                        WHEN 'Каска защитная'           THEN %s * INTERVAL '1 day'
                        WHEN 'Страховочная система'      THEN %s * INTERVAL '1 day'
                        WHEN 'Перчатки диэлектрические'  THEN %s * INTERVAL '1 day'
                        WHEN 'Пояс монтажный'            THEN %s * INTERVAL '1 day'
                        ELSE 365 * INTERVAL '1 day' END)::date <= %s THEN 2
                    ELSE 1 END) AS pr
            FROM p_max GROUP BY employee_id
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
    """, list(excl) + [
        vt, ref_date, vt, warn,
        vm, ref_date, vm, warn,
        h, ha, g, h, ref_date, h, ha, g, h, warn,
    ])
    total, bad, warn_cnt, ok = cur.fetchone()
    return int(total), int(bad), int(warn_cnt), int(ok)


def employee_card(cur, cfg, last_name, first_name, ref_date=None):
    """Карточка сотрудника на указанную дату."""
    if ref_date is None:
        ref_date = date.today()
    cur.execute("""
        SELECT employee_id, full_name, department, position
        FROM employees
        WHERE lower(full_name) LIKE lower(%s)
        LIMIT 3
    """, [f"{last_name} {first_name}%"])
    found = cur.fetchall()
    if not found:
        return []

    result = []
    for eid, name, dept, pos in found:
        records = []
        v = cfg.get("training_validity_days", 365)

        # Только периодические инструктажи
        excl = tuple(ONE_TIME_TRAININGS)
        excl_ph = ",".join(["%s"] * len(excl))
        cur.execute(f"""
            SELECT training_type, MAX(training_date),
                   (MAX(training_date) + %s * INTERVAL '1 day')::date
            FROM trainings
            WHERE employee_id = %s AND training_type NOT IN ({excl_ph})
            GROUP BY training_type ORDER BY 3
        """, [v, eid] + list(excl))
        for ttype, last_d, expiry in cur.fetchall():
            records.append((f"Инструктаж — {ttype}", last_d, expiry,
                            (expiry - ref_date).days))

        # Медосмотр
        cur.execute("""
            SELECT MAX(medical_date),
                   (MAX(medical_date) + %s * INTERVAL '1 day')::date
            FROM medical WHERE employee_id = %s
        """, [cfg.get("medical_validity_days", 365), eid])
        row = cur.fetchone()
        if row and row[0]:
            records.append(("Медосмотр", row[0], row[1], (row[1] - ref_date).days))

        # СИЗ
        h  = cfg.get("ppe_helmet_days",  365)
        ha = cfg.get("ppe_harness_days", 365)
        g  = cfg.get("ppe_gloves_days",  180)
        cur.execute("""
            SELECT ppe_type, MAX(issue_date),
                   (MAX(issue_date) + CASE ppe_type
                       WHEN 'Каска защитная'           THEN %s * INTERVAL '1 day'
                       WHEN 'Страховочная система'      THEN %s * INTERVAL '1 day'
                       WHEN 'Перчатки диэлектрические'  THEN %s * INTERVAL '1 day'
                       WHEN 'Пояс монтажный'            THEN %s * INTERVAL '1 day'
                       ELSE 365 * INTERVAL '1 day' END)::date
            FROM ppe WHERE employee_id = %s
            GROUP BY ppe_type ORDER BY 3
        """, [h, ha, g, h, eid])
        for ptype, last_d, expiry in cur.fetchall():
            records.append((f"СИЗ — {ptype}", last_d, expiry,
                            (expiry - ref_date).days))

        result.append({"name": name, "dept": dept, "pos": pos, "records": records})
    return result


# ── Форматирование ────────────────────────────────────────────────────────────

def fmt_record(label, last_d, expiry, days):
    ic = icon(days) if days <= 30 else "🟢"
    return (
        f"{ic} {label}\n"
        f"   выдано/пройдено: {last_d.strftime('%d.%m.%Y')} "
        f"→ до {expiry.strftime('%d.%m.%Y')}\n"
        f"   {days_str(days)}"
    )


def format_report(rows, ref_date, horizon, title=""):
    """Форматировать список нарушений с заголовком."""
    if not rows:
        return None, None

    expired  = [r for r in rows if r[4] < 0]
    expiring = [r for r in rows if r[4] >= 0]

    blocks = []
    if title:
        blocks.append(title)
    if expired:
        blocks.append(f"🔴 <b>Просрочено — {len(expired)} записей</b>")
        for name, label, last_d, expiry, days in expired:
            blocks.append(
                f"🔴 {name}\n"
                f"   {label}\n"
                f"   до {expiry.strftime('%d.%m.%Y')} — {days_str(days)}"
            )
    if expiring:
        blocks.append(f"🟡 <b>Истекает в ближайшие {horizon} дн. — {len(expiring)} записей</b>")
        for name, label, last_d, expiry, days in expiring:
            blocks.append(
                f"🟡 {name}\n"
                f"   {label}\n"
                f"   до {expiry.strftime('%d.%m.%Y')} — {days_str(days)}"
            )
    return blocks, None


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def main_keyboard():
    """Главное меню кнопок."""
    return [
        [
            {"text": "📋 Сводка сегодня",     "callback_data": "today_summary"},
            {"text": "🟡 Истекают (30 дн.)",  "callback_data": "today_expiring"},
        ],
        [
            {"text": "🔴 Все нарушения",      "callback_data": "all_violations"},
            {"text": "📅 На дату...",          "callback_data": "ask_date"},
        ],
        [
            {"text": "👤 Найти сотрудника",   "callback_data": "ask_employee"},
        ],
    ]


def date_presets_keyboard():
    """Кнопки быстрого выбора дат."""
    today = date.today()
    dates = [
        ("Сегодня",    today.strftime("%Y-%m-%d")),
        ("+1 месяц",   (today + timedelta(days=30)).strftime("%Y-%m-%d")),
        ("+3 месяца",  (today + timedelta(days=90)).strftime("%Y-%m-%d")),
        ("+6 месяцев", (today + timedelta(days=180)).strftime("%Y-%m-%d")),
        ("+1 год",     (today + timedelta(days=365)).strftime("%Y-%m-%d")),
    ]
    keyboard = [[{"text": label, "callback_data": f"check_date:{d}"}] for label, d in dates]
    keyboard.append([{"text": "✏️ Ввести вручную (ДД.ММ.ГГГГ)", "callback_data": "manual_date"}])
    keyboard.append([{"text": "◀️ Назад", "callback_data": "back_main"}])
    return keyboard


# ── Обработка кнопок ──────────────────────────────────────────────────────────

# Состояния ожидания ввода от пользователя
waiting_for = {}  # chat_id -> "date" | "employee"


def handle_callback(cq):
    cq_id   = cq["id"]
    chat_id = cq["message"]["chat"]["id"]
    data    = cq.get("data", "")

    answer_callback(cq_id)

    if data == "back_main":
        send("Главное меню:", chat_id, keyboard=main_keyboard())

    elif data == "today_summary":
        cmd_summary(chat_id)

    elif data == "today_expiring":
        cmd_today(chat_id)

    elif data == "all_violations":
        cmd_all(chat_id)

    elif data == "ask_date":
        send(
            "📅 <b>Проверка на дату</b>\n\n"
            "Выберите дату или введите вручную в формате ДД.ММ.ГГГГ:",
            chat_id,
            keyboard=date_presets_keyboard()
        )

    elif data.startswith("check_date:"):
        date_str = data.split(":", 1)[1]
        try:
            ref = datetime.strptime(date_str, "%Y-%m-%d").date()
            cmd_check_date(chat_id, ref)
        except ValueError:
            send("Неверный формат даты.", chat_id)

    elif data == "manual_date":
        waiting_for[chat_id] = "date"
        send(
            "✏️ Введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
            "Например: <code>01.09.2026</code>",
            chat_id
        )

    elif data == "ask_employee":
        waiting_for[chat_id] = "employee"
        send(
            "👤 Введите <b>Фамилию Имя</b> сотрудника:\n"
            "Например: <code>Морозов Александр</code>",
            chat_id
        )


# ── Команды ───────────────────────────────────────────────────────────────────

def cmd_summary(chat_id, ref_date=None):
    """Сводка по статусам на дату."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cfg  = load_settings(cur)
        if ref_date is None:
            ref_date = date.today()
        total, bad, warn_cnt, ok = summary_counts(cur, cfg, ref_date)
        cur.close(); conn.close()

        date_label = ref_date.strftime("%d.%m.%Y")
        is_future = ref_date > date.today()
        prefix = f"📅 Прогноз на {date_label}" if is_future else f"📋 Сводка на {date_label}"

        text = (
            f"<b>{prefix}</b>\n\n"
            f"👷 Всего сотрудников: {total}\n"
            f"🔴 Не допущены:       <b>{bad}</b>\n"
            f"🟡 Требует внимания:  <b>{warn_cnt}</b>\n"
            f"🟢 Допущены:          <b>{ok}</b>"
        )
        if bad > 0 and not is_future:
            text += f"\n\n{ADMIN_TAG} требуется реакция!"

        send(text, chat_id, keyboard=main_keyboard())
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


def cmd_today(chat_id, ref_date=None):
    """Истекающие в ближайшие 30 дней."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cfg  = load_settings(cur)
        if ref_date is None:
            ref_date = date.today()
        horizon = cfg.get("expiring_notification_days", 30)
        rows = (
            [r for r in trainings_expiring(cur, cfg, horizon, ref_date) if r[4] >= 0] +
            [r for r in medical_expiring(cur, cfg, horizon, ref_date)   if r[4] >= 0] +
            [r for r in ppe_expiring(cur, cfg, horizon, ref_date)       if r[4] >= 0]
        )
        cur.close(); conn.close()

        if not rows:
            send(
                f"✅ На {ref_date.strftime('%d.%m.%Y')} — "
                f"истекающих в ближайшие {horizon} дней нет.",
                chat_id, keyboard=main_keyboard()
            )
            return

        blocks = [f"🟡 <b>Истекает в ближайшие {horizon} дн. от {ref_date.strftime('%d.%m.%Y')}</b>"]
        for name, label, last_d, expiry, days in rows:
            blocks.append(
                f"🟡 {name}\n"
                f"   {label} — до {expiry.strftime('%d.%m.%Y')} ({days_str(days)})"
            )
        send_parts(blocks, chat_id, keyboard=main_keyboard())
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


def cmd_all(chat_id, ref_date=None):
    """Все нарушения (просроченные до 90 дней от ref_date)."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cfg  = load_settings(cur)
        if ref_date is None:
            ref_date = date.today()
        rows = (
            trainings_expiring(cur, cfg, 90, ref_date) +
            medical_expiring(cur, cfg, 90, ref_date) +
            ppe_expiring(cur, cfg, 90, ref_date)
        )
        cur.close(); conn.close()

        if not rows:
            send(
                f"✅ На {ref_date.strftime('%d.%m.%Y')} — нарушений нет.",
                chat_id, keyboard=main_keyboard()
            )
            return

        expired  = [r for r in rows if r[4] < 0]
        expiring = [r for r in rows if r[4] >= 0]
        blocks = []
        if expired:
            blocks.append(f"🔴 <b>Просрочено ({len(expired)})</b>")
            for name, label, _, expiry, days in expired:
                blocks.append(f"🔴 {name} — {label} ({days_str(days)})")
        if expiring:
            blocks.append(f"🟡 <b>Истекает ({len(expiring)})</b>")
            for name, label, _, expiry, days in expiring:
                blocks.append(f"🟡 {name} — {label}, до {expiry.strftime('%d.%m.%Y')} ({days_str(days)})")
        send_parts(blocks, chat_id, keyboard=main_keyboard())
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


def cmd_check_date(chat_id, ref_date):
    """Полный отчёт на конкретную дату."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cfg  = load_settings(cur)
        total, bad, warn_cnt, ok = summary_counts(cur, cfg, ref_date)

        horizon = cfg.get("expiring_notification_days", 30)
        rows = (
            trainings_expiring(cur, cfg, horizon, ref_date) +
            medical_expiring(cur, cfg, horizon, ref_date) +
            ppe_expiring(cur, cfg, horizon, ref_date)
        )
        cur.close(); conn.close()

        is_future = ref_date > date.today()
        date_label = ref_date.strftime("%d.%m.%Y")
        emoji = "🔮" if is_future else "📅"

        # Сводка
        summary = (
            f"{emoji} <b>{'Прогноз' if is_future else 'Состояние'} на {date_label}</b>\n\n"
            f"👷 Всего: {total}\n"
            f"🔴 Не допущены:      <b>{bad}</b>\n"
            f"🟡 Требует внимания: <b>{warn_cnt}</b>\n"
            f"🟢 Допущены:         <b>{ok}</b>"
        )
        send(summary, chat_id)

        if not rows:
            send("✅ Нарушений и истекающих документов нет.", chat_id, keyboard=main_keyboard())
            return

        expired  = [r for r in rows if r[4] < 0]
        expiring = [r for r in rows if r[4] >= 0]
        blocks = []
        if expired:
            blocks.append(f"🔴 <b>Просрочено — {len(expired)}</b>")
            for name, label, last_d, expiry, days in expired:
                blocks.append(f"🔴 {name}\n   {label}\n   до {expiry.strftime('%d.%m.%Y')} — {days_str(days)}")
        if expiring:
            blocks.append(f"🟡 <b>Истекает в {horizon} дн. — {len(expiring)}</b>")
            for name, label, last_d, expiry, days in expiring:
                blocks.append(f"🟡 {name}\n   {label}\n   до {expiry.strftime('%d.%m.%Y')} — {days_str(days)}")
        send_parts(blocks, chat_id, keyboard=main_keyboard())
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


def cmd_status(chat_id, last_name, first_name, ref_date=None):
    """Карточка сотрудника на дату."""
    if ref_date is None:
        ref_date = date.today()
    try:
        conn = get_conn(); cur = conn.cursor()
        cfg  = load_settings(cur)
        data = employee_card(cur, cfg, last_name, first_name, ref_date)
        cur.close(); conn.close()

        if not data:
            send(f"Сотрудник {last_name} {first_name} не найден.", chat_id, keyboard=main_keyboard())
            return

        date_label = ref_date.strftime("%d.%m.%Y")
        for emp in data:
            records = emp["records"]
            if not records:
                send(f"{emp['name']} — нет данных.", chat_id)
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
                f"Статус на {date_label}: {overall}"
            ]
            for label, last_d, expiry, days in records:
                blocks.append(fmt_record(label, last_d, expiry, days))
            send_parts(blocks, chat_id, keyboard=main_keyboard())
    except Exception as e:
        send(f"Ошибка: {e}", chat_id)


# ── Ежедневный отчёт ──────────────────────────────────────────────────────────

def daily_report():
    log.info("ежедневный отчёт")
    try:
        conn = get_conn(); cur = conn.cursor()
        cfg  = load_settings(cur)
        today = date.today()
        total, bad, warn_cnt, ok = summary_counts(cur, cfg, today)

        text = (
            f"<b>Охрана труда — {today.strftime('%d.%m.%Y')}</b>\n\n"
            f"👷 Всего сотрудников: {total}\n"
            f"🔴 Не допущены:       {bad}\n"
            f"🟡 Требует внимания:  {warn_cnt}\n"
            f"🟢 Допущены:          {ok}"
        )
        if bad > 0:
            text += f"\n\n{ADMIN_TAG} есть недопущенные сотрудники"
        send(text, keyboard=main_keyboard())

        if bad == 0 and warn_cnt == 0:
            cur.close(); conn.close()
            return

        horizon = cfg.get("expiring_notification_days", 30)
        rows = (
            trainings_expiring(cur, cfg, horizon, today) +
            medical_expiring(cur, cfg, horizon, today) +
            ppe_expiring(cur, cfg, horizon, today)
        )
        cur.close(); conn.close()

        if not rows:
            return

        expired  = [r for r in rows if r[4] < 0]
        expiring = [r for r in rows if r[4] >= 0]
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


# ── Обработка текстовых сообщений ────────────────────────────────────────────

def handle_text(text, chat_id):
    text = text.strip()
    low  = text.lower()

    # Проверяем — ждём ли ввода от этого чата
    state = waiting_for.get(chat_id)

    if state == "date":
        # Ожидаем дату
        waiting_for.pop(chat_id, None)
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                ref = datetime.strptime(text, fmt).date()
                cmd_check_date(chat_id, ref)
                return
            except ValueError:
                continue
        send(
            "Не могу распознать дату. Введите в формате <code>ДД.ММ.ГГГГ</code>, "
            "например: <code>01.09.2026</code>",
            chat_id
        )

    elif state == "employee":
        # Ожидаем фамилию имя
        waiting_for.pop(chat_id, None)
        words = text.split()
        if len(words) < 2:
            send("Укажите Фамилию и Имя через пробел.", chat_id)
            return
        cmd_status(chat_id, words[0], words[1])

    elif low.startswith("/start") or low.startswith("/help") or low == "/menu":
        send(
            "<b>Охрана труда — команды</b>\n\n"
            "/menu — главное меню с кнопками\n"
            "/today — истекающие в 30 дней\n"
            "/all — все нарушения\n"
            "/status Фамилия Имя — карточка сотрудника\n"
            "/check ДД.ММ.ГГГГ — состояние на дату\n\n"
            f"Ежедневный отчёт в {DAILY_TIME}",
            chat_id, keyboard=main_keyboard()
        )

    elif low.startswith("/menu"):
        send("Главное меню:", chat_id, keyboard=main_keyboard())

    elif low.startswith("/today"):
        cmd_today(chat_id)

    elif low.startswith("/all"):
        cmd_all(chat_id)

    elif low.startswith("/check"):
        # /check 01.09.2026
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            waiting_for[chat_id] = "date"
            send(
                "Введите дату в формате <code>ДД.ММ.ГГГГ</code>:",
                chat_id, keyboard=date_presets_keyboard()
            )
            return
        date_str = parts[1].strip()
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                ref = datetime.strptime(date_str, fmt).date()
                cmd_check_date(chat_id, ref)
                return
            except ValueError:
                continue
        send("Формат: /check ДД.ММ.ГГГГ — например /check 01.09.2026", chat_id)

    elif low.startswith("/status"):
        after = text[len("/status"):].strip()
        words = after.split()
        if len(words) < 2:
            waiting_for[chat_id] = "employee"
            send("Введите Фамилию и Имя сотрудника:", chat_id)
            return
        cmd_status(chat_id, words[0], words[1])

    else:
        send("Неизвестная команда. /help — список команд.", chat_id, keyboard=main_keyboard())


# ── Polling ───────────────────────────────────────────────────────────────────

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


def run_scheduler():
    schedule.every().day.at(DAILY_TIME).do(daily_report)
    log.info("планировщик запущен, отчёт в %s", DAILY_TIME)
    while True:
        schedule.run_pending()
        time.sleep(30)


def main():
    log.info("бот запущен")
    threading.Thread(target=run_scheduler, daemon=True).start()
    send(
        f"✅ Бот запущен. Ежедневный отчёт в {DAILY_TIME}.",
        keyboard=main_keyboard()
    )

    offset = 0
    while True:
        updates = get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1

            # Inline кнопки
            if "callback_query" in upd:
                handle_callback(upd["callback_query"])
                continue

            # Обычные сообщения
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            text    = msg.get("text", "")
            chat_id = msg["chat"]["id"]
            if text:
                log.info("сообщение от %s: %s", chat_id, text[:50])
                handle_text(text, chat_id)

        time.sleep(1)


if __name__ == "__main__":
    main()
