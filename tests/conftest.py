"""Disable CSRF for the request-level tests; TestCsrf re-enables it explicitly."""
import os

os.environ.setdefault("FLASK_ENV", "test")
os.environ.setdefault("WTF_CSRF_ENABLED", "0")


def pytest_configure():
    try:
        import app
        app.app.config["WTF_CSRF_ENABLED"] = False
    except Exception:
        pass
