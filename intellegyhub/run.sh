#!/usr/bin/with-contenv sh
set -e

APP_VERSION="$(python -c 'from app.carrier import APP_VERSION; print(APP_VERSION)')"
printf '%s\n' '-----------------------------------------------------------'
printf ' App: IntellegyHUB\n App version: %s\n' "$APP_VERSION"
printf '%s\n' '-----------------------------------------------------------'

python -m app.integration_installer

exec uvicorn app.main:app --host 0.0.0.0 --port 8098 --no-access-log
