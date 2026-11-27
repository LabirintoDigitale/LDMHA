"""Ascolto dal pannello: il browser suona il file della musica (con i salti avanti e indietro). Solo per gli
amministratori e solo dentro le cartelle configurate; l'indirizzo è firmato da Home Assistant (auth/sign_path), così
il lettore audio del browser lo può aprire senza intestazioni di accesso."""

from __future__ import annotations

import base64
import mimetypes

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .library import LibraryError


def audio_key(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


class AudioView(HomeAssistantView):
    url = "/api/ldmha/audio/{key}"
    name = "api:ldmha:audio"
    requires_auth = True

    async def get(self, request: web.Request, key: str) -> web.StreamResponse:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return web.Response(status=403)
        hass = request.app["hass"]
        entry = hass.data.get(DOMAIN)
        if entry is None:
            return web.Response(status=404)
        try:
            path = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode("utf-8")
            real = await hass.async_add_executor_job(entry.runtime_data.library.check, path)
        except (ValueError, UnicodeDecodeError, LibraryError):
            return web.Response(status=404)
        ctype = mimetypes.guess_type(real)[0] or "audio/mpeg"
        return web.FileResponse(real, headers={"Content-Type": ctype, "Cache-Control": "no-store"})
