import html
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
import requests

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bookings.db")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123").strip()

MAIN_SERVICES = [
    "Массаж головы",
    "Массаж спины",
    "Массаж ног",
    "Массаж стоп",
    "Массаж груди",
    "Массаж плеч",
    "Массаж рук",
    "Комплексный массаж всего тела",
]

EXTRA_SERVICES = [
    "Интимный массаж",
    "Массаж при свечах",
    "Пенная ванна",
    "Чайная церемония",
    "Ароматерапия (масла и свечи)",
    "Тёплые массажные масла",
    "Свой плейлист (музыка на ваш вкус)",
    "Горячие полотенца",
    "Увлажняющая маска для лица",
    "Травяной чай с угощениями",
]

PLAYLIST_EXTRA = "Свой плейлист (музыка на ваш вкус)"

TIME_SLOTS = [
    "09:00", "10:00", "11:00", "12:00", "13:00", "14:00",
    "15:00", "16:00", "17:00", "18:00", "19:00", "20:00",
]

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

STATUS_LABELS = {
    "new": "🆕 Ожидает подтверждения",
    "confirmed": "✅ Подтверждена",
    "cancelled": "❌ Отменена",
}

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-in-production")


# ---------- База данных ----------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                service TEXT NOT NULL,
                extras TEXT NOT NULL DEFAULT '',
                playlist TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                tgm_chat_id TEXT,
                master_chat_id TEXT,
                master_msg_id INTEGER,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        for column, decl in {
            "extras": "TEXT NOT NULL DEFAULT ''",
            "notes": "TEXT NOT NULL DEFAULT ''",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "playlist": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'new'",
            "tgm_chat_id": "TEXT",
            "master_chat_id": "TEXT",
            "master_msg_id": "INTEGER",
        }.items():
            ensure_column(conn, "bookings", column, decl)


def ensure_column(conn, table, column, decl):
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def get_setting(key):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_booking(booking_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


# ---------- Утилиты ----------

def normalize_phone(s):
    digits = re.sub(r"\D", "", s or "")
    return digits if len(digits) >= 7 else ""


def pretty_date(date_str, with_weekday=False):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    if with_weekday:
        return f"{d.day:02d}.{d.month:02d}.{d.year} ({WEEKDAYS[d.weekday()]})"
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def build_booking_text(row):
    status = row["status"]
    header = "📅 <b>Новая запись на массаж!</b>" if status == "new" else "📅 <b>Запись на массаж</b>"
    lines = [
        header,
        "",
        f"👤 Имя: <b>{html.escape(row['name'])}</b>",
        f"📱 Телефон: <b>{html.escape(row['phone'])}</b>",
        f"📆 Дата: <b>{pretty_date(row['date'], with_weekday=True)}</b>",
        f"⏰ Время: <b>{row['time']}</b>",
        f"💆‍♀️ Массаж: <b>{html.escape(row['service'])}</b>",
    ]
    if row["extras"]:
        lines.append(f"✨ Дополнительно: <b>{html.escape(row['extras'])}</b>")
    if row["playlist"]:
        lines.append(f"🎵 Плейлист: {html.escape(row['playlist'])}")
    if row["notes"]:
        lines.append(f"💬 Пожелания: <b>{html.escape(row['notes'])}</b>")
    lines.append("")
    lines.append(f"Статус: {STATUS_LABELS.get(status, status)}")
    if row["tgm_chat_id"]:
        lines.append("🔗 Клиент в Telegram: ✅ подключён")
    else:
        lines.append("🔗 Клиент в Telegram: не подключён (нет телефона в боте)")
    return "\n".join(lines)


# ---------- Telegram ----------

def admin_chat_id():
    if ADMIN_CHAT_ID:
        return ADMIN_CHAT_ID
    return get_setting("admin_chat_id")


def tg_api(method, payload):
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        return requests.post(url, json=payload, timeout=10).json()
    except requests.RequestException:
        return None


def tg_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)


_BOT_USERNAME = None


def bot_username():
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    data = tg_api("getMe", {})
    if data and data.get("ok"):
        _BOT_USERNAME = data.get("result", {}).get("username", "")
    return _BOT_USERNAME


def notify_booking(booking_id):
    row = get_booking(booking_id)
    chat_id = admin_chat_id()
    if not row or not chat_id:
        return
    markup = {"inline_keyboard": [[
        {"text": "✅ Подтвердить", "callback_data": f"confirm:{booking_id}"},
        {"text": "❌ Отменить", "callback_data": f"cancel:{booking_id}"},
    ]]}
    resp = tg_send(chat_id, build_booking_text(row), markup)
    if resp and resp.get("ok"):
        msg = resp["result"]
        with get_db() as conn:
            conn.execute(
                "UPDATE bookings SET master_chat_id = ?, master_msg_id = ? WHERE id = ?",
                (str(msg["chat"]["id"]), msg["message_id"], booking_id),
            )


def apply_confirmation(booking_id, new_status):
    with get_db() as conn:
        conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (new_status, booking_id))
    row = get_booking(booking_id)
    if not row:
        return

    if row["master_chat_id"] and row["master_msg_id"]:
        tg_api("editMessageText", {
            "chat_id": row["master_chat_id"],
            "message_id": row["master_msg_id"],
            "text": build_booking_text(row),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},
        })

    client_chat = row["tgm_chat_id"]
    if not client_chat:
        return
    when = f"📆 {pretty_date(row['date'])} в {row['time']}"
    if new_status == "confirmed":
        msg = (
            "✅ <b>Ваша запись подтверждена!</b>\n\n"
            f"{when}\n"
            f"💆‍♀️ Массаж: <b>{html.escape(row['service'])}</b>\n\n"
            "Ждём вас! 💕"
        )
    else:
        msg = (
            "❌ <b>Запись отменена</b>\n\n"
            f"{when}\n"
            f"💆‍♀️ Массаж: <b>{html.escape(row['service'])}</b>\n\n"
            "Попробуйте выбрать другое время на сайте."
        )
    tg_send(client_chat, msg)


def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return

    if text.startswith("/start-master"):
        set_setting("admin_chat_id", str(chat_id))
        tg_send(chat_id, "Вы зарегистрированы как мастер ✅\nСюда будут приходить записи на массаж и кнопки «Подтвердить / Отменить».")
        return

    if text.startswith("/start"):
        payload = text[len("/start"):].strip()
        if payload.startswith("confirm_"):
            bid = payload[len("confirm_"):]
            if bid.isdigit():
                with get_db() as conn:
                    conn.execute(
                        "UPDATE bookings SET tgm_chat_id = ? WHERE id = ?",
                        (chat_id, int(bid)),
                    )
                tg_send(chat_id, "Отлично! 💌 Когда мастер подтвердит вашу запись, я напишу вам сюда.")
            else:
                tg_send(chat_id, "Ссылка не распознана. Попробуйте ещё раз со страницы записи.")
            return
        tg_send(chat_id, "Привет! 👋\nВы записались на массаж? Нажмите кнопку «Получать подтверждение» на странице записи — я напишу вам сюда, когда мастер подтвердит запись.")
        return

    if text.startswith("/status"):
        rest = text[len("/status"):].strip()
        phone = normalize_phone(rest)
        if not phone:
            tg_send(chat_id, "Команда: /status <номер телефона>")
            return
        reply_status(phone, chat_id)
        return

    phone = normalize_phone(text)
    if phone:
        link_phone_to_chat(phone, chat_id)


def link_phone_to_chat(phone, chat_id):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM bookings").fetchall()
    matches = [r for r in rows if normalize_phone(r["phone"]) == phone]
    if not matches:
        tg_send(chat_id, "Не нашёл запись с таким номером 🤔\nПроверьте, что номер совпадает с указанным при записи на сайте.")
        return
    ids = [r["id"] for r in matches]
    placeholders = ",".join("?" * len(ids))
    with get_db() as conn:
        conn.execute(
            f"UPDATE bookings SET tgm_chat_id = ? WHERE id IN ({placeholders})",
            [chat_id] + ids,
        )
    confirmed = any(r["status"] == "confirmed" for r in matches)
    cancelled = any(r["status"] == "cancelled" for r in matches)
    if confirmed:
        tg_send(chat_id, "✅ Ваша запись уже подтверждена мастером! Ждём вас 💕")
    elif cancelled:
        tg_send(chat_id, "❌ Ваша запись была отменена. Запишитесь на другое время на сайте.")
    else:
        tg_send(chat_id, "📲 Номер привязан! Я напишу вам подтверждение в этот чат, когда мастер подтвердит запись 💌")


def reply_status(phone, chat_id):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM bookings").fetchall()
    matches = [r for r in rows if normalize_phone(r["phone"]) == phone]
    if not matches:
        tg_send(chat_id, "Не нашёл запись с таким номером 🤔")
        return
    lines = ["Ваши записи:"]
    for r in matches:
        lines.append(f"• {pretty_date(r['date'])} в {r['time']} — {STATUS_LABELS.get(r['status'], r['status'])}")
    tg_send(chat_id, "\n".join(lines))


def handle_callback(cp):
    data = cp.get("data", "")
    cid = cp.get("id")
    if cid:
        tg_api("answerCallbackQuery", {"callback_query_id": cid})
    parts = data.split(":", 1)
    if len(parts) != 2:
        return
    action, bid_str = parts
    if action not in ("confirm", "cancel") or not bid_str.isdigit():
        return
    chat_id = cp.get("message", {}).get("chat", {}).get("id")
    if chat_id is None or str(chat_id) != str(admin_chat_id()):
        return
    apply_confirmation(int(bid_str), "confirmed" if action == "confirm" else "cancelled")


def tg_listener():
    """Фоновый поток: обрабатывает команды клиентов и нажатия кнопок мастера."""
    offset = 0
    while True:
        data = tg_api("getUpdates", {
            "offset": offset,
            "timeout": 30,
            "allowed_updates": ["message", "callback_query"],
        })
        if data:
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        handle_callback(update["callback_query"])
                    elif "message" in update:
                        handle_message(update["message"])
                except Exception:
                    pass
        time.sleep(1)


# ---------- Маршруты ----------

@app.route("/")
def index():
    return render_template(
        "index.html",
        main_services=MAIN_SERVICES,
        extra_services=EXTRA_SERVICES,
        playlist_extra=PLAYLIST_EXTRA,
        time_slots=TIME_SLOTS,
        today=date.today().isoformat(),
    )


@app.route("/book", methods=["POST"])
def book():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    date_str = request.form.get("date", "").strip()
    time_str = request.form.get("time", "").strip()
    service = request.form.get("service", "").strip()
    extras = request.form.getlist("extras")
    playlist = request.form.get("playlist", "").strip()
    notes = request.form.get("notes", "").strip()

    errors = []
    if not name or len(name) > 100:
        errors.append("Пожалуйста, укажите ваше имя.")
    if not normalize_phone(phone):
        errors.append("Укажите корректный номер телефона — по нему мастер подтвердит запись.")
    if service not in MAIN_SERVICES:
        errors.append("Пожалуйста, выберите основной массаж.")
    for extra in extras:
        if extra not in EXTRA_SERVICES:
            errors.append("Некорректная дополнительная услуга.")
    if len(extras) > len(EXTRA_SERVICES):
        errors.append("Слишком много дополнительных услуг.")
    if playlist and PLAYLIST_EXTRA not in extras:
        errors.append("Плейлист указывается только вместе с услугой «Свой плейлист».")
    if len(playlist) > 1000:
        errors.append("Плейлист слишком длинный (максимум 1000 символов).")
    if len(notes) > 500:
        errors.append("Пожелания слишком длинные (максимум 500 символов).")
    if date_str:
        try:
            selected = datetime.strptime(date_str, "%Y-%m-%d").date()
            if selected < date.today():
                errors.append("Нельзя записаться на прошедшую дату.")
        except ValueError:
            errors.append("Некорректная дата.")
    else:
        errors.append("Пожалуйста, выберите дату.")

    if not errors:
        with get_db() as conn:
            taken = conn.execute(
                "SELECT id FROM bookings WHERE date = ? AND time = ?",
                (date_str, time_str),
            ).fetchone()
            if taken:
                errors.append("Это время уже занято. Пожалуйста, выберите другое.")

    if errors:
        for err in errors:
            flash(err, "error")
        return redirect(url_for("index"))

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extras_csv = ", ".join(extras)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO bookings (name, phone, date, time, service, extras, playlist, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, phone, date_str, time_str, service, extras_csv, playlist, notes, created_at),
        )
        booking_id = cur.lastrowid

    notify_booking(booking_id)
    return redirect(
        url_for("success", booking_id=booking_id, name=name, phone=phone, date=date_str,
                time=time_str, service=service, extras=extras_csv, playlist=playlist, notes=notes)
    )


@app.route("/success")
def success():
    booking_id = request.args.get("booking_id", "")
    name = request.args.get("name", "")
    phone = request.args.get("phone", "")
    date_str = request.args.get("date", "")
    time_str = request.args.get("time", "")
    service = request.args.get("service", "")
    extras = request.args.get("extras", "")
    playlist = request.args.get("playlist", "")
    notes = request.args.get("notes", "")
    if date_str:
        pretty = pretty_date(date_str)
    else:
        pretty = ""
    return render_template(
        "success.html",
        booking_id=booking_id,
        bot_username=bot_username(),
        name=name, phone=phone, date=pretty, time=time_str,
        service=service, extras=extras, playlist=playlist, notes=notes,
    )


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Неверный пароль", "error")

    if not session.get("admin"):
        return render_template("admin_login.html")

    filter_date = request.args.get("date", "").strip()
    with get_db() as conn:
        if filter_date:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE date = ? ORDER BY time",
                (filter_date,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bookings ORDER BY date, time"
            ).fetchall()

    bookings = []
    for row in rows:
        bookings.append({
            "id": row["id"],
            "name": row["name"],
            "phone": row["phone"],
            "date": pretty_date(row["date"]),
            "date_iso": row["date"],
            "weekday": WEEKDAYS[datetime.strptime(row["date"], "%Y-%m-%d").weekday()],
            "time": row["time"],
            "service": row["service"],
            "extras": row["extras"],
            "playlist": row["playlist"],
            "notes": row["notes"],
            "status": row["status"],
            "status_label": STATUS_LABELS.get(row["status"], row["status"]),
            "client_connected": bool(row["tgm_chat_id"]),
            "created_at": row["created_at"],
        })

    today = date.today().isoformat()
    return render_template("admin.html", bookings=bookings, filter_date=filter_date, today=today)


@app.route("/admin/delete/<int:booking_id>", methods=["POST"])
def delete_booking(booking_id):
    if not session.get("admin"):
        return redirect(url_for("admin"))
    with get_db() as conn:
        conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    flash("Запись удалена", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/confirm/<int:booking_id>", methods=["POST"])
def admin_confirm(booking_id):
    if not session.get("admin"):
        return redirect(url_for("admin"))
    action = request.form.get("action")
    if action == "confirmed":
        apply_confirmation(booking_id, "confirmed")
        flash("Запись подтверждена", "ok")
    elif action == "cancelled":
        apply_confirmation(booking_id, "cancelled")
        flash("Запись отменена", "ok")
    return redirect(url_for("admin"))


# ---------- Запуск ----------

if __name__ == "__main__":
    init_db()
    if BOT_TOKEN:
        threading.Thread(target=tg_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
