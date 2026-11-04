"""Shared WebSocket transport rules for speech providers."""

import logging

import websockets
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from voice_bridge.providers.base import ProviderError

# websockets 15 otherwise reads OS proxy settings; 14 has no proxy argument.
CONNECT_OPTIONS: dict = {"proxy": None} if int(websockets.__version__.split(".")[0]) >= 15 else {}

# DEBUG wire logs include Authorization, audio and recognized text.
WIRE_LOGGER = logging.getLogger("voice_bridge.providers.wire")
WIRE_LOGGER.setLevel(logging.WARNING)


class DirectConnect(connect):
    """Never forward the bearer key to a redirect target."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


def handshake_error(exc: InvalidStatus) -> ProviderError:
    """Classify rejections without retaining the HTTP body, headers or reason text."""
    status = exc.response.status_code
    if status in {401, 403}:
        code, message = "upstream_auth_error", "Speech recognition authentication failed."
    elif status == 429:
        code, message = "upstream_rate_limited", "Speech recognition is at capacity."
    elif status in {500, 502, 503, 504}:
        code, message = "upstream_unavailable", "Speech recognition is temporarily unavailable."
    else:
        code, message = "upstream_rejected", "Speech recognition rejected the connection."
    return ProviderError(code, message, upstream_code=f"HTTP_{status}")
