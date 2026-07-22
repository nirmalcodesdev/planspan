import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "planspan"))


def test_scrub_off_by_default(monkeypatch):
    monkeypatch.delenv("SCRUB_LITERALS", raising=False)
    import importlib
    import scrub
    importlib.reload(scrub)
    text = "(email = 'user@example.com')"
    assert scrub.scrub(text) == text


def test_scrub_redacts_quoted_literals(monkeypatch):
    monkeypatch.setenv("SCRUB_LITERALS", "true")
    import importlib
    import scrub
    importlib.reload(scrub)
    text = "((orders.email)::text = 'user9000@example.com'::text)"
    assert scrub.scrub(text) == "((orders.email)::text = '***'::text)"


def test_scrub_handles_multiple_literals(monkeypatch):
    monkeypatch.setenv("SCRUB_LITERALS", "true")
    import importlib
    import scrub
    importlib.reload(scrub)
    text = "status = 'paid' AND email = 'x@y.com'"
    assert scrub.scrub(text) == "status = '***' AND email = '***'"


def test_scrub_none_passthrough(monkeypatch):
    monkeypatch.setenv("SCRUB_LITERALS", "true")
    import importlib
    import scrub
    importlib.reload(scrub)
    assert scrub.scrub(None) is None
