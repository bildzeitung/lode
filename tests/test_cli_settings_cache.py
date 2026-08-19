"""Tests for the per-invocation ``_resolve_settings()`` cache (lode-bsga).

``main()`` resolves settings once per invocation for ``[cli.theme]``
application (lode-mk9j) and the subcommand body then resolves again; the
cache collapses those into one ``load_settings()`` call. Covered here:

* the single-load acceptance criterion itself, counted rather than assumed;
* the reset in ``main()`` -- the whole reason the cache is safe -- proven by
  two back-to-back in-process invocations against *different* config files,
  the exact leak a module-level cache would otherwise introduce under
  ``CliRunner``;
* a *failed* resolution staying uncached, which is what keeps ``lode
  status``'s ``lode-l38d.6`` survival contract (main() swallows, the command
  body re-attempts and swallows again) working unchanged -- caching the
  failure would collapse two attempts into one and quietly rewrite that
  contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from lode import cli
from lode.config import Settings

runner = CliRunner()


@pytest.fixture
def load_spy() -> Iterator[mock.MagicMock]:
    """Count real ``lode.cli.load_settings`` calls without stubbing it out.

    ``wraps=`` (the house idiom, cf. ``tests/test_llm_provider.py``) forwards
    whatever it is given, so this cannot silently drift if
    ``load_settings``'s signature changes.
    """
    with mock.patch.object(cli, "load_settings", wraps=cli.load_settings) as spy:
        yield spy


def test_one_config_load_per_invocation(
    tmp_path: Path, load_spy: mock.MagicMock
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[cli.theme.styles]\nnote_id = "bold magenta"\n', encoding="utf-8"
    )

    # `lode config` resolves settings in its own body AND is preceded by
    # main()'s global [cli.theme] resolution -- the double load this ticket
    # exists to remove.
    result = runner.invoke(cli.app, ["config"], env={"LODE_HOME": str(home)})
    assert result.exit_code == 0, result.output
    assert load_spy.call_count == 1


def test_cache_does_not_leak_across_invocations(
    tmp_path: Path, load_spy: mock.MagicMock
) -> None:
    # Two in-process invocations, different config files: without main()'s
    # reset the second would render the first's resolved settings.
    first = tmp_path / "first"
    first.mkdir()
    (first / "config.toml").write_text("embedding_vector_dim = 111\n", encoding="utf-8")
    second = tmp_path / "second"
    second.mkdir()
    (second / "config.toml").write_text(
        "embedding_vector_dim = 222\n", encoding="utf-8"
    )

    one = runner.invoke(cli.app, ["config"], env={"LODE_HOME": str(first)})
    assert one.exit_code == 0, one.output
    assert isinstance(cli._settings_cache, Settings)
    assert cli._settings_cache.embedding_vector_dim == 111

    two = runner.invoke(cli.app, ["config"], env={"LODE_HOME": str(second)})
    assert two.exit_code == 0, two.output
    assert isinstance(cli._settings_cache, Settings)
    # The load-bearing assertion: the second invocation resolved its OWN
    # config rather than reusing the first's cached Settings.
    assert cli._settings_cache.embedding_vector_dim == 222
    # One load each, not one shared across both.
    assert load_spy.call_count == 2


def test_failed_resolution_is_not_cached(
    tmp_path: Path, load_spy: mock.MagicMock
) -> None:
    # lode-l38d.6: `lode status` survives a broken config because BOTH
    # main() and status's own body swallow the failure. Caching a failure
    # would silently change that shape; assert the two real attempts.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("embedding_model = [not valid toml\n")

    result = runner.invoke(
        cli.app,
        ["status", "--db", str(tmp_path / "lode.db")],
        env={"LODE_HOME": str(home)},
    )
    assert result.exit_code == 0, result.output
    assert load_spy.call_count == 2
    assert cli._settings_cache is None
