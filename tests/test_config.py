"""Tests for lode.config — the typed settings module (lode-txh.3).

Asserts the acceptance criteria: every knob has a kind tag (runtime/tune/build),
documented defaults load, and invalid values fail validation at load. Also covers
the single-root on-disk layout under ``$LODE_HOME`` (lode-qd9), including the
model-weights cache directory (lode-gmo).
"""

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from lode.config import (
    Kind,
    Settings,
    config_path,
    default_db_path,
    knob_kinds,
    lance_dir,
    load_settings,
    lode_home,
    log_dir,
    model_cache_dir,
)

VALID_KINDS = {k.value for k in Kind}


def test_every_knob_has_a_valid_kind_tag() -> None:
    kinds = knob_kinds()
    # Every declared field is tagged, and every tag is one of runtime/tune/build.
    assert set(kinds) == set(Settings.model_fields)
    assert all(kind in VALID_KINDS for kind in kinds.values())


def test_documented_defaults_load() -> None:
    s = load_settings()
    assert s.retrieval_top_k == 20
    assert s.rrf_k == 60
    assert s.rerank_enabled is True
    assert s.drawdown_hop_limit == 1
    assert s.fetch_timeout_s == 10.0
    assert s.fetch_max_redirects == 5
    assert s.fetch_min_extract_chars == 200
    assert s.content_hash == "xxh3-128"
    assert s.no_egress_default is False


# --- load_settings() reads config.toml (lode-40g) ----------------------------
#
# load_settings() previously did nothing but ``Settings(**overrides)`` -- it
# never read $LODE_HOME/config.toml at all, so wiring the CLI to call it (the
# original scope of lode-40g) would have changed no observable behavior. These
# cover the loader itself; tests/test_cli.py's
# test_work_honors_config_file_refresh_ttl_s_end_to_end covers a caller
# actually seeing the override reach real behavior, not just that Settings
# parses it.


def test_load_settings_reads_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("retrieval_top_k = 5\n", encoding="utf-8")
    assert load_settings().retrieval_top_k == 5


def test_load_settings_with_no_config_toml_uses_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    assert not (tmp_path / "config.toml").exists()
    assert load_settings().retrieval_top_k == 20


def test_load_settings_explicit_override_wins_over_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("retrieval_top_k = 5\n", encoding="utf-8")
    assert load_settings(retrieval_top_k=9).retrieval_top_k == 9


def test_load_settings_config_toml_invalid_value_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("retrieval_top_k = 0\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings()


def test_load_settings_config_toml_unknown_key_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("not_a_real_knob = 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings()


def test_model_ids_are_pinned() -> None:
    s = Settings()
    assert s.enrichment_llm == "claude-haiku-4-5"
    assert s.qa_llm == "claude-sonnet-4-6"
    assert s.qa_think_harder_llm == "claude-opus-4-8"


def test_local_model_ids_and_dim_are_pinned() -> None:
    # lode-txh.6: embedder + vector dim, reranker, NLI model + loader pinned to
    # fastembed-loadable ids (load verified in tests/test_models_smoke.py).
    s = Settings()
    assert s.embedding_model == "nomic-ai/nomic-embed-text-v1.5"
    assert s.embedding_vector_dim == 768
    assert s.rerank_model == "BAAI/bge-reranker-base"
    assert s.entailment_model == "BAAI/bge-reranker-base"
    assert s.entailment_loader == "fastembed-cross-encoder"


def test_entailment_gate_ships_fail_closed() -> None:
    assert Settings().entailment_threshold == 0.9


# --- On-disk layout under $LODE_HOME (lode-qd9) -----------------------------


def test_lode_home_defaults_to_dot_lode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LODE_HOME", raising=False)
    assert lode_home() == Path.home() / ".lode"


def test_lode_home_honours_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path / "root"))
    assert lode_home() == tmp_path / "root"


def test_lode_home_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODE_HOME", "~/custom-lode")
    assert lode_home() == Path.home() / "custom-lode"


def test_layout_lives_under_one_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("LODE_HOME", str(root))
    db = default_db_path()
    assert db == root / "lode.db"
    # lancedb sits beside the DB, logs + the optional config under the root —
    # one inspectable tree.
    assert lance_dir(db) == root / "lancedb"
    assert log_dir() == root / "logs"
    assert config_path() == root / "config.toml"
    assert model_cache_dir() == root / "models"


def test_lance_dir_follows_an_explicit_db_override(tmp_path: Path) -> None:
    # --db relocates the DB file; the vector store co-locates beside the chosen DB
    # so capture and retrieval still share one store.
    db = tmp_path / "elsewhere" / "custom.db"
    assert lance_dir(db) == tmp_path / "elsewhere" / "lancedb"


def test_model_cache_dir_defaults_under_home_not_tempdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # lode-gmo: fastembed's own default (tempfile.gettempdir()/fastembed_cache) is
    # wiped on reboot by WSL/systemd-tmpfiles, so the pinned weights must resolve
    # under the durable root instead. The $LODE_HOME-relative case is covered by
    # test_layout_lives_under_one_root; this pins the *un-configured* default,
    # which must still land under the user's home, not a wipeable tempdir.
    monkeypatch.delenv("LODE_HOME", raising=False)
    cache_dir = model_cache_dir()
    assert cache_dir == Path.home() / ".lode" / "models"
    assert not str(cache_dir).startswith(tempfile.gettempdir())


@pytest.mark.parametrize(
    "overrides",
    [
        {"retrieval_top_k": 0},  # gt=0
        {"rrf_k": -1},  # gt=0
        {"entailment_threshold": 1.5},  # le=1.0
        {"entailment_threshold": -0.1},  # ge=0.0
        {"drawdown_hop_limit": -1},  # ge=0
        {"fetch_timeout_s": 0},  # gt=0.0
        {"fetch_max_redirects": -1},  # ge=0
        {"fetch_min_extract_chars": -1},  # ge=0
        {"retry_max_attempts": 0},  # ge=1
        {"unknown_knob": 1},  # extra="forbid"
    ],
)
def test_invalid_values_fail_at_load(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        load_settings(**overrides)
