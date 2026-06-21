# Contributing to AgentLine

Thank you for your interest in contributing to AgentLine! This guide will help you get started.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Pull Request Process](#pull-request-process)
- [Reporting Issues](#reporting-issues)
- [Code Style](#code-style)

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold this code.

## Development Setup

### Prerequisites

- Python 3.12+
- Docker and Docker Compose (for running PostgreSQL and Redis)
- API keys for: SignalWire, Deepgram, Cartesia, OpenAI, Supabase

### Getting Started

1. **Fork and clone the repo:**

   ```bash
   git clone https://github.com/YOUR_USERNAME/AgentLine.git
   cd AgentLine
   ```

2. **Set up a virtual environment:**

   ```bash
   python -m venv venv
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**

   ```bash
   cp .env.example .env
   # Fill in your API keys
   ```

4. **Start dependencies:**

   ```bash
   docker-compose up -d postgres redis
   ```

5. **Run the API server:**

   ```bash
   uvicorn agentline.main:app --reload
   ```

6. **Verify it's working:**

   Open `http://localhost:8000/docs` to see the Swagger UI.

## Making Changes

1. Create a new branch from `main`:

   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes and test them locally.

3. Ensure your code follows the project's style (see [Code Style](#code-style)).

4. Commit with clear, descriptive messages:

   ```bash
   git commit -m "feat: add support for toll-free numbers"
   ```

   We follow [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat:` — New features
   - `fix:` — Bug fixes
   - `docs:` — Documentation changes
   - `refactor:` — Code refactoring
   - `test:` — Adding or updating tests
   - `chore:` — Maintenance tasks

## Pull Request Process

1. Push your branch and open a Pull Request against `main`.
2. Fill in the PR template with a clear description of your changes.
3. Ensure CI checks pass (linting, type checking).
4. A maintainer will review your PR. Please be patient — we're a small team.
5. Once approved, your PR will be merged.

### What Makes a Good PR

- **Focused** — One feature or fix per PR
- **Tested** — Include steps to verify your changes
- **Documented** — Update docs if you change API behavior
- **Small** — Smaller PRs are reviewed faster

## Reporting Issues

### Bug Reports

Use the [bug report template](https://github.com/agentlineHQ/AgentLine/issues/new?template=bug_report.md) and include:

- Steps to reproduce
- Expected vs. actual behavior
- Python version and OS
- Relevant logs or error messages

### Feature Requests

Use the [feature request template](https://github.com/agentlineHQ/AgentLine/issues/new?template=feature_request.md) and include:

- The problem you're trying to solve
- Your proposed solution
- Any alternatives you've considered

### Security Vulnerabilities

**Do NOT open a public issue.** See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.

## Code Style

- **Python 3.12+** with type hints on all function signatures
- **Docstrings** for all public functions and classes
- **Async/await** for all I/O operations (database, HTTP, etc.)
- **Ruff** for linting — run `ruff check .` before committing
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes

### Example

```python
async def create_agent(
    account_id: str,
    name: str,
    voice_id: str = "female-1",
) -> dict:
    """Create a new AI voice agent.

    Args:
        account_id: The account that owns this agent.
        name: Display name for the agent.
        voice_id: Cartesia voice preset or UUID.

    Returns:
        Dictionary with the created agent's details.
    """
    ...
```

---

Thank you for helping make AgentLine better! 🎉
