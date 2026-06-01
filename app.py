import os
import sys
import logging
from flask import Flask, redirect, url_for, session
from flask_login import LoginManager, current_user, login_user
from flask_migrate import Migrate
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth

load_dotenv()

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Import Database, Mail and Models ────────────────────────────────────────
from models import db, mail, Student, Message, SyncLog, LiveJob, LiveInternship

app = Flask(__name__)

# ── App Config ───────────────────────────────────────────────────────────────
app.config['UPLOAD_FOLDER'] = 'uploads/resumes'
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev_secret_key_12345_change_in_production')

# ── Google OAuth Config ──────────────────────────────────────────────────────
app.config['GOOGLE_CLIENT_ID']     = os.getenv("GOOGLE_CLIENT_ID")
app.config['GOOGLE_CLIENT_SECRET'] = os.getenv("GOOGLE_CLIENT_SECRET")

# ── Database Configuration ───────────────────────────────────────────────────
#
#  Dynamic configuration with automatic local SQLite fallback:
#    1. If a production/remote DATABASE_URL is defined, use it directly.
#    2. Try to connect to a local PostgreSQL instance (using either local DATABASE_URL
#       or individual PG_* credentials).
#    3. If local PostgreSQL is unreachable, automatically fall back to local SQLite
#       (placement.db) for developer testing.
#
def _configure_database(app):
    raw_url = os.getenv("DATABASE_URL", "").strip()

    # Determine if DATABASE_URL points to a local or remote PostgreSQL
    is_local = True
    if raw_url:
        if raw_url.startswith("postgres://"):
            raw_url = raw_url.replace("postgres://", "postgresql://", 1)
        
        from urllib.parse import urlparse
        try:
            res = urlparse(raw_url)
            # FIX: Only treat 'localhost' and '127.0.0.1' as local.
            # Do NOT check for the substring 'postgres' — Render's managed PostgreSQL
            # hostnames look like 'dpg-xxx.oregon-postgres.render.com', which contains
            # 'postgres' as a substring and would have incorrectly been treated as local.
            if res.hostname and res.hostname not in ('localhost', '127.0.0.1'):
                is_local = False
        except Exception:
            pass

    # 1. If it's a remote PostgreSQL (production / Render), use it directly
    if raw_url and not is_local:
        # Mask credentials in the log so we can confirm the correct host without leaking secrets
        try:
            from urllib.parse import urlparse, urlunparse
            _p = urlparse(raw_url)
            _masked = urlunparse(_p._replace(netloc=f"****:****@{_p.hostname}:{_p.port or 5432}"))
        except Exception:
            _masked = "<unparseable>"
        logger.info("Database: using production/remote DATABASE_URL → %s", _masked)
        db_url = raw_url
    else:
        # 2. It's a local database — test the connection to verify PostgreSQL is running
        pg_user     = os.getenv("PG_USER",     "postgres")
        pg_password = os.getenv("PG_PASSWORD", "postgres")
        pg_host     = os.getenv("PG_HOST",     "localhost")
        pg_port     = os.getenv("PG_PORT",     "5432")
        pg_db       = os.getenv("PG_DB",       "placement_portal")

        test_url = raw_url or f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"
        
        try:
            import psycopg2
            from urllib.parse import urlparse
            res = urlparse(test_url)
            # Quick connection check to see if local PostgreSQL is reachable
            conn = psycopg2.connect(
                user=res.username or pg_user,
                password=res.password or pg_password,
                host=res.hostname or pg_host,
                port=res.port or pg_port,
                database="postgres",  # connect to system DB first
                connect_timeout=1
            )
            conn.close()
            logger.info("Database: Local PostgreSQL is reachable. Using PostgreSQL.")
            db_url = test_url
        except Exception:
            db_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "placement.db")
            logger.warning(
                "Database: Local PostgreSQL unreachable. "
                "Falling back to local SQLite at %s for developer testing.", db_path
            )
            db_url = f"sqlite:///{db_path}"

    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Connection pooling configurations only apply to PostgreSQL
    if db_url.startswith("postgresql"):
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            "pool_pre_ping":  True,
            "pool_size":      5,
            "max_overflow":   10,
            "pool_recycle":   1800,
            "connect_args":   {"connect_timeout": 10},
        }
        logger.info("Database: Connection pooling options loaded for PostgreSQL.")
    else:
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        logger.info("Database: SQLite mode active (pooling disabled).")

_configure_database(app)

# ── Mail Config ──────────────────────────────────────────────────────────────
app.config['MAIL_SERVER']         = os.getenv('MAIL_SERVER', 'sandbox.smtp.mailtrap.io')
app.config['MAIL_PORT']           = int(os.getenv('MAIL_PORT', 2525))
app.config['MAIL_USE_TLS']        = os.getenv('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USE_SSL']        = os.getenv('MAIL_USE_SSL', 'False') == 'True'
app.config['MAIL_USERNAME']       = os.getenv('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD']       = os.getenv('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', 'noreply@placement.edu')

# ── Initialize Extensions ────────────────────────────────────────────────────
db.init_app(app)
mail.init_app(app)
migrate = Migrate(app, db)          # Flask-Migrate for schema migrations

# ── Google OAuth ─────────────────────────────────────────────────────────────
oauth  = OAuth(app)
google = oauth.register(
    name='google',
    client_id=app.config['GOOGLE_CLIENT_ID'],
    client_secret=app.config['GOOGLE_CLIENT_SECRET'],
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# ── Flask-Login ───────────────────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.login_view          = 'auth.login'
login_manager.login_message_category = 'warning'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return Student.query.get(int(user_id))

# ── Context Processor ─────────────────────────────────────────────────────────
@app.context_processor
def inject_db_info():
    unread_messages_count = 0
    if current_user.is_authenticated and not current_user.is_admin:
        unread_messages_count = Message.query.filter_by(
            student_id=current_user.id,
            is_read=False
        ).count()
    return dict(
        using_sqlite=app.config['SQLALCHEMY_DATABASE_URI'].startswith("sqlite"),
        unread_messages_count=unread_messages_count,
    )

# ── Google Login Routes ───────────────────────────────────────────────────────
@app.route('/google/login')
def google_login():
    redirect_uri = url_for('google_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/google/callback')
def google_callback():
    token     = google.authorize_access_token()
    user_info = token.get('userinfo')
    email     = user_info['email']
    name      = user_info['name']

    user = Student.query.filter_by(email=email).first()
    if not user:
        user = Student(
            name=name,
            email=email,
            password_hash='google_oauth',
            auth_provider='google',
        )
        db.session.add(user)
        db.session.commit()

    login_user(user)
    session['google_user'] = user_info
    return redirect(url_for('student.dashboard'))

# ── Blueprint Registration ────────────────────────────────────────────────────
from blueprints.auth     import auth_bp
from blueprints.student  import student_bp
from blueprints.admin    import admin_bp
from blueprints.linkedin import linkedin_bp

app.register_blueprint(auth_bp)
app.register_blueprint(student_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(linkedin_bp)

# ── Database Health Check & Startup ──────────────────────────────────────────
def _verify_db_connection():
    """Quick ping to confirm the DB is reachable before serving traffic."""
    from sqlalchemy import text
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database health check: OK (PostgreSQL is reachable).")
        return True
    except Exception as exc:
        logger.error("Database health check FAILED: %s", exc)
        return False


with app.app_context():
    try:
        # Create tables that don't exist yet (safe no-op for existing tables)
        db.create_all()
        logger.info("Database tables verified/created via db.create_all().")

        # Confirm connectivity
        if not _verify_db_connection():
            logger.warning(
                "Database unreachable at startup — application may not function correctly."
            )

        # ── Immediate startup sync ────────────────────────────────────────────
        # Run one sync right away so the DB is populated immediately on first
        # deploy, rather than waiting up to 59 minutes for the first cron tick.
        try:
            from services.job_api import JobSyncService, InternshipSyncService
            logger.info("STARTUP: Running initial JobSyncService.sync()...")
            JobSyncService.sync()
            logger.info("STARTUP: Running initial InternshipSyncService.sync()...")
            InternshipSyncService.sync()
            logger.info("STARTUP: Initial sync complete.")
        except Exception as sync_exc:
            logger.exception("STARTUP: Initial sync failed (non-fatal) — %s", sync_exc)

        # Start background sync scheduler
        from services.scheduler_service import init_scheduler
        init_scheduler(app)

        logger.info("Application startup complete. PostgreSQL backend active.")

    except Exception as exc:
        logger.critical("CRITICAL: Startup failed — %s", exc, exc_info=True)

# ── Root Route ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin.admin_dashboard'))
        return redirect(url_for('student.dashboard'))
    return redirect(url_for('auth.login'))

# ── Run Server (development only) ─────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)