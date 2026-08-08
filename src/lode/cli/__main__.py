"""``python -m lode.cli`` entry point (lode-35nu.9: lode.cli is now a package).

Mirrors the console-script entry (``pyproject.toml``'s ``lode = "lode.cli:app"``)
so a fresh-subprocess invocation of the CLI -- used by tests that need a real
process boundary rather than ``CliRunner`` (e.g. to observe ``is_terminal``/
``NO_COLOR`` at Console-construction time) -- keeps working the same way it
did when ``lode.cli`` was a single module.
"""

from lode.cli import app

if __name__ == "__main__":  # pragma: no cover
    app()
