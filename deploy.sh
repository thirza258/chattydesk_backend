#!/usr/bin/env bash
#
# ChattyDesk backend deploy script.
#
#   ./deploy.sh                 install deps, collect static, migrate, serve
#   ./deploy.sh --no-serve      release steps only (Heroku/Railway release phase)
#   ./deploy.sh --serve-only    skip the release steps and just boot gunicorn
#   ./deploy.sh --skip-install  reuse the already-installed dependencies
#
# Environment comes from the process env first and .env second, so a platform's
# config vars always win over a checked-out .env file.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

RUN_INSTALL=1
RUN_STATIC=1
RUN_MIGRATE=1
RUN_SERVE=1

for arg in "$@"; do
    case "$arg" in
        --skip-install) RUN_INSTALL=0 ;;
        --skip-static)  RUN_STATIC=0 ;;
        --skip-migrate) RUN_MIGRATE=0 ;;
        --no-serve)     RUN_SERVE=0 ;;
        --serve-only)   RUN_INSTALL=0; RUN_STATIC=0; RUN_MIGRATE=0 ;;
        -h|--help)      sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. Load .env without clobbering variables the platform already set
# --------------------------------------------------------------------------- #
if [ -f .env ]; then
    log "Loading .env"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        key="${line%%=*}"
        [ "$key" = "$line" ] && continue          # no '=' on the line
        value="${line#*=}"

        key="${key#export }"
        key="$(printf '%s' "$key" | tr -d '[:space:]')"

        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;   # "quoted value"
            \'*\') value="${value#\'}"; value="${value%\'}" ;;   # 'quoted value'
            *' #'*) value="${value%% #*}" ;;                     # trailing comment
        esac

        # Process/platform env always wins over the file.
        if [ -z "${!key:-}" ]; then
            export "$key=$value"
        fi
    done < .env
fi

# --------------------------------------------------------------------------- #
# 2. Pick a Python interpreter (local venv if one is lying around)
# --------------------------------------------------------------------------- #
if [ -n "${VIRTUAL_ENV:-}" ]; then
    PYTHON="${VIRTUAL_ENV}/bin/python"
elif [ -x env/bin/python ]; then
    PYTHON="env/bin/python"
elif [ -x venv/bin/python ]; then
    PYTHON="venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    die "No python interpreter found."
fi
log "Using $($PYTHON -V 2>&1) at $PYTHON"

# --------------------------------------------------------------------------- #
# 3. Validate configuration before doing any work
# --------------------------------------------------------------------------- #
log "Checking environment"
[ -n "${SECRET_KEY:-}" ] || die "SECRET_KEY is not set (see .env.example)."

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    warn "OPENROUTER_API_KEY is not set — /models will work but chat requests will fail."
else
    echo "  OPENROUTER_API_KEY ....... set"
fi
echo "  OPENROUTER_DEFAULT_MODEL . ${OPENROUTER_DEFAULT_MODEL:-openai/gpt-4o-mini}"
echo "  DEBUG .................... ${DEBUG:-False}"
echo "  DJANGO_ALLOWED_HOSTS ..... ${DJANGO_ALLOWED_HOSTS:-127.0.0.1,localhost}"

if [ "${DEVELOPMENT_MODE:-False}" = "True" ]; then
    echo "  Database ................. sqlite (DEVELOPMENT_MODE=True)"
else
    [ -n "${DATABASE_URL:-}" ] || die "DEVELOPMENT_MODE is not True, so DATABASE_URL is required."
    echo "  Database ................. postgres via DATABASE_URL"
fi

if [ "${DEBUG:-False}" = "True" ] && [ "$RUN_SERVE" = "1" ]; then
    warn "DEBUG=True while serving with gunicorn — do not do this in production."
fi

# --------------------------------------------------------------------------- #
# 4. Dependencies
# --------------------------------------------------------------------------- #
if [ "$RUN_INSTALL" = "1" ]; then
    log "Installing dependencies"
    "$PYTHON" -m pip install --upgrade pip --quiet
    "$PYTHON" -m pip install -r requirements.txt --quiet
fi

# --------------------------------------------------------------------------- #
# 5. Static files — runs before the DATABASE_URL-dependent steps on purpose:
#    settings.py skips database configuration for the collectstatic command.
# --------------------------------------------------------------------------- #
if [ "$RUN_STATIC" = "1" ]; then
    log "Collecting static files"
    "$PYTHON" manage.py collectstatic --noinput
fi

# --------------------------------------------------------------------------- #
# 6. Database migrations
# --------------------------------------------------------------------------- #
if [ "$RUN_MIGRATE" = "1" ]; then
    log "Applying migrations"
    "$PYTHON" manage.py migrate --noinput
fi

# --------------------------------------------------------------------------- #
# 7. Deployment checklist (advisory only — never blocks a release)
# --------------------------------------------------------------------------- #
log "Running deployment checks"
"$PYTHON" manage.py check --deploy || warn "manage.py check --deploy reported issues (see above)."

# --------------------------------------------------------------------------- #
# 8. Serve
# --------------------------------------------------------------------------- #
if [ "$RUN_SERVE" = "1" ]; then
    PORT="${PORT:-8000}"
    WEB_CONCURRENCY="${WEB_CONCURRENCY:-3}"
    # Inference calls are slow; the timeout must clear OPENROUTER_TIMEOUT.
    GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-180}"

    log "Starting gunicorn on 0.0.0.0:${PORT} (${WEB_CONCURRENCY} workers)"
    exec "$PYTHON" -m gunicorn chattydesk.wsgi:application \
        --bind "0.0.0.0:${PORT}" \
        --workers "$WEB_CONCURRENCY" \
        --timeout "$GUNICORN_TIMEOUT" \
        --access-logfile - \
        --error-logfile -
else
    log "Release steps finished (--no-serve)."
fi
