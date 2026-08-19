import os
import uuid
import sqlite3
import threading
import time
import requests
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

DEFAULTS = {
    "name": "كريم ابراهيم الناعوق",
    "phone": "0597511457",
    "whatsapp": "+970597511457",
    "telegram_username": "@kareem_alnouq",
    "sell_rate": "2.80",
    "buy_rate": "3.00",
    "wallet_address": "TBUxYF7vAqmCL2NBbcaXWXTpRvBaKRQHRy",
    "recipient_name": "كريم ابراهيم الناعوق",
    "recipient_phone": "0597511457",
    "service_providers": "جوال باي",
}

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT UNIQUE NOT NULL,
            order_type TEXT NOT NULL,
            usdt_amount REAL NOT NULL,
            fiat_amount REAL NOT NULL,
            customer_name TEXT,
            customer_phone TEXT,
            provider TEXT,
            wallet_address TEXT,
            tx_image TEXT,
            status TEXT NOT NULL DEFAULT 'قيد المراجعة',
            created_at TEXT NOT NULL,
            telegram_message_id INTEGER
        )
    """)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "telegram_message_id" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN telegram_message_id INTEGER")
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()

def settings():
    conn = db()
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def save_settings(data):
    conn = db()
    for key in DEFAULTS:
        if key in data:
            conn.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(data[key]).strip()))
    conn.commit()
    conn.close()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _telegram_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "8698042207:AAGsjki-aYOJKwFqYghKf9_NwZUG2PYAGm8").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "7759771359").strip()
    discovered = ""
    try:
        with open(os.path.join(BASE_DIR, ".telegram_chat_id"), "r", encoding="utf-8") as f:
            discovered = f.read().strip()
    except OSError:
        pass
    return token, chat_id, discovered

def _telegram_get_updates(token, offset=None, timeout=0):
    params = {"timeout": timeout, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params=params,
        timeout=max(10, timeout + 10)
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data.get("result", [])

def _telegram_send_message(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": str(chat_id), "text": text},
        timeout=15
    )
    try:
        data = r.json()
    except ValueError:
        data = {"ok": False, "description": r.text[:300]}
    if not r.ok or not data.get("ok"):
        return False, data, None
    return True, data, data.get("result", {}).get("message_id")

def send_telegram(order, s):
    token, configured_chat_id, discovered_chat_id = _telegram_config()
    if not token:
        print("[Telegram] ERROR: TELEGRAM_BOT_TOKEN is missing.")
        return False, "Telegram bot token is missing.", None

    text = (
        f"🧾 طلب جديد #{order['order_code']}\n\n"
        f"النوع: {order['order_type']}\n"
        f"الكمية: {order['usdt_amount']:.2f} USDT\n"
        f"القيمة: {order['fiat_amount']:.2f} شيكل\n"
        f"الاسم: {order['customer_name'] or '-'}\n"
        f"الجوال: {order['customer_phone'] or '-'}\n"
        f"مزود الخدمة: {order['provider'] or '-'}\n"
        f"المحفظة: {order['wallet_address'] or '-'}\n"
        f"الوقت: {order['created_at']}\n\n"
        f"✍️ للإنهاء: اعمل Reply على هذه الرسالة واكتب: تم"
    )

    # Prefer the chat ID discovered by the listener. The configured value is
    # kept as a fallback so an old/wrong value cannot prevent recovery.
    candidates = []
    if discovered_chat_id:
        candidates.append(str(discovered_chat_id))
    if configured_chat_id and str(configured_chat_id) not in candidates:
        candidates.append(str(configured_chat_id))

    try:
        if not candidates:
            print("[Telegram] ERROR: No chat ID available. Send /start to the bot once, then submit the order.")
            return False, "No Telegram chat ID available.", None

        last_error = None
        for chat_id in reversed(candidates):
            ok, data, tg_message_id = _telegram_send_message(token, chat_id, text)
            if ok:
                print(f"[Telegram] Order message sent to chat_id={chat_id}, message_id={tg_message_id}")
                # If auto-discovery found the working chat, tell the user what to
                # put in TELEGRAM_CHAT_ID for future runs without exposing a token.
                if str(chat_id) != str(configured_chat_id):
                    print(f"[Telegram] Auto-discovered working chat ID: {chat_id}")
                if order["tx_image"]:
                    path = os.path.join(UPLOAD_DIR, order["tx_image"])
                    if os.path.exists(path):
                        with open(path, "rb") as f:
                            photo_r = requests.post(
                                f"https://api.telegram.org/bot{token}/sendPhoto",
                                data={"chat_id": chat_id, "caption": f"صورة إثبات الطلب #{order['order_code']}"},
                                files={"photo": f},
                                timeout=20
                            )
                        if not photo_r.ok:
                            print(f"[Telegram] sendPhoto error: {photo_r.text[:300]}")
                return True, "sent", tg_message_id
            last_error = data
            print(f"[Telegram] sendMessage failed for chat_id={chat_id}: {data}")

        return False, str(last_error), None
    except requests.RequestException as e:
        print(f"[Telegram] Connection error while sending: {e}")
        return False, str(e), None
    except Exception as e:
        print(f"[Telegram] Send error: {e}")
        return False, str(e), None

def telegram_worker():
    """Listen for owner replies and mark the exact replied-to order as completed."""
    offset = 0
    token, configured_chat_id, discovered_chat_id = _telegram_config()

    if not token:
        print("[Telegram] Bot token is not configured.")
        return

    print("[Telegram] Reply listener started.")
    if not discovered_chat_id:
        print("[Telegram] Send /start to your bot once so the listener can discover your chat ID.")

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={
                    "timeout": 20,
                    "offset": offset,
                    "allowed_updates": ["message"]
                },
                timeout=30
            )

            if r.status_code == 409:
                print("[Telegram] Conflict: another process is reading this bot. Close other copies of the site.")
                time.sleep(5)
                continue

            if not r.ok:
                print(f"[Telegram] getUpdates HTTP {r.status_code}: {r.text[:300]}")
                time.sleep(5)
                continue

            payload = r.json()
            if not payload.get("ok"):
                print(f"[Telegram] API error: {payload}")
                time.sleep(5)
                continue

            for upd in payload.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                incoming_chat_id = str(msg.get("chat", {}).get("id", ""))

                # If the configured ID is wrong/empty, keep the latest real chat
                # ID in a local file so send_telegram can use it on the next order.
                if incoming_chat_id:
                    try:
                        with open(os.path.join(BASE_DIR, ".telegram_chat_id"), "w", encoding="utf-8") as f:
                            f.write(incoming_chat_id)
                    except OSError:
                        pass

                # A reply to one of our stored order messages is authoritative.
                # This also lets the bot recover if TELEGRAM_CHAT_ID was entered incorrectly.
                body = (msg.get("text") or "").strip()
                if body != "تم":
                    continue

                reply = msg.get("reply_to_message") or {}
                replied_message_id = reply.get("message_id")

                if not replied_message_id:
                    print("[Telegram] Received 'تم' but it was not a Reply to an order message.")
                    continue

                conn = db()
                order = conn.execute(
                    "SELECT id, order_code, status FROM orders WHERE telegram_message_id=?",
                    (replied_message_id,)
                ).fetchone()

                if order:
                    conn.execute(
                        "UPDATE orders SET status='تم التحويل' WHERE id=?",
                        (order["id"],)
                    )
                    conn.commit()
                    print(f"[Telegram] Order #{order['order_code']} marked as تم التحويل.")
                else:
                    print(f"[Telegram] Reply message_id={replied_message_id} did not match an order.")
                conn.close()

        except requests.RequestException as e:
            print(f"[Telegram] Connection error: {e}")
            time.sleep(5)
        except Exception as e:
            print(f"[Telegram] Listener error: {e}")
            time.sleep(5)


@app.context_processor
def inject_globals():
    return {"site": settings()}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/sell", methods=["GET", "POST"])
def sell():
    s = settings()
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", "0"))
        except ValueError:
            amount = 0
        if amount <= 0:
            flash("أدخل كمية صحيحة من USDT.", "error")
            return redirect(url_for("sell"))
        rate = float(s["sell_rate"])
        fiat = amount * rate
        return render_template("sell.html", step=2, amount=amount, fiat=fiat, providers=s["service_providers"].split("|"))
    return render_template("sell.html", step=1)

@app.route("/sell/submit", methods=["POST"])
def sell_submit():
    s = settings()
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    if amount <= 0:
        flash("المبلغ غير صحيح.", "error")
        return redirect(url_for("sell"))

    rate = float(s["sell_rate"])
    fiat = amount * rate
    image = request.files.get("tx_image")
    filename = None

    if image and image.filename:
        if not allowed_file(image.filename):
            flash("صيغة الصورة غير مدعومة. استخدم PNG أو JPG أو WEBP.", "error")
            return redirect(url_for("sell"))
        ext = image.filename.rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        image.save(os.path.join(UPLOAD_DIR, filename))

    code = uuid.uuid4().hex[:8].upper()
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db()
    conn.execute("""
        INSERT INTO orders
        (order_code,order_type,usdt_amount,fiat_amount,customer_name,customer_phone,
         provider,wallet_address,tx_image,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        code, "بيع USDT", amount, fiat,
        request.form.get("customer_name","").strip(),
        request.form.get("customer_phone","").strip(),
        request.form.get("provider","").strip(),
        "", filename, "قيد المراجعة", created
    ))
    conn.commit()
    order = conn.execute("SELECT * FROM orders WHERE order_code=?", (code,)).fetchone()
    conn.close()

    _, _, tg_message_id = send_telegram(order, s)
    if tg_message_id:
        conn = db()
        conn.execute("UPDATE orders SET telegram_message_id=? WHERE order_code=?", (tg_message_id, code))
        conn.commit()
        conn.close()
    wa_text = f"مرحباً، أرسلت إشعار حوالة طلب بيع USDT رقم #{code}. المبلغ: {fiat:.2f} شيكل."
    session["last_order_code"] = code
    return redirect(url_for("order_result", order_code=code))

@app.route("/buy", methods=["GET", "POST"])
def buy():
    s = settings()
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", "0"))
        except ValueError:
            amount = 0
        if amount <= 0:
            flash("أدخل كمية صحيحة من USDT.", "error")
            return redirect(url_for("buy"))
        rate = float(s["buy_rate"])
        fiat = amount * rate
        return render_template("buy.html", step=2, amount=amount, fiat=fiat, providers=s["service_providers"].split("|"))
    return render_template("buy.html", step=1)

@app.route("/buy/submit", methods=["POST"])
def buy_submit():
    s = settings()
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    if amount <= 0:
        flash("المبلغ غير صحيح.", "error")
        return redirect(url_for("buy"))

    rate = float(s["buy_rate"])
    fiat = amount * rate
    image = request.files.get("tx_image")
    filename = None

    if image and image.filename:
        if not allowed_file(image.filename):
            flash("صيغة الصورة غير مدعومة. استخدم PNG أو JPG أو WEBP.", "error")
            return redirect(url_for("buy"))
        ext = image.filename.rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        image.save(os.path.join(UPLOAD_DIR, filename))

    code = uuid.uuid4().hex[:8].upper()
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db()
    conn.execute("""
        INSERT INTO orders
        (order_code,order_type,usdt_amount,fiat_amount,customer_name,customer_phone,
         provider,wallet_address,tx_image,status,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        code, "شراء USDT", amount, fiat,
        request.form.get("customer_name","").strip(),
        request.form.get("customer_phone","").strip(),
        request.form.get("provider","").strip(),
        request.form.get("wallet_address","").strip(),
        filename, "قيد المراجعة", created
    ))
    conn.commit()
    order = conn.execute("SELECT * FROM orders WHERE order_code=?", (code,)).fetchone()
    conn.close()

    _, _, tg_message_id = send_telegram(order, s)
    if tg_message_id:
        conn = db()
        conn.execute("UPDATE orders SET telegram_message_id=? WHERE order_code=?", (tg_message_id, code))
        conn.commit()
        conn.close()
    wa_text = f"مرحباً، أرسلت إشعار حوالة طلب شراء USDT رقم #{code}. المبلغ: {fiat:.2f} شيكل."
    session["last_order_code"] = code
    return redirect(url_for("order_result", order_code=code))

@app.route("/order/<order_code>")
def order_result(order_code):
    # Result pages are GET-only, so refreshing them never creates a new order.
    conn = db()
    order = conn.execute(
        "SELECT * FROM orders WHERE order_code=?",
        (order_code,)
    ).fetchone()
    conn.close()
    if not order:
        abort(404)
    wa_text = f"مرحباً، أرسلت إشعار حوالة طلب {order['order_type']} رقم #{order['order_code']}. المبلغ: {order['fiat_amount']:.2f} شيكل."
    return render_template("success.html", order=order, whatsapp_message=wa_text)

@app.route("/order/<order_code>/status")
def order_status(order_code):
    conn = db()
    order = conn.execute(
        "SELECT order_code,status FROM orders WHERE order_code=?",
        (order_code,)
    ).fetchone()
    conn.close()
    if not order:
        abort(404)
    return {"order_code": order["order_code"], "status": order["status"]}

@app.route("/uploads/<path:filename>")
@admin_required
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == os.environ.get("ADMIN_PASSWORD", "123456"):
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("كلمة المرور غير صحيحة.", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin():
    conn = db()
    orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin.html", orders=orders)

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        save_settings(request.form)
        flash("تم حفظ الإعدادات بنجاح.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("settings.html")

@app.route("/admin/order/<int:order_id>/status", methods=["POST"])
@admin_required
def update_status(order_id):
    status = request.form.get("status", "قيد المراجعة")
    allowed = {"قيد المراجعة", "قيد التنفيذ", "مكتملة", "مرفوضة"}
    if status not in allowed:
        abort(400)
    conn = db()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

if __name__ == "__main__":
    init_db()
    threading.Thread(target=telegram_worker, daemon=True).start()
    print("\nUSDT site running at: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
