"""Telephony provider contract.

A provider buys numbers, places calls, and renders the XML (or equivalent)
that connects the call to a voice runtime. Implement the methods below and
register the class:

    from agentline.providers import register_telephony
    register_telephony("acme", AcmeProvider)

or set ``TELEPHONY_PROVIDER=myapp.acme:AcmeProvider``.
"""

from dataclasses import dataclass


def xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def public_ws_url(path: str) -> str:
    """Turn BASE_URL plus a path into a websocket URL."""
    from agentline.config import settings

    base = settings.base_url_clean
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = f"wss://{base}"
    if not path.startswith("/"):
        path = "/" + path
    return f"{ws_base}{path}"


def public_http_url(path: str) -> str:
    from agentline.config import settings

    if not path.startswith("/"):
        path = "/" + path
    return f"{settings.base_url_clean}{path}"


@dataclass
class ProvisionedNumber:
    provider_id: str
    phone_number: str


@dataclass
class SmsResult:
    provider_message_id: str
    status: str


class LamlMedia:
    """TwiML / LaML documents shared by SignalWire, Twilio, and Telnyx TeXML.

    ``prefix`` is the webhook mount, for example ``signalwire``.
    ``media`` is the audio framing name the voice pipeline uses.
    """

    def __init__(self, prefix: str, media: str = "signalwire"):
        self.prefix = prefix.strip("/")
        self.media = media

    def stream_xml(self, call_id: str) -> str:
        url = public_ws_url(f"/{self.prefix}/stream/{call_id}")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{xml_escape(url)}" />
    </Connect>
</Response>"""

    def sip_dial_xml(self, sip_uri: str, headers: dict[str, str] | None = None) -> str:
        header_xml = ""
        if headers:
            header_xml = "".join(
                f'<Header name="{xml_escape(key)}" value="{xml_escape(value)}" />'
                for key, value in headers.items()
            )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Sip>{xml_escape(sip_uri)}{header_xml}</Sip>
    </Dial>
</Response>"""

    def say_xml(self, text: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Say>{xml_escape(text)}</Say></Response>"
        )

    def answer_url(self, call_id: str) -> str:
        return public_http_url(f"/{self.prefix}/answer/{call_id}")

    def hangup_url(self, call_id: str) -> str:
        return public_http_url(f"/{self.prefix}/hangup/{call_id}")

    def inbound_url(self) -> str:
        return public_http_url(f"/{self.prefix}/inbound")

    def inbound_hangup_url(self) -> str:
        return public_http_url(f"/{self.prefix}/inbound_hangup")

    def sms_url(self) -> str:
        return public_http_url(f"/{self.prefix}/sms")
