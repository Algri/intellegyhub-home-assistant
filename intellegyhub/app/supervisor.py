from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request


SUPERVISOR_SHUTDOWN_URL = "http://supervisor/host/shutdown"


class SupervisorShutdownError(RuntimeError):
    pass


async def shutdown_host(force: bool = False) -> None:
    await asyncio.to_thread(_shutdown_host_sync, force)


def _shutdown_host_sync(force: bool) -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise SupervisorShutdownError("SUPERVISOR_TOKEN is not available")

    payload = json.dumps({"force": force}).encode("utf-8")
    request = urllib.request.Request(
        SUPERVISOR_SHUTDOWN_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise SupervisorShutdownError(f"Supervisor shutdown failed: HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise SupervisorShutdownError(f"Supervisor shutdown failed: HTTP {exc.code}") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise SupervisorShutdownError(f"Supervisor shutdown failed: {exc}") from exc
