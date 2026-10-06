"""
AgentLine — Configuration

Loads environment variables with pydantic-settings. Telephony, speech,
and voice-runtime choices are all optional switches. Bring the accounts
you want; this process does not talk to Supabase or a hosted billing ledger.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL. Docker Compose supplies a local default.
    DATABASE_URL: str = ""

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Which carrier places calls and buys numbers.
    # signalwire | twilio | plivo | telnyx | module:Class
    TELEPHONY_PROVIDER: str = "signalwire"

    # Which voice stack handles the conversation.
    # builtin | livekit | pipecat | module:Class
    VOICE_RUNTIME: str = "builtin"

    # Built-in pipeline vendors. Ignored when VOICE_RUNTIME is livekit or pipecat,
    # unless that runtime calls back into these hooks.
    STT_PROVIDER: str = "deepgram"
    TTS_PROVIDER: str = "cartesia"
    LLM_PROVIDER: str = "openai"

    # SignalWire
    SIGNALWIRE_PROJECT_ID: str = ""
    SIGNALWIRE_TOKEN: str = ""
    SIGNALWIRE_SPACE_URL: str = ""

    # Twilio
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""

    # Plivo
    PLIVO_AUTH_ID: str = ""
    PLIVO_AUTH_TOKEN: str = ""
    PLIVO_APP_ID: str = ""

    # Telnyx. TELNYX_ACCOUNT_SID is the TeXML application id.
    # TELNYX_CONNECTION_ID attaches purchased numbers to that application.
    TELNYX_API_KEY: str = ""
    TELNYX_ACCOUNT_SID: str = ""
    TELNYX_CONNECTION_ID: str = ""

    # Voice pipeline — LLM (OpenAI-compatible)
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"

    # Voice pipeline — STT
    DEEPGRAM_API_KEY: str = ""

    # Voice pipeline — TTS
    CARTESIA_API_KEY: str = ""

    # LiveKit. Install requirements-livekit.txt when bridging audio in-process.
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""
    LIVEKIT_AGENT_NAME: str = "agentline"
    LIVEKIT_SIP_URI: str = ""

    # Pipecat. module:function that replaces the default bot.
    PIPECAT_FACTORY: str = ""

    # App
    SECRET_KEY: str = "change-me-in-production"
    BASE_URL: str = "http://localhost:8000"
    WEBHOOK_SECRET_SALT: str = "change-me-in-production"

    @property
    def base_url_clean(self) -> str:
        """BASE_URL with trailing slashes stripped to prevent double-slash URLs."""
        return self.BASE_URL.rstrip("/")

    @property
    def db_dsn(self) -> str:
        """Returns asyncpg-compatible DSN from DATABASE_URL."""
        if self.DATABASE_URL:
            url = self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
            url = url.replace("postgres://", "postgresql://")
            return url
        return "postgresql://agentline:secret@localhost:5432/agentline"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
