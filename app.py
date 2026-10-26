import os, secrets, hashlib, hmac, mimetypes, uuid, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, and_, func, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import cloudinary
    import cloudinary.uploader
except ImportError:
    cloudinary = None
try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '0') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=35 * 1024 * 1024,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

raw_db_url = os.getenv('DATABASE_URL', '').strip()
if raw_db_url.startswith('postgres://'):
    raw_db_url = raw_db_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif raw_db_url.startswith('postgresql://'):
    raw_db_url = raw_db_url.replace('postgresql://', 'postgresql+psycopg://', 1)
if raw_db_url:
    app.config['SQLALCHEMY_DATABASE_URI'] = raw_db_url
else:
    # Never silently switch a deployed Render app to an empty local SQLite DB.
    # That makes all existing PostgreSQL profiles appear to have disappeared.
    if os.getenv('RENDER') or os.getenv('RENDER_SERVICE_ID'):
        raise RuntimeError(
            'DATABASE_URL is missing on Render. Configure the PostgreSQL DATABASE_URL; '
            'refusing to start with a temporary SQLite database.'
        )
    local_db = Path(os.getenv('DATABASE_PATH', 'nstu_dating.db'))
    local_db.parent.mkdir(parents=True, exist_ok=True)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(local_db.resolve())
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
db = SQLAlchemy(app)

UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', 'uploads')).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ALLOWED_VIDEO = {'mp4', 'webm', 'mov'}
ALLOWED_DOC = {'pdf', 'ppt', 'pptx', 'png', 'jpg', 'jpeg', 'webp'}
COUNTRIES = ['Россия', 'Китай', 'Казахстан', 'Индия', 'Бангладеш', 'Вьетнам', 'Монголия', 'Беларусь', 'Армения', 'Узбекистан', 'Кыргызстан', 'Таджикистан', 'Турция', 'Иран', 'Ирак', 'Сирия', 'Иордания', 'Египет', 'Нигерия', 'Гана', 'Бразилия', 'Германия', 'Франция', 'Другая страна']
COUNTRY_FLAGS = {'Россия':'🇷🇺','Китай':'🇨🇳','Казахстан':'🇰🇿','Индия':'🇮🇳','Бангладеш':'🇧🇩','Вьетнам':'🇻🇳','Монголия':'🇲🇳','Беларусь':'🇧🇾','Армения':'🇦🇲','Узбекистан':'🇺🇿','Кыргызстан':'🇰🇬','Таджикистан':'🇹🇯','Турция':'🇹🇷','Иран':'🇮🇷','Ирак':'🇮🇶','Сирия':'🇸🇾','Иордания':'🇯🇴','Египет':'🇪🇬','Нигерия':'🇳🇬','Гана':'🇬🇭','Бразилия':'🇧🇷','Германия':'🇩🇪','Франция':'🇫🇷','Другая страна':'🌐'}
UNIVERSITIES = ['НГТУ']
FACULTIES = ['АВТФ', 'ФЭН', 'ФПМИ', 'ФГО', 'ФМА', 'ФТФ', 'РЭФ', 'ФБ', 'Другой факультет']
COURSES = ['1', '2', '3', '4', '5', '6']
PREMIUM_PRICE = 360
PREMIUM_DAYS = 30
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER', '').strip()
YOOMONEY_NOTIFICATION_SECRET = os.getenv('YOOMONEY_NOTIFICATION_SECRET', '').strip()
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
CLOUDINARY_URL = os.getenv('CLOUDINARY_URL', '').strip()
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '').strip()
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '').strip()
VAPID_CLAIMS_EMAIL = os.getenv('VAPID_CLAIMS_EMAIL', 'mailto:support@nstu-meet.onrender.com').strip()

# Compatibility helper for templates that use row['field'] syntax.
class ItemAccessMixin:
    def __getitem__(self, key):
        return getattr(self, key)

class User(ItemAccessMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(50), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    faculty = db.Column(db.String(100), default='', nullable=False)
    course = db.Column(db.String(30), default='', nullable=False)
    interests = db.Column(db.String(300), default='', nullable=False)
    about = db.Column(db.String(700), default='', nullable=False)
    looking_for = db.Column(db.String(100), default='Общение и знакомства', nullable=False)
    mood_status = db.Column(db.String(40), default='🙂 На расслабоне', nullable=False)
    country = db.Column(db.String(80), default='Россия', nullable=False)
    university = db.Column(db.String(80), default='НГТУ', nullable=False)
    university_honest = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    premium_until = db.Column(db.DateTime(timezone=True))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    @property
    def photo(self):
        p = ProfilePhoto.query.filter_by(user_id=self.id).order_by(ProfilePhoto.id.desc()).first()
        return p.filename if p else None

class YandexIdentity(db.Model):
    __tablename__ = 'yandex_identities'
    id = db.Column(db.Integer, primary_key=True)
    yandex_uid = db.Column(db.String(100), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Like(db.Model):
    __tablename__ = 'likes'
    id = db.Column(db.Integer, primary_key=True)
    liker_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    liked_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (UniqueConstraint('liker_id', 'liked_id'), db.CheckConstraint('liker_id != liked_id'))

class Message(ItemAccessMixin, db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    body = db.Column(db.String(1000), nullable=False, default='')
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class ChatMedia(ItemAccessMixin, db.Model):
    __tablename__ = 'chat_media'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    media_type = db.Column(db.String(30), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Block(db.Model):
    __tablename__ = 'blocks'
    blocker_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    blocked_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Report(db.Model):
    __tablename__ = 'reports'
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    reported_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Payment(ItemAccessMixin, db.Model):
    __tablename__ = 'yoomoney_payments'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    label = db.Column(db.String(80), unique=True, nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False, default=PREMIUM_PRICE)
    status = db.Column(db.String(30), nullable=False, default='pending')
    operation_id = db.Column(db.String(100))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class PushSubscription(db.Model):
    __tablename__ = 'push_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    subscription_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class ProfilePhoto(ItemAccessMixin, db.Model):
    __tablename__ = 'profile_photos'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Group(ItemAccessMixin, db.Model):
    __tablename__ = 'groups_table'
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    university = db.Column(db.String(80), default='НГТУ', nullable=False)
    name = db.Column(db.String(70), nullable=False)
    description = db.Column(db.String(500), default='', nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class GroupMember(db.Model):
    __tablename__ = 'group_members'
    group_id = db.Column(db.Integer, db.ForeignKey('groups_table.id', ondelete='CASCADE'), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    joined_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class GroupMessage(ItemAccessMixin, db.Model):
    __tablename__ = 'group_messages'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('groups_table.id', ondelete='CASCADE'), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    body = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Listing(ItemAccessMixin, db.Model):
    __tablename__ = 'listings'
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    university = db.Column(db.String(80), default='НГТУ', nullable=False)
    title = db.Column(db.String(100), nullable=False, index=True)
    description = db.Column(db.String(1000), default='', nullable=False)
    price = db.Column(db.String(30), nullable=False)
    photo = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    seller = db.relationship('User', foreign_keys=[seller_id])
    @property
    def display_name(self): return self.seller.display_name if self.seller else ''
    @property
    def country(self): return self.seller.country if self.seller else ''

class ListingContact(db.Model):
    __tablename__ = 'listing_contacts'
    listing_id = db.Column(db.Integer, db.ForeignKey('listings.id', ondelete='CASCADE'), primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Lecture(ItemAccessMixin, db.Model):
    __tablename__ = 'lectures'
    id = db.Column(db.Integer, primary_key=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    faculty = db.Column(db.String(100), nullable=False, index=True)
    course = db.Column(db.String(10), nullable=False, index=True)
    teacher = db.Column(db.String(120), nullable=False, index=True)
    subject = db.Column(db.String(120), nullable=False, index=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.String(1000), default='', nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_kind = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    uploader = db.relationship('User', foreign_keys=[uploader_id])
    @property
    def uploader_name(self): return self.uploader.display_name if self.uploader else 'Студент'

class LectureAccess(db.Model):
    __tablename__ = 'lecture_access'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    subject = db.Column(db.String(120), primary_key=True)
    first_access_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


def now_utc(): return datetime.now(timezone.utc)
def premium_active(user):
    if not user or not user.premium_until: return False
    until = user.premium_until
    if until.tzinfo is None: until = until.replace(tzinfo=timezone.utc)
    return until > now_utc()
def get_user(user_id=None):
    uid = user_id if user_id is not None else session.get('user_id')
    return db.session.get(User, int(uid)) if uid else None
@app.context_processor
def inject_globals():
    user = get_user()
    return {'current_user': user, 'is_premium': premium_active(user)}
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not get_user():
            session.clear(); flash('Сначала войди в аккаунт.', 'warning'); return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper
def is_blocked(a,b):
    return db.session.query(Block.blocker_id).filter(or_(and_(Block.blocker_id==a,Block.blocked_id==b),and_(Block.blocker_id==b,Block.blocked_id==a))).first() is not None
def matched(a,b):
    return Like.query.filter_by(liker_id=a, liked_id=b).first() is not None and Like.query.filter_by(liker_id=b, liked_id=a).first() is not None
def can_chat(a,b):
    if matched(a,b) and not is_blocked(a,b): return True
    if is_blocked(a,b): return False
    return ListingContact.query.filter(or_(and_(ListingContact.buyer_id==a,ListingContact.seller_id==b),and_(ListingContact.buyer_id==b,ListingContact.seller_id==a))).first() is not None

def save_upload(file, allowed, max_bytes=30*1024*1024):
    if not file or not file.filename or '.' not in file.filename: return None
    original = secure_filename(file.filename)
    ext = original.rsplit('.',1)[1].lower()
    if ext not in allowed: return None
    # Cloudinary is persistent storage; local uploads are only a fallback for development.
    if CLOUDINARY_URL and cloudinary:
        try:
            file.stream.seek(0, 2); size = file.stream.tell(); file.stream.seek(0)
            if size > max_bytes: return None
            result = cloudinary.uploader.upload(file, resource_type='auto', folder='nstu-meet', public_id=secrets.token_hex(18), overwrite=False, use_filename=False, unique_filename=False)
            return result.get('secure_url')
        except Exception:
            app.logger.exception('Cloudinary upload failed')
            return None
    name = secrets.token_hex(18) + '.' + ext
    target = UPLOAD_DIR / name
    file.save(target)
    if target.stat().st_size > max_bytes:
        target.unlink(missing_ok=True); return None
    return name

@app.route('/')
def index():
    if get_user(): return redirect(url_for('discover'))
    return render_template('index.html')

@app.route('/register', methods=['GET','POST'])
def register():
    # New accounts are created only after successful Yandex OAuth.
    flash('Создание аккаунта доступно только через кнопку «Войти через Яндекс».', 'info')
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    # Password-based login is disabled to keep the requested Yandex-only flow.
    return render_template('login.html')

YANDEX_CLIENT_ID = os.getenv('YANDEX_CLIENT_ID', '').strip()
YANDEX_CLIENT_SECRET = os.getenv('YANDEX_CLIENT_SECRET', '').strip()
YANDEX_REDIRECT_URI = os.getenv('YANDEX_REDIRECT_URI', 'https://nstu-meet.onrender.com/auth/yandex/callback').strip()

@app.route('/auth/yandex')
def yandex_login():
    if not YANDEX_CLIENT_ID:
        flash('Вход через Яндекс пока не настроен. Попробуй позже.', 'warning')
        return redirect(url_for('login'))
    state = secrets.token_urlsafe(32)
    session['yandex_oauth_state'] = state
    params = {'response_type':'code','client_id':YANDEX_CLIENT_ID,'redirect_uri':YANDEX_REDIRECT_URI,'state':state}
    return redirect('https://oauth.yandex.ru/authorize?' + urlencode(params))

@app.route('/auth/yandex/callback')
def yandex_callback():
    expected = session.pop('yandex_oauth_state', None)
    if not expected or not hmac.compare_digest(expected, request.args.get('state','')):
        flash('Проверка входа не пройдена. Попробуй ещё раз.', 'warning')
        return redirect(url_for('login'))
    if request.args.get('error') or not request.args.get('code'):
        flash('Вход через Яндекс отменён или не одобрен.', 'warning')
        return redirect(url_for('login'))
    if not YANDEX_CLIENT_ID or not YANDEX_CLIENT_SECRET:
        app.logger.error('Yandex OAuth credentials are not configured')
        flash('Вход через Яндекс ещё не подключён до конца.', 'warning')
        return redirect(url_for('login'))
    try:
        import urllib.request
        token_data = urlencode({'grant_type':'authorization_code','code':request.args['code'],'client_id':YANDEX_CLIENT_ID,'client_secret':YANDEX_CLIENT_SECRET,'redirect_uri':YANDEX_REDIRECT_URI}).encode()
        token_req = urllib.request.Request('https://oauth.yandex.ru/token', data=token_data, headers={'Content-Type':'application/x-www-form-urlencoded'}, method='POST')
        with urllib.request.urlopen(token_req, timeout=12) as response:
            token_json = json.loads(response.read().decode('utf-8'))
        access_token = token_json.get('access_token')
        if not access_token: raise ValueError('No access token returned')
        info_req = urllib.request.Request('https://login.yandex.ru/info?format=json', headers={'Authorization':'OAuth '+access_token})
        with urllib.request.urlopen(info_req, timeout=12) as response:
            info = json.loads(response.read().decode('utf-8'))
        yandex_uid = str(info.get('id','')).strip()
        if not yandex_uid: raise ValueError('Yandex account ID missing')
        identity = YandexIdentity.query.filter_by(yandex_uid=yandex_uid).first()
        if identity:
            user = db.session.get(User, identity.user_id)
            if not user or not user.is_active:
                flash('Этот аккаунт NSTU Meet неактивен.', 'warning')
                return redirect(url_for('login'))
            session.clear(); session.permanent=True; session['user_id']=user.id
            return redirect(url_for('discover' if user.faculty and user.course and user.country else 'profile'))
        username = 'ya_' + hashlib.sha256(yandex_uid.encode()).hexdigest()[:30]
        user = User(username=username, password_hash=generate_password_hash(secrets.token_urlsafe(32)),
                    display_name='Новый пользователь', age=18, faculty='', course='', country='Россия',
                    university='НГТУ', university_honest=True)
        db.session.add(user); db.session.flush()
        db.session.add(YandexIdentity(yandex_uid=yandex_uid, user_id=user.id))
        db.session.commit()
        session.clear(); session.permanent=True; session['user_id']=user.id
        flash('Вход выполнен! Заполни факультет, курс и страну, чтобы продолжить.', 'success')
        return redirect(url_for('profile'))
    except Exception:
        db.session.rollback()
        app.logger.exception('Yandex OAuth callback failed')
        flash('Не удалось войти через Яндекс. Проверь настройки приложения и попробуй ещё раз.', 'danger')
        return redirect(url_for('login'))


@app.route('/logout', methods=['POST'])
@login_required
def logout(): session.clear(); return redirect(url_for('index'))

@app.route('/profile', methods=['GET','POST'])
@login_required
def profile():
    user=get_user()
    if request.method=='POST':
        try: age=int(request.form.get('age','0'))
        except ValueError: age=0
        name=request.form.get('display_name','').strip(); faculty=request.form.get('faculty','').strip(); course=request.form.get('course','').strip(); country=request.form.get('country',user.country).strip()
        interests=request.form.get('interests','').strip(); about=request.form.get('about','').strip(); looking=request.form.get('looking_for','Общение и знакомства').strip(); mood=request.form.get('mood_status','🙂 На расслабоне').strip()
        if not name or len(name)>50 or not 18<=age<=100: flash('Проверь имя и возраст.','danger')
        elif country not in COUNTRIES: flash('Выбери страну из списка.','danger')
        else:
            if mood not in ['😊 В хорошем настроении','🥰 Хочу знакомиться','😎 На своём вайбе','🙂 На расслабоне','🥱 Немного устал(а)','🎧 Слушаю музыку','🤍 Просто общаюсь']:
                mood='🙂 На расслабоне'
            user.display_name=name; user.age=age; user.faculty=faculty[:100]; user.course=course[:30]; user.country=country; user.university='НГТУ'; user.interests=interests[:300]; user.about=about[:700]; user.looking_for=looking[:100]; user.mood_status=mood
            db.session.commit(); flash('Анкета сохранена.','success'); return redirect(url_for('profile'))
    photos=ProfilePhoto.query.filter_by(user_id=user.id).order_by(ProfilePhoto.id.desc()).all()
    return render_template('profile.html',user=user,photos=photos,countries=COUNTRIES,country_flags=COUNTRY_FLAGS,universities=UNIVERSITIES,faculties=FACULTIES,courses=COURSES)

@app.route('/discover')
@login_required
def discover():
    me=get_user(); liked_ids=db.session.query(Like.liked_id).filter_by(liker_id=me.id).subquery()
    blocked_ids=db.session.query(Block.blocked_id).filter_by(blocker_id=me.id).subquery()
    reverse_blocked=db.session.query(Block.blocker_id).filter_by(blocked_id=me.id).subquery()
    profiles=User.query.filter(User.id!=me.id,User.is_active.is_(True),~User.id.in_(liked_ids),~User.id.in_(blocked_ids),~User.id.in_(reverse_blocked)).order_by(User.created_at.desc()).all()
    today=now_utc().date(); day_start=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc); day_end=day_start+timedelta(days=1); likes_today=Like.query.filter(Like.liker_id==me.id,Like.created_at>=day_start,Like.created_at<day_end).count()
    return render_template('discover.html',profiles=profiles,likes_today=likes_today,limit_reached=(not premium_active(me) and likes_today>=10))

@app.route('/like/<int:target_id>',methods=['POST'])
@login_required
def like(target_id):
    me=get_user()
    if target_id==me.id or not User.query.filter_by(id=target_id,is_active=True).first() or is_blocked(me.id,target_id): abort(404)
    today=now_utc().date(); day_start=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc); day_end=day_start+timedelta(days=1); count=Like.query.filter(Like.liker_id==me.id,Like.created_at>=day_start,Like.created_at<day_end).count()
    if not premium_active(me) and count>=10: flash('На бесплатном аккаунте доступно 10 лайков в день.','warning'); return redirect(url_for('premium'))
    try: db.session.add(Like(liker_id=me.id,liked_id=target_id)); db.session.commit()
    except IntegrityError: db.session.rollback()
    if matched(me.id,target_id): flash('Это взаимно! Теперь можно общаться.','success'); return redirect(url_for('matches'))
    flash('Лайк отправлен.','success'); return redirect(url_for('discover'))

@app.route('/matches')
@login_required
def matches_page():
    me=get_user(); mine=Like.query.filter_by(liker_id=me.id).all(); ids=[x.liked_id for x in mine if Like.query.filter_by(liker_id=x.liked_id,liked_id=me.id).first() and not is_blocked(me.id,x.liked_id)]
    people=User.query.filter(User.id.in_(ids),User.is_active.is_(True)).all() if ids else []
    return render_template('matches.html',matches=people)
# Keep the endpoint name expected by existing templates.
app.add_url_rule('/matches', endpoint='matches', view_func=matches_page)

@app.route('/chat/<int:other_id>',methods=['GET','POST'])
@login_required
def chat(other_id):
    me=get_user(); other=User.query.filter_by(id=other_id,is_active=True).first()
    if not other or other.id==me.id or not can_chat(me.id,other_id): flash('Чат открывается после взаимного лайка или обращения по объявлению.','warning'); return redirect(url_for('matches'))
    if request.method=='POST':
        body=request.form.get('body','').strip()
        if body and len(body)<=1000:
            db.session.add(Message(sender_id=me.id,receiver_id=other_id,body=body)); db.session.commit()
            send_push_to_user(other_id, 'Новое сообщение', f'{me.display_name}: {body[:100]}', url_for('chat', other_id=me.id))
        elif body: flash('Сообщение слишком длинное.','warning')
        return redirect(url_for('chat',other_id=other_id))
    msgs=[]
    for m in Message.query.filter(or_(and_(Message.sender_id==me.id,Message.receiver_id==other_id),and_(Message.sender_id==other_id,Message.receiver_id==me.id))).all():
        m.sender_name=User.query.get(m.sender_id).display_name; m.filename=None; m.media_type='text'; msgs.append(m)
    for m in ChatMedia.query.filter(or_(and_(ChatMedia.sender_id==me.id,ChatMedia.receiver_id==other_id),and_(ChatMedia.sender_id==other_id,ChatMedia.receiver_id==me.id))).all():
        m.body=''; m.sender_name=User.query.get(m.sender_id).display_name; msgs.append(m)
    msgs.sort(key=lambda x:(x.created_at or now_utc(),x.id))
    return render_template('chat.html',other=other,messages=msgs,me_id=me.id)

@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    # Cloudinary URLs are stored directly in the database; local names use the Render upload folder.
    if filename.startswith('https://') or filename.startswith('http://'):
        return redirect(filename)
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False, conditional=True)

@app.route('/profile/photos',methods=['POST'])
@login_required
def upload_profile_photos():
    me=get_user(); existing=ProfilePhoto.query.filter_by(user_id=me.id).count(); added=0
    for f in request.files.getlist('photos'):
        if existing+added>=6: break
        name=save_upload(f,ALLOWED_IMAGE,8*1024*1024)
        if name: db.session.add(ProfilePhoto(user_id=me.id,filename=name)); added+=1
    db.session.commit(); flash(f'Загружено фотографий: {added}. Максимум — 6.','success' if added else 'warning'); return redirect(url_for('profile'))
@app.route('/profile/photos/<int:photo_id>/delete',methods=['POST'])
@login_required
def delete_profile_photo(photo_id):
    p=ProfilePhoto.query.filter_by(id=photo_id,user_id=get_user().id).first()
    if p:
        if p.filename.startswith('http'):
            if cloudinary and CLOUDINARY_URL:
                try:
                    public_id='nstu-meet/'+p.filename.split('/nstu-meet/')[-1].rsplit('.',1)[0]
                    cloudinary.uploader.destroy(public_id, resource_type='image', invalidate=True)
                except Exception: app.logger.exception('Cloudinary delete failed')
        else:
            (UPLOAD_DIR/p.filename).unlink(missing_ok=True)
        db.session.delete(p); db.session.commit()
    return redirect(url_for('profile'))

@app.route('/likes/sent')
@login_required
def likes_sent():
    me=get_user()
    if not premium_active(me): flash('Список отправленных лайков доступен в Premium.','warning'); return redirect(url_for('premium'))
    profiles=[]
    for l in Like.query.filter_by(liker_id=me.id).order_by(Like.created_at.desc()).all():
        u=User.query.get(l.liked_id)
        if u and u.is_active and not is_blocked(me.id,u.id): profiles.append(u)
    return render_template('likes.html',profiles=profiles,page_title='Мои лайки',received=False)
@app.route('/likes/sent/<int:target_id>/delete',methods=['POST'])
@login_required
def delete_sent_like(target_id):
    me=get_user()
    if not premium_active(me):
        flash('Список отправленных лайков доступен в Premium.','warning')
        return redirect(url_for('premium'))
    like_row=Like.query.filter_by(liker_id=me.id,liked_id=target_id).first()
    if like_row:
        db.session.delete(like_row); db.session.commit()
        flash('Лайк удалён из твоего списка.','success')
    return redirect(url_for('likes_sent'))
@app.route('/likes/received')
@login_required
def likes_received():
    me=get_user()
    profiles=[]
    for l in Like.query.filter_by(liked_id=me.id).order_by(Like.created_at.desc()).all():
        u=User.query.get(l.liker_id)
        if u and u.is_active and not is_blocked(me.id,u.id): profiles.append(u)
    return render_template('likes.html',profiles=profiles,page_title='Кто меня лайкнул',received=True)
@app.route('/chats')
@login_required
def chats():
    me=get_user(); ids=set()
    for l in Like.query.filter_by(liker_id=me.id).all():
        if matched(me.id,l.liked_id): ids.add(l.liked_id)
    for c in ListingContact.query.filter(or_(ListingContact.buyer_id==me.id,ListingContact.seller_id==me.id)).all(): ids.add(c.seller_id if c.buyer_id==me.id else c.buyer_id)
    chats_data=[]
    for uid in ids:
        if is_blocked(me.id,uid): continue
        u=User.query.get(uid)
        if not u or not u.is_active: continue
        last=Message.query.filter(or_(and_(Message.sender_id==me.id,Message.receiver_id==uid),and_(Message.sender_id==uid,Message.receiver_id==me.id))).order_by(Message.id.desc()).first()
        u.last_message=last.body if last else ''
        chats_data.append(u)
    chats_data.sort(key=lambda u:u.display_name.lower())
    return render_template('chats.html',chats=chats_data)
@app.route('/chat/<int:other_id>/send-media',methods=['POST'])
@login_required
def chat_send_media(other_id):
    me=get_user()
    if not can_chat(me.id,other_id) or is_blocked(me.id,other_id): abort(403)
    f=request.files.get('media'); ext=f.filename.rsplit('.',1)[-1].lower() if f and f.filename and '.' in f.filename else ''
    if ext not in ALLOWED_IMAGE|ALLOWED_VIDEO: flash('Допустимы изображения и видео MP4/WebM/MOV.','warning')
    else:
        name=save_upload(f,ALLOWED_IMAGE|ALLOWED_VIDEO,20*1024*1024)
        if name: db.session.add(ChatMedia(sender_id=me.id,receiver_id=other_id,filename=name,media_type='video' if ext in ALLOWED_VIDEO else 'image')); db.session.commit()
        else: flash('Не удалось сохранить файл.','warning')
    return redirect(url_for('chat',other_id=other_id))
@app.route('/api/chat/<int:other_id>')
@login_required
def chat_updates(other_id):
    me=get_user()
    if not can_chat(me.id,other_id) or is_blocked(me.id,other_id): abort(403)
    after=request.args.get('after',0,type=int); out=[]
    for m in Message.query.filter(or_(and_(Message.sender_id==me.id,Message.receiver_id==other_id),and_(Message.sender_id==other_id,Message.receiver_id==me.id)),Message.id>after).order_by(Message.id).all(): out.append({'id':m.id,'sender_id':m.sender_id,'body':m.body,'created_at':m.created_at.isoformat(),'filename':None,'media_type':'text'})
    for m in ChatMedia.query.filter(or_(and_(ChatMedia.sender_id==me.id,ChatMedia.receiver_id==other_id),and_(ChatMedia.sender_id==other_id,ChatMedia.receiver_id==me.id)),ChatMedia.id>after).order_by(ChatMedia.id).all(): out.append({'id':m.id,'sender_id':m.sender_id,'body':'','created_at':m.created_at.isoformat(),'filename':m.filename,'media_type':m.media_type})
    return jsonify(sorted(out,key=lambda x:x['created_at']))

@app.route('/groups',methods=['GET','POST'])
@login_required
def groups():
    me=get_user()
    if not premium_active(me): flash('Создание и участие в группах доступны в Premium.','warning'); return redirect(url_for('premium'))
    if request.method=='POST':
        name=request.form.get('name','').strip()[:70]; desc=request.form.get('description','').strip()[:500]
        if name:
            g=Group(owner_id=me.id,university='НГТУ',name=name,description=desc); db.session.add(g); db.session.flush(); db.session.add(GroupMember(group_id=g.id,user_id=me.id)); db.session.commit(); return redirect(url_for('groups'))
        flash('Укажи название группы.','warning')
    all_groups=[]
    for g in Group.query.order_by(Group.created_at.desc()).all():
        g.owner_name=User.query.get(g.owner_id).display_name; g.member_count=GroupMember.query.filter_by(group_id=g.id).count(); g.joined=GroupMember.query.filter_by(group_id=g.id,user_id=me.id).first() is not None; all_groups.append(g)
    return render_template('groups.html',groups=all_groups)
@app.route('/groups/<int:group_id>',methods=['GET','POST'])
@login_required
def group_detail(group_id):
    me=get_user()
    if not premium_active(me): flash('Группы доступны только в Premium.','warning'); return redirect(url_for('premium'))
    g=db.session.get(Group,group_id)
    if not g: abort(404)
    if request.method=='POST':
        if request.form.get('action')=='join':
            if not GroupMember.query.filter_by(group_id=g.id,user_id=me.id).first(): db.session.add(GroupMember(group_id=g.id,user_id=me.id)); db.session.commit()
        else:
            member=GroupMember.query.filter_by(group_id=g.id,user_id=me.id).first(); body=request.form.get('body','').strip()
            if member and body: db.session.add(GroupMessage(group_id=g.id,sender_id=me.id,body=body[:1000])); db.session.commit()
        return redirect(url_for('group_detail',group_id=g.id))
    joined=GroupMember.query.filter_by(group_id=g.id,user_id=me.id).first() is not None
    messages=GroupMessage.query.filter_by(group_id=g.id).order_by(GroupMessage.id.desc()).limit(100).all()[::-1]
    for m in messages: m.display_name=User.query.get(m.sender_id).display_name
    return render_template('group_detail.html',group=g,joined=joined,messages=messages,me_id=me.id)

@app.route('/market',methods=['GET','POST'])
@login_required
def market():
    me=get_user()
    if request.method=='POST':
        title=request.form.get('title','').strip()[:100]; description=request.form.get('description','').strip()[:1000]; price=request.form.get('price','').strip()[:30]
        photo=save_upload(request.files.get('photo'),ALLOWED_IMAGE,8*1024*1024)
        if not title or not price: flash('Укажи название и цену.','warning')
        else: db.session.add(Listing(seller_id=me.id,university='НГТУ',title=title,description=description,price=price,photo=photo)); db.session.commit(); flash('Объявление опубликовано.','success'); return redirect(url_for('market'))
    search=request.args.get('q','').strip()[:100]
    query=Listing.query.filter_by(is_active=True)
    if search: query=query.filter(or_(Listing.title.ilike(f'%{search}%'),Listing.description.ilike(f'%{search}%')))
    listings=query.order_by(Listing.created_at.desc()).limit(200).all()
    return render_template('market.html',listings=listings,universities=UNIVERSITIES,selected_university='НГТУ',me=me,search=search)
@app.route('/market/<int:listing_id>/contact',methods=['POST'])
@login_required
def market_contact(listing_id):
    me=get_user(); listing=db.session.get(Listing,listing_id)
    if not listing or not listing.is_active: abort(404)
    if listing.seller_id==me.id: flash('Это твоё объявление.','info'); return redirect(url_for('market'))
    if is_blocked(me.id,listing.seller_id): flash('Связаться с этим пользователем нельзя.','warning'); return redirect(url_for('market'))
    contact=ListingContact.query.filter_by(listing_id=listing.id,buyer_id=me.id).first()
    if not contact: db.session.add(ListingContact(listing_id=listing.id,buyer_id=me.id,seller_id=listing.seller_id)); db.session.commit()
    flash('Чат с продавцом открыт. Напиши ему!','success'); return redirect(url_for('chat',other_id=listing.seller_id))
@app.route('/market/<int:listing_id>/delete',methods=['POST'])
@login_required
def delete_listing(listing_id):
    l=Listing.query.filter_by(id=listing_id,seller_id=get_user().id).first()
    if l: l.is_active=False; db.session.commit()
    return redirect(url_for('market'))

@app.route('/lectures',methods=['GET','POST'])
@login_required
def lectures():
    me=get_user()
    if request.method=='POST':
        faculty=request.form.get('faculty','').strip()[:100]; course=request.form.get('course','').strip()[:10]; teacher=request.form.get('teacher','').strip()[:120]; subject=request.form.get('subject','').strip()[:120]; title=request.form.get('title','').strip()[:150]; desc=request.form.get('description','').strip()[:1000]; f=request.files.get('file')
        if not all([faculty,course,teacher,subject,title,f and f.filename]): flash('Заполни факультет, курс, преподавателя, предмет, название и приложи файл.','warning')
        elif course not in COURSES: flash('Выбери корректный курс.','warning')
        else:
            ext=f.filename.rsplit('.',1)[-1].lower() if '.' in f.filename else ''
            if ext not in ALLOWED_DOC: flash('Поддерживаются PDF, PPT/PPTX и изображения JPG/PNG/WebP.','warning')
            else:
                # Premium can publish; free users may browse up to three subjects and cannot upload.
                if not premium_active(me): flash('Публикация материалов доступна в Premium.','warning')
                else:
                    original=secure_filename(f.filename)[:255]; saved=save_upload(f,ALLOWED_DOC,25*1024*1024)
                    if saved:
                        db.session.add(Lecture(uploader_id=me.id,faculty=faculty,course=course,teacher=teacher,subject=subject,title=title,description=desc,filename=saved,original_filename=original,file_kind='image' if ext in {'png','jpg','jpeg','webp'} else 'document')); db.session.commit(); flash('Учебный материал опубликован.','success'); return redirect(url_for('lectures',faculty=faculty,course=course,subject=subject))
    faculty=request.args.get('faculty','').strip(); course=request.args.get('course','').strip(); teacher=request.args.get('teacher','').strip(); subject=request.args.get('subject','').strip(); search=request.args.get('q','').strip()[:100]
    q=Lecture.query
    if faculty: q=q.filter_by(faculty=faculty)
    if course: q=q.filter_by(course=course)
    if teacher: q=q.filter(Lecture.teacher.ilike(f'%{teacher}%'))
    if subject: q=q.filter_by(subject=subject)
    if search: q=q.filter(or_(Lecture.title.ilike(f'%{search}%'),Lecture.description.ilike(f'%{search}%'),Lecture.subject.ilike(f'%{search}%'),Lecture.teacher.ilike(f'%{search}%')))
    materials=q.order_by(Lecture.created_at.desc()).limit(200).all()
    subjects=[r[0] for r in db.session.query(Lecture.subject).distinct().order_by(Lecture.subject).all()]
    selected_subjects=[r[0] for r in LectureAccess.query.filter_by(user_id=me.id).all()]
    if not premium_active(me) and subject and subject not in selected_subjects:
        if len(selected_subjects)>=3:
            flash('В бесплатном доступе можно выбрать три предмета. Удали один из выбранных ниже или подключи Premium.','warning'); materials=[]
        else:
            db.session.add(LectureAccess(user_id=me.id,subject=subject)); db.session.commit(); selected_subjects.append(subject)
    if not premium_active(me):
        if subject and subject not in selected_subjects: materials=[]
        elif not subject: materials=[m for m in materials if m.subject in selected_subjects]
    return render_template('lectures.html',materials=materials,faculties=FACULTIES,courses=COURSES,subjects=subjects,selected_faculty=faculty,selected_course=course,selected_teacher=teacher,selected_subject=subject,search=search,selected_subjects=selected_subjects,me=me)
@app.route('/lectures/access/remove',methods=['POST'])
@login_required
def lecture_access_remove():
    subject=request.form.get('subject','').strip()
    row=LectureAccess.query.filter_by(user_id=get_user().id,subject=subject).first()
    if row: db.session.delete(row); db.session.commit(); flash(f'Предмет «{subject}» удалён из выбранных.','success')
    return redirect(url_for('lectures'))

@app.route('/lectures/<int:lecture_id>/download')
@login_required
def lecture_download(lecture_id):
    l=db.session.get(Lecture,lecture_id)
    if not l: abort(404)
    if not premium_active(get_user()) and l.subject not in [x[0] for x in db.session.query(LectureAccess.subject).filter_by(user_id=get_user().id).all()]:
        flash('Выбери предмет в разделе лекций или подключи Premium.','warning'); return redirect(url_for('lectures'))
    if l.filename.startswith('https://') or l.filename.startswith('http://'): return redirect(l.filename)
    return send_from_directory(UPLOAD_DIR,l.filename,as_attachment=True,download_name=l.original_filename)
@app.route('/lectures/<int:lecture_id>/delete',methods=['POST'])
@login_required
def lecture_delete(lecture_id):
    l=db.session.get(Lecture,lecture_id)
    if not l: abort(404)
    if l.uploader_id!=get_user().id: abort(403)
    (UPLOAD_DIR/l.filename).unlink(missing_ok=True); db.session.delete(l); db.session.commit(); flash('Материал удалён.','success'); return redirect(url_for('lectures'))

@app.route('/block/<int:target_id>',methods=['POST'])
@login_required
def block(target_id):
    me=get_user()
    if target_id==me.id: abort(400)
    if not Block.query.filter_by(blocker_id=me.id,blocked_id=target_id).first(): db.session.add(Block(blocker_id=me.id,blocked_id=target_id)); db.session.commit()
    flash('Пользователь заблокирован.','success'); return redirect(url_for('discover'))
@app.route('/report/<int:target_id>',methods=['POST'])
@login_required
def report(target_id):
    reason=request.form.get('reason','').strip()[:500]
    if target_id==get_user().id or not reason: flash('Укажи причину жалобы.','warning')
    elif User.query.filter_by(id=target_id,is_active=True).first(): db.session.add(Report(reporter_id=get_user().id,reported_id=target_id,reason=reason)); db.session.commit(); flash('Жалоба сохранена для проверки.','success')
    return redirect(url_for('discover'))

@app.route('/premium')
@login_required
def premium(): return render_template('premium.html',premium_price=PREMIUM_PRICE)
@app.route('/premium/pay',methods=['POST'])
@login_required
def premium_pay():
    me=get_user()
    if not YOOMONEY_RECEIVER:
        flash('Оплата пока не подключена: администратору нужно добавить YOOMONEY_RECEIVER в переменные окружения Render.','warning'); return redirect(url_for('premium'))
    label='nstumeet-'+uuid.uuid4().hex
    p=Payment(user_id=me.id,label=label,amount=PREMIUM_PRICE,status='pending'); db.session.add(p); db.session.commit()
    params={'receiver':YOOMONEY_RECEIVER,'quickpay-form':'shop','targets':'NSTU Meet Premium на 30 дней','sum':f'{PREMIUM_PRICE:.2f}','label':label,'paymentType':'AC'}
    if PUBLIC_BASE_URL: params['successURL']=PUBLIC_BASE_URL + url_for('premium_return')
    return redirect('https://yoomoney.ru/quickpay/confirm.xml?'+urlencode(params))
@app.route('/payments/yoomoney/notify',methods=['POST'])
def yoomoney_notify():
    if not YOOMONEY_NOTIFICATION_SECRET: return 'notifications disabled',503
    f=request.form
    # YooMoney P2P notification signature formula. Never trust the browser return URL to grant Premium.
    parts=[f.get(k,'') for k in ['notification_type','operation_id','amount','currency','datetime','sender','codepro']]
    parts.extend([YOOMONEY_NOTIFICATION_SECRET, f.get('label','')])
    expected=hashlib.sha1('&'.join(parts).encode('utf-8')).hexdigest()
    if not hmac.compare_digest(expected.lower(),f.get('sha1_hash','').lower()): return 'bad signature',403
    p=Payment.query.filter_by(label=f.get('label','')).first()
    if not p: return 'unknown label',404
    try: amount=float(f.get('amount','0'))
    except ValueError: amount=0
    if amount < p.amount or f.get('currency') not in ('643','RUB') or f.get('codepro','false').lower()=='true': return 'invalid payment',400
    if p.status!='succeeded':
        p.status='succeeded'; p.operation_id=f.get('operation_id','')
        u=db.session.get(User,p.user_id); base=u.premium_until if u.premium_until and (u.premium_until.replace(tzinfo=timezone.utc) if u.premium_until.tzinfo is None else u.premium_until)>now_utc() else now_utc()
        u.premium_until=base+timedelta(days=PREMIUM_DAYS); db.session.commit()
    return 'OK',200
@app.route('/premium/return')
@login_required
def premium_return():
    flash('Спасибо! Premium активируется после подтверждения платежа платёжным сервисом. Обычная оплата через кошелёк не означает автоматического ежемесячного списания.','info'); return redirect(url_for('premium'))
def send_push_to_user(user_id, title, body, path='/chats'):
    if not (webpush and VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY): return
    for row in PushSubscription.query.filter_by(user_id=user_id).all():
        try:
            webpush(subscription_info=json.loads(row.subscription_json), data=json.dumps({'title': title, 'body': body, 'url': path}), vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={'sub': VAPID_CLAIMS_EMAIL})
        except WebPushException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (404, 410): db.session.delete(row)
        except Exception:
            app.logger.exception('Push notification failed')
    db.session.commit()

@app.route('/api/push/public-key')
@login_required
def push_public_key():
    if not VAPID_PUBLIC_KEY: return jsonify(error='push_not_configured'), 503
    return jsonify(publicKey=VAPID_PUBLIC_KEY)

@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    if not (webpush and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY): return jsonify(error='push_not_configured'), 503
    payload=request.get_json(silent=True) or {}
    if not payload.get('endpoint') or not payload.get('keys'): return jsonify(error='invalid_subscription'), 400
    row=PushSubscription.query.filter_by(endpoint=payload['endpoint']).first()
    if row:
        row.user_id=get_user().id; row.subscription_json=json.dumps(payload)
    else:
        db.session.add(PushSubscription(user_id=get_user().id, endpoint=payload['endpoint'], subscription_json=json.dumps(payload)))
    db.session.commit()
    return jsonify(ok=True)

@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    payload=request.get_json(silent=True) or {}; endpoint=payload.get('endpoint')
    if endpoint:
        PushSubscription.query.filter_by(user_id=get_user().id, endpoint=endpoint).delete(); db.session.commit()
    return jsonify(ok=True)

@app.route('/settings/delete',methods=['POST'])
@login_required
def delete_account():
    me=get_user(); me.is_active=False; db.session.commit(); session.clear(); flash('Аккаунт деактивирован.','info'); return redirect(url_for('index'))
@app.errorhandler(400)
def bad_request(_): return render_template('error.html',code=400,message='Некорректный запрос.'),400
@app.errorhandler(403)
def forbidden(_): return render_template('error.html',code=403,message='Нет доступа к этому действию.'),403
@app.errorhandler(404)
def not_found(_): return render_template('error.html',code=404,message='Страница не найдена.'),404

@app.route('/health/db')
def database_health():
    """Read-only database check; returns aggregate counts only."""
    try:
        with db.engine.connect() as connection:
            total = db.session.query(func.count(User.id)).scalar() or 0
            active = db.session.query(func.count(User.id)).filter(User.is_active.is_(True)).scalar() or 0
            return jsonify(status='ok', database='connected', users_total=total,
                           users_active=active, users_inactive=total-active)
    except Exception:
        app.logger.exception('Database health check failed')
        return jsonify(status='error', database='query_failed'), 503

with app.app_context():
    db.create_all()
    # Add the mood field to existing databases without dropping user data.
    from sqlalchemy import inspect, text
    if 'mood_status' not in {column['name'] for column in inspect(db.engine).get_columns('users')}:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE users ADD COLUMN mood_status VARCHAR(40) NOT NULL DEFAULT '🙂 На расслабоне'"))

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=os.getenv('FLASK_DEBUG','0')=='1')
