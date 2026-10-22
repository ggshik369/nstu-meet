import os
import sqlite3
import secrets
from datetime import datetime, timedelta, date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from yookassa import Configuration, Payment
except ImportError:
    Configuration = None
    Payment = None

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)
DB_PATH = os.getenv("DATABASE_PATH", "nstu_dating.db")
PREMIUM_PRICE = "400.00"
PREMIUM_DAYS = 30

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5000")

if Configuration and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            age INTEGER NOT NULL,
            faculty TEXT NOT NULL DEFAULT '',
            course TEXT NOT NULL DEFAULT '',
            interests TEXT NOT NULL DEFAULT '',
            about TEXT NOT NULL DEFAULT '',
            looking_for TEXT NOT NULL DEFAULT 'Общение и знакомства',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            premium_until TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            liker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            liked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(liker_id, liked_id),
            CHECK(liker_id != liked_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS blocks (
            blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(blocker_id, blocked_id),
            CHECK(blocker_id != blocked_id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reported_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider_payment_id TEXT UNIQUE NOT NULL,
            amount TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)


def get_user(user_id=None):
    user_id = user_id or session.get("user_id")
    if not user_id:
        return None
    with db() as con:
        return con.execute(
            "SELECT id, username, display_name, age, faculty, course, interests, about, looking_for, premium_until "
            "FROM users WHERE id=? AND is_active=1", (user_id,)
        ).fetchone()


def premium_active(user):
    if not user or not user["premium_until"]:
        return False
    try:
        return datetime.fromisoformat(user["premium_until"]) > datetime.now()
    except ValueError:
        return False


@app.context_processor
def inject_globals():
    user = get_user()
    return {"current_user": user, "is_premium": premium_active(user)}


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Сначала войди в аккаунт.", "warning")
            return redirect(url_for("login"))
        if not get_user():
            session.clear()
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def matched(con, a, b):
    return con.execute(
        "SELECT 1 FROM likes l1 JOIN likes l2 ON l1.liker_id=l2.liked_id AND l1.liked_id=l2.liker_id "
        "WHERE l1.liker_id=? AND l1.liked_id=?", (a, b)
    ).fetchone() is not None


def is_blocked(con, a, b):
    return con.execute(
        "SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",
        (a, b, b, a)
    ).fetchone() is not None


@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("discover"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()
        age_raw = request.form.get("age", "").strip()
        faculty = request.form.get("faculty", "").strip()
        course = request.form.get("course", "").strip()

        if len(username) < 3 or len(username) > 40 or not username.replace("_", "").replace(".", "").isalnum():
            flash("Логин: от 3 до 40 символов, латиница, цифры, точка или подчёркивание.", "danger")
            return render_template("register.html")
        if len(password) < 8:
            flash("Пароль должен содержать минимум 8 символов.", "danger")
            return render_template("register.html")
        if not display_name or len(display_name) > 50:
            flash("Укажи имя или ник длиной до 50 символов.", "danger")
            return render_template("register.html")
        try:
            age = int(age_raw)
        except ValueError:
            age = 0
        if age < 18 or age > 100:
            flash("Эта версия сервиса предназначена только для пользователей 18 лет и старше.", "danger")
            return render_template("register.html")
        if len(faculty) > 100 or len(course) > 30:
            flash("Слишком длинное название факультета или курса.", "danger")
            return render_template("register.html")

        try:
            with db() as con:
                cur = con.execute(
                    "INSERT INTO users(username,password_hash,display_name,age,faculty,course) VALUES(?,?,?,?,?,?)",
                    (username, generate_password_hash(password), display_name, age, faculty, course)
                )
                session.clear()
                session["user_id"] = cur.lastrowid
                session["csrf_token"] = secrets.token_urlsafe(32)
            flash("Аккаунт создан! Заполни анкету и начинай знакомиться.", "success")
            return redirect(url_for("profile"))
        except sqlite3.IntegrityError:
            flash("Такой логин уже занят.", "danger")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        with db() as con:
            user = con.execute("SELECT id,password_hash FROM users WHERE username=? AND is_active=1", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("discover"))
        flash("Неверный логин или пароль.", "danger")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = get_user()
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        age_raw = request.form.get("age", "").strip()
        faculty = request.form.get("faculty", "").strip()
        course = request.form.get("course", "").strip()
        interests = request.form.get("interests", "").strip()
        about = request.form.get("about", "").strip()
        looking_for = request.form.get("looking_for", "Общение и знакомства").strip()

        try:
            age = int(age_raw)
        except ValueError:
            age = 0
        if not display_name or len(display_name) > 50 or age < 18 or age > 100:
            flash("Проверь имя и возраст: сервис доступен только с 18 лет.", "danger")
        elif any(len(x) > limit for x, limit in [(faculty,100),(course,30),(interests,300),(about,700),(looking_for,100)]):
            flash("Одно из полей слишком длинное.", "danger")
        else:
            with db() as con:
                con.execute(
                    "UPDATE users SET display_name=?,age=?,faculty=?,course=?,interests=?,about=?,looking_for=? WHERE id=?",
                    (display_name, age, faculty, course, interests, about, looking_for, user["id"])
                )
            flash("Анкета сохранена.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html", user=get_user())


@app.route("/discover")
@login_required
def discover():
    me = get_user()
    with db() as con:
        profiles = con.execute("""
            SELECT u.id,u.display_name,u.age,u.faculty,u.course,u.interests,u.about,u.looking_for
            FROM users u
            WHERE u.id != ? AND u.is_active=1
              AND NOT EXISTS (SELECT 1 FROM blocks b WHERE
                (b.blocker_id=? AND b.blocked_id=u.id) OR (b.blocker_id=u.id AND b.blocked_id=?))
              AND NOT EXISTS (SELECT 1 FROM likes l WHERE l.liker_id=? AND l.liked_id=u.id)
            ORDER BY u.created_at DESC
        """, (me["id"], me["id"], me["id"], me["id"])).fetchall()
        likes_today = con.execute(
            "SELECT COUNT(*) AS n FROM likes WHERE liker_id=? AND date(created_at)=date('now','localtime')",
            (me["id"],)
        ).fetchone()["n"]
    return render_template("discover.html", profiles=profiles, likes_today=likes_today, limit_reached=(not premium_active(me) and likes_today >= 10))


@app.route("/like/<int:target_id>", methods=["POST"])
@login_required
def like(target_id):
    me = get_user()
    if target_id == me["id"]:
        abort(400)
    with db() as con:
        target = con.execute("SELECT id FROM users WHERE id=? AND is_active=1", (target_id,)).fetchone()
        if not target or is_blocked(con, me["id"], target_id):
            flash("Эта анкета недоступна.", "warning")
            return redirect(url_for("discover"))
        likes_today = con.execute(
            "SELECT COUNT(*) AS n FROM likes WHERE liker_id=? AND date(created_at)=date('now','localtime')",
            (me["id"],)
        ).fetchone()["n"]
        if not premium_active(me) and likes_today >= 10:
            flash("На бесплатном аккаунте можно поставить до 10 лайков в день. Подключи Premium.", "warning")
            return redirect(url_for("premium"))
        con.execute("INSERT OR IGNORE INTO likes(liker_id,liked_id) VALUES(?,?)", (me["id"], target_id))
        if matched(con, me["id"], target_id):
            flash("Это взаимно! Теперь вы можете общаться.", "success")
            return redirect(url_for("matches"))
    flash("Лайк отправлен.", "success")
    return redirect(url_for("discover"))


@app.route("/matches")
@login_required
def matches():
    me = get_user()
    with db() as con:
        rows = con.execute("""
            SELECT u.id,u.display_name,u.age,u.faculty,u.course,u.interests
            FROM likes mine
            JOIN likes theirs ON theirs.liker_id=mine.liked_id AND theirs.liked_id=mine.liker_id
            JOIN users u ON u.id=mine.liked_id
            WHERE mine.liker_id=? AND u.is_active=1
            ORDER BY u.display_name
        """, (me["id"],)).fetchall()
        rows = [r for r in rows if not is_blocked(con, me["id"], r["id"])]
    return render_template("matches.html", matches=rows)


@app.route("/chat/<int:other_id>", methods=["GET", "POST"])
@login_required
def chat(other_id):
    me = get_user()
    if other_id == me["id"]:
        abort(400)
    with db() as con:
        other = con.execute(
            "SELECT id,display_name,age,faculty,course FROM users WHERE id=? AND is_active=1", (other_id,)
        ).fetchone()
        if not other or not matched(con, me["id"], other_id) or is_blocked(con, me["id"], other_id):
            flash("Чат доступен только после взаимного лайка.", "warning")
            return redirect(url_for("matches"))
        if request.method == "POST":
            body = request.form.get("body", "").strip()
            if body and len(body) <= 1000:
                con.execute(
                    "INSERT INTO messages(sender_id,receiver_id,body) VALUES(?,?,?)",
                    (me["id"], other_id, body)
                )
            elif body:
                flash("Сообщение не должно превышать 1000 символов.", "danger")
            return redirect(url_for("chat", other_id=other_id))
        msgs = con.execute("""
            SELECT m.id,m.sender_id,m.receiver_id,m.body,m.created_at,u.display_name AS sender_name
            FROM messages m JOIN users u ON u.id=m.sender_id
            WHERE (m.sender_id=? AND m.receiver_id=?) OR (m.sender_id=? AND m.receiver_id=?)
            ORDER BY m.id
        """, (me["id"], other_id, other_id, me["id"])).fetchall()
    return render_template("chat.html", other=other, messages=msgs, me_id=me["id"])


@app.route("/block/<int:target_id>", methods=["POST"])
@login_required
def block(target_id):
    me = get_user()
    if target_id == me["id"]:
        abort(400)
    with db() as con:
        con.execute("INSERT OR IGNORE INTO blocks(blocker_id,blocked_id) VALUES(?,?)", (me["id"], target_id))
    flash("Пользователь заблокирован. Вы больше не увидите друг друга.", "success")
    return redirect(url_for("discover"))


@app.route("/report/<int:target_id>", methods=["POST"])
@login_required
def report(target_id):
    me = get_user()
    reason = request.form.get("reason", "").strip()[:500]
    if target_id == me["id"] or not reason:
        flash("Укажи причину жалобы.", "warning")
    else:
        with db() as con:
            exists = con.execute("SELECT id FROM users WHERE id=? AND is_active=1", (target_id,)).fetchone()
            if exists:
                con.execute("INSERT INTO reports(reporter_id,reported_id,reason) VALUES(?,?,?)", (me["id"], target_id, reason))
                flash("Жалоба отправлена. В демоверсии она сохраняется в базе для проверки администратором.", "success")
    return redirect(url_for("discover"))


@app.route("/premium")
@login_required
def premium():
    return render_template("premium.html")


@app.route("/premium/pay", methods=["POST"])
@login_required
def premium_pay():
    if not (Configuration and Payment and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        flash("Оплата пока не настроена. Добавь учётные данные YooKassa в файл .env.", "warning")
        return redirect(url_for("premium"))
    me = get_user()
    payment = Payment.create({
        "amount": {"value": PREMIUM_PRICE, "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": PUBLIC_BASE_URL.rstrip("/") + url_for("premium_return")
        },
        "description": "NSTU Meet — Premium на 30 дней",
        "metadata": {"user_id": str(me["id"]), "kind": "premium_subscription"},
        "payment_method_data": {"type": "sbp"}
    }, secrets.token_urlsafe(24))
    with db() as con:
        con.execute(
            "INSERT INTO payments(user_id,provider_payment_id,amount,status) VALUES(?,?,?,'pending')",
            (me["id"], payment.id, PREMIUM_PRICE)
        )
    return redirect(payment.confirmation.confirmation_url)


@app.route("/premium/return")
@login_required
def premium_return():
    payment_id = request.args.get("payment_id", "").strip()
    if not payment_id or not (Payment and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        flash("Вернулись со страницы оплаты. Статус можно проверить после настройки YooKassa.", "info")
        return redirect(url_for("premium"))
    try:
        payment = Payment.find_one(payment_id)
        me = get_user()
        metadata = payment.metadata or {}
        with db() as con:
            local = con.execute(
                "SELECT * FROM payments WHERE provider_payment_id=? AND user_id=?",
                (payment_id, me["id"])
            ).fetchone()
            if local and str(metadata.get("user_id")) == str(me["id"]) and metadata.get("kind") == "premium_subscription":
                con.execute("UPDATE payments SET status=? WHERE provider_payment_id=?", (payment.status, payment_id))
                if payment.status == "succeeded" and str(payment.amount.value) == PREMIUM_PRICE:
                    until = datetime.now() + timedelta(days=PREMIUM_DAYS)
                    con.execute("UPDATE users SET premium_until=? WHERE id=?", (until.isoformat(timespec="seconds"), me["id"]))
                    flash("Premium активирован на 30 дней!", "success")
                else:
                    flash("Оплата пока не подтверждена. Если деньги списались, проверь статус в YooKassa.", "info")
            else:
                flash("Не удалось подтвердить этот платёж для текущего аккаунта.", "danger")
    except Exception:
        flash("Не удалось проверить платёж. Попробуй позже.", "danger")
    return redirect(url_for("premium"))


@app.route("/settings/delete", methods=["POST"])
@login_required
def delete_account():
    me = get_user()
    with db() as con:
        con.execute("UPDATE users SET is_active=0 WHERE id=?", (me["id"],))
    session.clear()
    flash("Аккаунт деактивирован. Для полного удаления данных обратись к администратору.", "info")
    return redirect(url_for("index"))


@app.errorhandler(400)
def bad_request(_):
    return render_template("error.html", code=400, message="Некорректный запрос."), 400


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, message="Страница не найдена."), 404


# Create tables also when imported by a production WSGI server.
init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
