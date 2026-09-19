from __future__ import annotations

from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import IntellegyHubApiClient
from .const import CONF_URL, DEFAULT_URL, DOMAIN, NAME


def _normalize_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid_url")
    return value.strip().rstrip("/")


async def _validate_backend(hass: HomeAssistant, url: str) -> None:
    client = IntellegyHubApiClient(async_get_clientsession(hass), url)
    await client.health()
    await client.state()


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            try:
                url = _normalize_url(user_input[CONF_URL])
                await _validate_backend(self.hass, url)
            except ValueError:
                errors["base"] = "invalid_url"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=NAME, data={CONF_URL: url})

        schema = vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
