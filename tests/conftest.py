"""
Pytest setup — set required env vars BEFORE app.config is imported by any test
that pulls in app modules transitively (e.g. agent_session, conversational).

Existing tests in test_config.py use monkeypatch + Settings(_env_file=None)
explicitly; this conftest only fills in defaults for the singleton path.
"""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token_for_pytest")
os.environ.setdefault("ADMIN_CHAT_IDS", "1,2,3")
