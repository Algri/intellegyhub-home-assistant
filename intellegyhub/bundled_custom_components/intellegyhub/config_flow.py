from __future__ import annotations

import os
from pathlib import Path

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IntellegyHubApiClient
from .const import CONF_URL, DEFAULT_URL, DOMAIN, NAME
from .discovery import backend_url_from_hint, backend_urls_from_addons_payload, normalize_url

SUPERVISOR_ADDONS_URL = "http://supervisor/addons"
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"
BACKEND_HINT_PATH = Path(__file__).with_name("backend.json")


async def _validate_backend(hass: HomeAssistant, url: str) -> None:
    client = IntellegyHubApiClient(async_get_clientsession(hass), url)
    await client.health()
    await client.state()


async def _supervisor_backend_urls(hass: HomeAssistant) -> list[str]:
    token = os.environ.get(SUPERVISOR_TOKEN_ENV)
    if not token:
        return []

    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.get(SUPERVISOR_ADDONS_URL, headers=headers, timeout=5) as response:
            if response.status != 200:
                return []
            payload = await response.json()
    except (ClientError, TimeoutError, ValueError):
        return []
    return backend_urls_from_addons_payload(payload)


async def _autodetect_backend_url(hass: HomeAssistant) -> str | None:
    candidates = [DEFAULT_URL]
    hinted_url = backend_url_from_hint(BACKEND_HINT_PATH)
    if hinted_url is not None and hinted_url not in candidates:
        candidates.insert(0, hinted_url)
    for url in await _supervisor_backend_urls(hass):
        if url not in candidates:
            candidates.append(url)

    for url in candidates:
        try:
            await _validate_backend(hass, url)
        except Exception:
            continue
        return url
    return None


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_URL])
                await _validate_backend(self.hass, url)
            except ValueError:
                errors["base"] = "invalid_url"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=NAME, data={CONF_URL: url})
        else:
            detected_url = await _autodetect_backend_url(self.hass)
            if detected_url is not None:
                return self.async_create_entry(title=NAME, data={CONF_URL: detected_url})

        schema = vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
