# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in AgentLine, please report it responsibly.

**⚠️ Do NOT open a public GitHub issue for security vulnerabilities.**

### How to Report

Email **support@agentline.cloud** with:

1. A description of the vulnerability
2. Steps to reproduce it
3. The potential impact
4. Any suggested fixes (optional)

### What to Expect

- **Acknowledgment** within 48 hours
- **Assessment** within 1 week
- **Fix and disclosure** timeline agreed upon together

### Scope

The following are in scope:

- AgentLine API server (`agentline/` codebase)
- Authentication and API key handling
- Webhook signature verification
- Database queries (SQL injection)
- Voice pipeline security

### Out of Scope

- Third-party services (SignalWire, Deepgram, Cartesia, OpenAI)
- The hosted version at agentline.cloud (report separately to support@agentline.cloud)
- Social engineering attacks

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | ✅ |
| Older versions | ❌ |

Only the latest version on `main` receives security updates.

## Security Best Practices for Self-Hosting

1. **Never commit `.env` files** — they contain secrets
2. **Rotate API keys** regularly
3. **Use HTTPS** in production (`BASE_URL` should start with `https://`)
4. **Set strong values** for `SECRET_KEY` and `WEBHOOK_SECRET_SALT`
5. **Restrict CORS** — tighten `allow_origins` in `main.py` for production
