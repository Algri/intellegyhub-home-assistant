#!/usr/bin/with-contenv sh
set -e

python -m app.integration_installer

exec uvicorn app.main:app --host 0.0.0.0 --port 8098 --no-access-log
