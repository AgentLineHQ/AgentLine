"""Telephony provider plugins.

The active provider is selected with ``TELEPHONY_PROVIDER``. Built-ins are
``signalwire``, ``twilio``, ``plivo``, and ``telnyx``. A dotted path or
``module:Class`` loads your own implementation.

Several carriers can run at once. ``TelephonyRouter`` picks one per new
number (see ``agentline.providers.router``); each number keeps its carrier.
"""

from agentline.providers.registry import (
    get_provider,
    get_telephony,
    register_telephony,
)
from agentline.providers.router import RoutingError, TelephonyRouter, get_router

__all__ = [
    "RoutingError",
    "TelephonyRouter",
    "get_provider",
    "get_router",
    "get_telephony",
    "register_telephony",
]
