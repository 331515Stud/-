import html
import os
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

MASSAGE_GROUPS = {
    "Основные услуги": [
        "Массаж головы",
        "Массаж спины",
        "Массаж ног",
        "Массаж стоп",
        "Массаж груди",
        "Массаж плеч",
        "Массаж рук",
        "Комплексный массаж всего тела",
    ],
    "Дополнительные услуги": [
        "Интимный массаж",
        "Эротический массаж",
        "Массаж при свечах",
        "Массаж при согласовании (особый)",
    ],
    "Прочее": [
        "Другое",
    ],
}

MASSAGE_TYPES = [s for services in MASSAGE_GROUPS.values() for s in services]

TIME_SLOTS = [
    "09:00", "10:00", "11:00", "12:00", "13:00", "14:00",
    "15:00", "16:00", "17:00", "18:00", "19:00", "20:00",
]

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

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
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                service TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )


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


def tg_send(chat_id, text):
    tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def tg_listener():
    """Фоновый поток: если ADMIN_CHAT_ID не задан, берём чат,
    в котором кто-то написал боту /start, и шлём уведомления туда."""
    offset = 0
    while True:
        data = tg_api("getUpdates", {
            "offset": offset,
            "timeout": 30,
            "allowed_updates": ["message"],
        })
        if data:
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat_id = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()
                if chat_id and text == "/start":
                    if not admin_chat_id():
                        set_setting("admin_chat_id", str(chat_id))
                    tg_send(chat_id, "Привет! Бот работает ✅\nУведомления о новых записях на массаж будут приходить в этот чат.")
        time.sleep(1)


def notify_about_booking(name, date_str, time_str, service):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    pretty_date = f"{date_obj.day:02d}.{date_obj.month:02d}.{date_obj.year} ({WEEKDAYS[date_obj.weekday()]})"
    text = (
        "📅 <b>Новая запись на массаж!</b>\n\n"
        f"👤 Имя: <b>{html.escape(name)}</b>\n"
        f"📆 Дата: <b>{pretty_date}</b>\n"
        f"⏰ Время: <b>{time_str}</b>\n"
        f"💆‍♀️ Тип: <b>{html.escape(service)}</b>"
    )
    chat_id = admin_chat_id()
    if chat_id:
        tg_send(chat_id, text)


# ---------- Маршруты ----------

@app.route("/")
def index():
    return render_template(
        "index.html",
        massage_groups=MASSAGE_GROUPS,
        time_slots=TIME_SLOTS,
        today=date.today().isoformat(),
    )


@app.route("/book", methods=["POST"])
def book():
    name = request.form.get("name", "").strip()
    date_str = request.form.get("date", "").strip()
    time_str = request.form.get("time", "").strip()
    service = request.form.get("service", "").strip()

    errors = []
    if not name or len(name) > 100:
        errors.append("Пожалуйста, укажите ваше имя.")
    if service not in MASSAGE_TYPES:
        errors.append("Пожалуйста, выберите тип массажа.")
    if time_str not in TIME_SLOTS:
        errors.append("Пожалуйста, выберите время.")
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
    with get_db() as conn:
        conn.execute(
            "INSERT INTO bookings (name, date, time, service, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, date_str, time_str, service, created_at),
        )

    notify_about_booking(name, date_str, time_str, service)
    return redirect(url_for("success", name=name, date=date_str, time=time_str, service=service))


@app.route("/success")
def success():
    name = request.args.get("name", "")
    date_str = request.args.get("date", "")
    time_str = request.args.get("time", "")
    service = request.args.get("service", "")
    if date_str:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        pretty_date = f"{date_obj.day:02d}.{date_obj.month:02d}.{date_obj.year}"
    else:
        pretty_date = ""
    return render_template("success.html", name=name, date=pretty_date, time=time_str, service=service)


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
        date_obj = datetime.strptime(row["date"], "%Y-%m-%d").date()
        bookings.append({
            "id": row["id"],
            "name": row["name"],
            "date": f"{date_obj.day:02d}.{date_obj.month:02d}.{date_obj.year}",
            "date_iso": row["date"],
            "weekday": WEEKDAYS[date_obj.weekday()],
            "time": row["time"],
            "service": row["service"],
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


# ---------- Запуск ----------

if __name__ == "__main__":
    init_db()
    if BOT_TOKEN:
        threading.Thread(target=tg_listener, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
