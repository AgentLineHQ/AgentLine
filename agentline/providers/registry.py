"""Resolve the telephony provider from configuration or an explicit registration."""

import importlib
import inspect
import logging

from agentline.config import settings

logger = logging.getLogger(__name__)

BUILTIN_TELEPHONY = ("signalwire", "twilio", "plivo", "telnyx")

_factories: dict = {}


def register_telephony(name: str, factory) -> None:
    """Register a provider class, zero-arg factory, or instance under ``name``."""
    _factories[name] = factory
    logger.info("Registered telephony provider '%s'", name)


def registered_telephony() -> list[str]:
    names = list(BUILTIN_TELEPHONY)
    for name in _factories:
        if name not in names:
            names.append(name)
    return names


def _instantiate(factory):
    if isinstance(factory, type) or inspect.isfunction(factory):
        return factory()
    return factory


def _builtin(name: str):
    if name == "signalwire":
        from agentline.providers.signalwire import SignalWireProvider
        return SignalWireProvider()
    if name == "twilio":
        from agentline.providers.twilio import TwilioProvider
        return TwilioProvider()
    if name == "plivo":
        from agentline.providers.plivo import PlivoProvider
        return PlivoProvider()
    if name == "telnyx":
        from agentline.providers.telnyx import TelnyxProvider
        return TelnyxProvider()
    return None


def load_attr(path: str):
    """Import ``package.module:Attr`` or ``package.module.Attr`` without calling it."""
    if ":" in path:
        module_name, attr = path.split(":", 1)
    else:
        module_name, attr = path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def load_object(path: str):
    """Import a class or zero-arg factory and build one instance."""
    return _instantiate(load_attr(path))


def get_provider(name: str | None = None):
    """Return a provider instance.

    ``name`` may be a built-in id, a name passed to ``register_telephony``,
    or a ``module:Class`` path. Omit it to use ``TELEPHONY_PROVIDER``.
    """
    key = (name or settings.TELEPHONY_PROVIDER or "signalwire").strip()
    if key in _factories:
        return _instantiate(_factories[key])
    builtin = _builtin(key)
    if builtin is not None:
        return builtin
    if "." in key or ":" in key:
        return load_object(key)
    known = ", ".join(registered_telephony())
    raise RuntimeError(
        f"Unknown telephony provider '{key}'. "
        f"Use one of: {known}. Or set TELEPHONY_PROVIDER to module:Class."
    )


def get_telephony():
    """Return the provider selected by ``TELEPHONY_PROVIDER``."""
    return get_provider(settings.TELEPHONY_PROVIDER)
