"""Shared Django settings — brain server (vendored-core build).

Vendored from mcp-api-starter-template: apps/core (endpoint registry,
REST + MCP bridge, security middleware), apps/api_keys, apps/mcp_proxy,
apps/docs. Everything SaaS-shaped (accounts/allauth, billing, plans,
oauth, notifications, observability app) is intentionally absent —
see my-brain-web-app/docs/PLAN.md §1.

Env vars are loaded by `config.settings.env::Settings` (pydantic-settings);
unused template env fields are inert.
"""
from pathlib import Path

from config.logging import build_logging

from .env import settings as _env

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ----- Core --------------------------------------------------------------------
# The vendored env schema defaults APP_NAME to the scaffold token
# "{{APP_NAME}}", which is truthy — so an install that never set it would
# render the literal token in every browser tab. Now that this value is
# also the fallback behind the /ops/settings/ override, reject it here.
_app_name_env = (_env.APP_NAME or "").strip()
if _app_name_env.startswith("{{"):
    _app_name_env = ""
APP_NAME = _app_name_env or "BrainOutside"
SECRET_KEY = _env.SECRET_KEY.get_secret_value()
DEBUG = False  # overridden in dev.py
ALLOWED_HOSTS: list[str] = list(_env.ALLOWED_HOSTS)

# ----- Apps --------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_q",
    # Vendored framework apps
    "apps.core",
    "apps.api_keys",
    # The `/mcp/k/<token>/` credential. Separate app from api_keys because
    # it is a separate credential: it expires by default, it is scrubbed
    # out of every log, and revoking it does not touch your bearer keys.
    "apps.url_mcp_tokens",
    "apps.mcp_proxy",
    "apps.docs",
    # Brain apps
    "apps.brain",
    "apps.events",
    "apps.mind",
    "apps.brainconfig",
    "apps.reader",
    "apps.feeds",
]

MIDDLEWARE = [
    # Outermost of all: publishes `_scrubbed_path` + `_url_token_plain`
    # for `/mcp/k/<mcpurl_...>/` requests before any inner middleware can
    # read a path, and before the first log line of the request. Does not
    # rewrite `request.path` — see the class docstring; that would 404 the
    # route it protects.
    "apps.core.security.log_scrub.URLTokenScrubMiddleware",
    # Then request_id, bound before anything logs.
    "apps.core.middleware.RequestIdMiddleware",
    # Scanner honeypot: generic 404 for /wp-admin, /.env, etc.
    "apps.core.security.HoneypotMiddleware",
    # Admin-prefix IP allowlist (404, not 403, outside the list).
    "apps.core.security.AdminIPAllowlistMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # CSP + Permissions-Policy on top of Django's security headers.
    "apps.core.security.SecurityHeadersMiddleware",
    # Admin-login brute-force lockout (per-IP, pre-auth).
    "apps.core.security.auth_signals.AdminLoginThrottleMiddleware",
    # whitenoise via our async-capable adapter: upstream's middleware is
    # sync-only, and one sync middleware here forces Django to run the
    # entire inner chain through async_to_sync (see the adapter docstring).
    "apps.core.middleware.AsyncWhiteNoiseMiddleware",
    # NOT the stock GZipMiddleware: Django's async streaming path gzips
    # every chunk as its own gzip member and Chromium truncates at the
    # first member boundary — the chat SSE stream rendered one delta and
    # dropped the rest. The subclass skips async streaming responses.
    "apps.core.middleware.StreamSafeGZipMiddleware",
    "django.middleware.http.ConditionalGetMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # 503 + Retry-After while the operator has maintenance mode on. Sits
    # here because `request.user.is_staff` is the bypass condition, so it
    # cannot run before AuthenticationMiddleware — and because a request
    # that is going to 503 should not first go through message storage,
    # clickjacking headers and the setup redirect.
    #
    # It was written, exported from `apps.core.security`, given a flag
    # store with a DB-backed cache, a branded template and an audit
    # call — and never listed here. Flipping the flag did nothing at all.
    "apps.core.security.MaintenanceModeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Sends an unowned or unfinished install to /setup/. After auth, since
    # it needs request.user to know whether the operator is the one asking.
    "apps.brainconfig.middleware.SetupRequiredMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.app_meta",
                "config.context_processors.csp_nonce",
            ],
        },
    },
]

# ----- Database ----------------------------------------------------------------
# SQLite for dev; prod.py switches to Postgres via DATABASE_URL.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "CONN_MAX_AGE": _env.DB_CONN_MAX_AGE,
        "OPTIONS": {"timeout": 5},
    }
}

# ----- Auth --------------------------------------------------------------------
# Django's default auth.User — a single human operator. The vendored
# api_keys app FKs settings.AUTH_USER_MODEL, which resolves to auth.User here.
# Custom branded login page at /login/ (templates/login.html); the Django
# admin at DJANGO_ADMIN_URL_PATH keeps its own login as a fallback.
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/" + (_env.ADMIN_PANEL_URL_PATH or "ops/").strip("/") + "/"
LOGOUT_REDIRECT_URL = "/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ----- I18N + TZ ---------------------------------------------------------------
LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

# ----- Static ------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "apps.core.staticfiles_storage.TolerantManifestStaticFilesStorage",
    },
}
WHITENOISE_MAX_AGE = 60 * 60 * 24

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ----- Security headers --------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # HTMX reads the CSRF cookie via JS.
CSRF_COOKIE_SAMESITE = "Lax"

CSP_REPORT_ONLY = False if _env.CSP_REPORT_ONLY is None else _env.CSP_REPORT_ONLY
CSP_REPORT_URI = "/_csp-report/"

SECURITY_TXT_CONTACT = _env.SECURITY_TXT_CONTACT
SECURITY_TXT_POLICY_URL = _env.SECURITY_TXT_POLICY_URL
SECURITY_TXT_ENCRYPTION_URL = _env.SECURITY_TXT_ENCRYPTION_URL
SECURITY_TXT_EXPIRES_DAYS = _env.SECURITY_TXT_EXPIRES_DAYS

DATA_UPLOAD_MAX_MEMORY_SIZE = _env.DATA_UPLOAD_MAX_MEMORY_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = _env.FILE_UPLOAD_MAX_MEMORY_MB * 1024 * 1024

# ----- Admin URL paths ---------------------------------------------------------
ADMIN_PANEL_URL_PATH = _env.ADMIN_PANEL_URL_PATH  # read by vendored security mw
DJANGO_ADMIN_URL_PATH = _env.DJANGO_ADMIN_URL_PATH
ADMIN_IP_ALLOWLIST = list(_env.ADMIN_IP_ALLOWLIST)
# Non-admin prefixes pulled inside the same IP-allowlist perimeter.
# Docs are staff-only and must 404 (not redirect) for outsiders; the
# login page is a door that shouldn't exist for the open internet at
# all (nothing-public decision). `setup/` is the most sensitive of the
# three before an account exists — it is where the first operator
# account gets created — so it belongs inside the perimeter too.
IP_ALLOWLIST_EXTRA_PREFIXES = ["docs/", "login/", "logout/", "setup/"]

# ----- Real client IP behind a reverse proxy -----------------------------------
# Read by `apps.core.security.client_ip`, which every per-IP control now
# resolves through. Empty = trust nothing, use REMOTE_ADDR (the default,
# and correct for a direct-to-origin deploy).
TRUSTED_PROXY_IP_HEADER = _env.TRUSTED_PROXY_IP_HEADER.strip()
TRUSTED_PROXY_IPS = list(_env.TRUSTED_PROXY_IPS)

# Backstop limit for anonymous callers (apps.mind.throttle). Only
# meaningful once the setting above resolves real callers — otherwise
# every anonymous caller shares the proxy's bucket.
ANONYMOUS_RATE_LIMIT_PER_MIN = _env.ANONYMOUS_RATE_LIMIT_PER_MIN

DEV_LOGIN_ENABLED = _env.DEV_LOGIN_ENABLED
DEV_LOGIN_EMAIL = _env.DEV_LOGIN_EMAIL

# ----- Email (console only — no transactional email in this app) ---------------
EMAIL_BACKEND_DRIVER = "console"
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = _env.DEFAULT_FROM_EMAIL

# ----- MCP subprocess ----------------------------------------------------------
MCP_LOOPBACK_HOST = _env.MCP_LOOPBACK_HOST
MCP_LOOPBACK_PORT = _env.MCP_LOOPBACK_PORT
MCP_LOOPBACK_SECRET = (
    _env.MCP_LOOPBACK_SECRET.get_secret_value() if _env.MCP_LOOPBACK_SECRET else ""
)
# URL-path MCP tokens. Env-driven and off by default — see the field in
# env.py for why on is not a resting state. Off means the proxy 404s
# `/mcp/k/...`, so the surface is invisible rather than merely closed.
MCP_URL_AUTH_ENABLED = _env.MCP_URL_AUTH_ENABLED
# Expiry the mint form preselects. It was defined in env.py and never
# exported, so the ops picker read its own fallback and an operator who
# set the env var got 90 days anyway.
URL_TOKEN_DEFAULT_TTL_DAYS = _env.URL_TOKEN_DEFAULT_TTL_DAYS
# OAuth flows are not vendored; issuer is still announced in 401 hints.
OAUTH_ISSUER = _env.OAUTH_ISSUER.rstrip("/")
MCP_OAUTH_DCR_MODE = "off"

# ----- Field encryption --------------------------------------------------------
# Fernet key for AppSetting encrypted values (apps.brainconfig, M1.9).
FIELD_ENCRYPTION_KEY = _env.FIELD_ENCRYPTION_KEY.get_secret_value()

# ----- Brain repo --------------------------------------------------------------
# Empty-string env values are treated as unset (Coolify lesson).
BRAIN_REPO_URL = _env.BRAIN_REPO_URL.strip()
BRAIN_REPO_DIR = (
    Path(_env.BRAIN_REPO_DIR.strip()).resolve()
    if _env.BRAIN_REPO_DIR.strip()
    else BASE_DIR / "data" / "brain-repo"
)
BRAIN_VIEWS_DIR = (
    Path(_env.BRAIN_VIEWS_DIR.strip()).resolve()
    if _env.BRAIN_VIEWS_DIR.strip()
    else BASE_DIR / "data" / "brain-views"
)
BRAIN_GIT_SSH_KEY_PATH = _env.BRAIN_GIT_SSH_KEY_PATH.strip()
FEED_PAYLOAD_MAX_KB = _env.FEED_PAYLOAD_MAX_KB
# Every server-side agent run bills the API key. Off here; the same
# work runs in Claude Code on the owner's subscription for nothing.
AGENT_RUNS_ENABLED = _env.AGENT_RUNS_ENABLED
# No queue daemon: cron runs the schedule, approvals commit inline.
TASK_QUEUE_ENABLED = _env.TASK_QUEUE_ENABLED
BRAIN_GIT_WRITE_PAT = _env.BRAIN_GIT_WRITE_PAT
BRAIN_GIT_WRITE_PAT_PATH = _env.BRAIN_GIT_WRITE_PAT_PATH.strip()
BRAIN_COMMIT_NAME = _env.BRAIN_COMMIT_NAME.strip() or "brain-app"
BRAIN_COMMIT_EMAIL = _env.BRAIN_COMMIT_EMAIL.strip() or "brain-app@localhost"
GITHUB_WEBHOOK_SECRET = _env.GITHUB_WEBHOOK_SECRET.get_secret_value().strip()

# ----- Cache -------------------------------------------------------------------
from config.cache import build_caches  # noqa: E402

CACHES = build_caches(
    redis_url=_env.REDIS_URL,
    env=_env.CACHE_ENV,
    version=_env.CACHE_KEY_VERSION,
)

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ----- Logging -----------------------------------------------------------------
LOGGING = build_logging(debug=DEBUG)

# ----- Observability hooks (observability app not vendored) --------------------
SENTRY_DSN = ""
GOOGLE_ANALYTICS_ID = ""

# ----- django-q2 ---------------------------------------------------------------
Q_CLUSTER = {
    "name": APP_NAME,
    "workers": _env.Q_WORKER_COUNT,
    "recycle": _env.Q_RECYCLE_AFTER_TASKS,
    # retry (ack) must stay > timeout or long SDK runs double-execute
    # (and double-bill Anthropic) — validated in env.py.
    "timeout": _env.Q_TASK_TIMEOUT_SECONDS,
    "retry": _env.Q_ACK_TIMEOUT_SECONDS,
    "compress": True,
    "save_limit": 250,
    "queue_limit": 500,
    "label": "Q2",
    "redis": _env.REDIS_URL,
    "poll": 1,
    "catch_up": False,
    "sync": False,
}
