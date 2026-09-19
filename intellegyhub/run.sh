#!/usr/bin/with-contenv sh
set -e

if command -v pigpiod >/dev/null 2>&1; then
  echo "[intellegyhub] starting pigpiod for GPIO18 hardware PWM"
  pigpiod -g -f -n 127.0.0.1 -x 262144 &
  PIGPIOD_PID="$!"
  sleep 1
  if ! kill -0 "${PIGPIOD_PID}" 2>/dev/null; then
    echo "[intellegyhub] pigpiod exited during startup" >&2
  elif command -v pigs >/dev/null 2>&1; then
    if pigs t >/dev/null 2>&1; then
      echo "[intellegyhub] pigpiod is accepting socket commands"
    else
      echo "[intellegyhub] pigpiod is running but pigs cannot connect" >&2
    fi
  fi
else
  echo "[intellegyhub] pigpiod binary is missing" >&2
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8098
