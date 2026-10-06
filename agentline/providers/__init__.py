"""Telephony provider plugins.

The active provider is selected with ``TELEPHONY_PROVIDER``. Built-ins are
``signalwire``, ``twilio``, ``plivo``, and ``telnyx``. A dotted path or
``module:Class`` loads your own implementation.
"""

from agentline.providers.registry import (
    get_provider,
    get_telephony,
    register_telephony,
)

__all__ = ["get_provider", "get_telephony", "register_telephony"]
