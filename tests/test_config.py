"""Tests for lode.config — the typed settings module (lode-txh.3).

Asserts the acceptance criteria: every knob has a kind tag (runtime/tune/build),
documented defaults load, and invalid values fail validation at load. Also covers
the single-root on-disk layout under ``$LODE_HOME`` (lode-qd9), including the
model-weights cache directory (lode-gmo).
"""

import tempfile
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from lode.config import (
    CONFLUENCE_EMAIL_ENV,
    CONFLUENCE_TOKEN_ENV,
    JIRA_EMAIL_ENV,
    JIRA_TOKEN_ENV,
    REDACTED_PLACEHOLDER,
    UNSET_PLACEHOLDER,
    AtlassianCredentials,
    Kind,
    Settings,
    config_lines,
    config_path,
    config_rows,
    confluence_active,
    default_db_path,
    hf_hub_offline,
    jira_active,
    knob_kinds,
    knob_rows,
    lance_dir,
    load_settings,
    lode_home,
    log_dir,
    model_cache_dir,
    resolve_confluence_credentials,
    resolve_jira_credentials,
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
    assert s.progress_heartbeat_interval_s == 15.0
    assert s.llm_call_timeout_s == 120.0
    assert s.qa_call_timeout_s == 300.0


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


# --- a None override must not clobber config.toml (lode-n8n) -----------------
#
# The natural shape of a per-knob CLI flag is a Typer option defaulting to
# None (`top_k: int | None = None`) passed straight through as **overrides.
# Since None is still a *present* key, `Settings(**{**file, **overrides})`
# would let it win over the file every time the user leaves the flag off --
# silently reverting their config.toml value to the field default. These two
# tests cover both directions of the fix.


def test_load_settings_none_override_preserves_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("retrieval_top_k = 5\n", encoding="utf-8")
    assert load_settings(retrieval_top_k=None).retrieval_top_k == 5


def test_load_settings_explicit_override_still_wins_when_other_overrides_are_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("retrieval_top_k = 5\n", encoding="utf-8")
    settings = load_settings(retrieval_top_k=9, rrf_k=None)
    assert settings.retrieval_top_k == 9
    assert settings.rrf_k == 60  # the dropped None left the default intact


def test_load_settings_unknown_override_still_raises_when_none_valued(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dropping None overrides must not punch a hole in ``extra="forbid"``.

    A typo'd override name (``top_k`` instead of ``retrieval_top_k``) carrying
    the not-supplied ``None`` must still be rejected, not silently swallowed --
    otherwise a mis-wired CLI flag would be a no-op every time the user left it
    off, and only fail once someone passed it.
    """
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    with pytest.raises(ValidationError):
        load_settings(top_k=None)


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


# --- llm_call_timeout_s back-compat rename (lode-568v.2) --------------------
#
# anthropic_call_timeout_s was renamed vendor-neutral ahead of the LLMProvider
# seam; a config.toml still carrying the old key must keep working rather than
# tripping extra="forbid" (docs/stack.md "Config shape").


def test_load_settings_remaps_old_anthropic_timeout_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "anthropic_call_timeout_s = 42.0\n", encoding="utf-8"
    )
    assert load_settings().llm_call_timeout_s == 42.0


def test_load_settings_new_key_wins_when_both_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "anthropic_call_timeout_s = 42.0\nllm_call_timeout_s = 7.0\n",
        encoding="utf-8",
    )
    assert load_settings().llm_call_timeout_s == 7.0


# --- ModelTier coercion from a bare config.toml string (lode-568v.2) --------


def test_load_settings_bare_string_model_knob_coerces_to_model_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'enrichment_llm = "custom-model"\n', encoding="utf-8"
    )
    settings = load_settings()
    assert settings.enrichment_llm.model == "custom-model"
    assert settings.enrichment_llm.reasoning_effort is None


def test_load_settings_inline_table_model_knob_sets_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'qa_think_harder_llm = { model = "gpt-5.5", reasoning_effort = "high" }\n',
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.qa_think_harder_llm.model == "gpt-5.5"
    assert settings.qa_think_harder_llm.reasoning_effort == "high"


def test_llm_provider_defaults_to_anthropic() -> None:
    assert Settings().llm_provider == "anthropic"


def test_llm_provider_accepts_openai() -> None:
    # lode-568v.3: "openai" is the second valid llm_provider value.
    assert Settings(llm_provider="openai").llm_provider == "openai"


def test_llm_provider_rejects_unsupported_value() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="azure")


# --- Azure OpenAI routing knobs (lode-568v.3) --------------------------------


def test_azure_openai_knobs_default_to_empty() -> None:
    s = Settings()
    assert s.azure_openai_endpoint == ""
    assert s.azure_openai_api_version == ""


def test_azure_openai_endpoint_requires_api_version() -> None:
    with pytest.raises(ValidationError, match="azure_openai_api_version"):
        Settings(azure_openai_endpoint="https://foo.openai.azure.com")


def test_azure_openai_endpoint_with_api_version_constructs() -> None:
    s = Settings(
        llm_provider="openai",
        azure_openai_endpoint="https://foo.openai.azure.com",
        azure_openai_api_version="2025-04-01-preview",
    )
    assert s.azure_openai_endpoint == "https://foo.openai.azure.com"
    assert s.azure_openai_api_version == "2025-04-01-preview"


def test_azure_openai_api_version_alone_is_fine() -> None:
    # Only azure_openai_endpoint presence triggers the requirement -- an
    # api_version with no endpoint is meaningless but not itself invalid.
    s = Settings(azure_openai_api_version="2025-04-01-preview")
    assert s.azure_openai_endpoint == ""


# --- reasoning_effort validated against llm_provider at load (lode-tvps) ----


@pytest.mark.parametrize(
    ("provider", "tier", "model", "effort"),
    [
        # An outright typo, on each tier in turn -- the validator must reach
        # every ModelTier knob, not just the first one.
        ("anthropic", "enrichment_llm", "claude-haiku-4-5", "bogus-effort"),
        ("anthropic", "qa_llm", "claude-haiku-4-5", "bogus-effort"),
        ("anthropic", "qa_think_harder_llm", "claude-haiku-4-5", "bogus-effort"),
        ("openai", "qa_llm", "gpt-5.5", "bogus-effort"),
        # "minimal" is a legal `reasoning.effort` value for OpenAI
        # (_OPENAI_EFFORT_LEVELS) but is absent from Anthropic's legal set
        # (_ANTHROPIC_EFFORT_LEVELS): legality is relative to the *configured*
        # provider, so being legal under some other provider is not enough.
        ("anthropic", "enrichment_llm", "claude-haiku-4-5", "minimal"),
    ],
)
def test_illegal_reasoning_effort_rejected_at_load(
    provider: str, tier: str, model: str, effort: str
) -> None:
    with pytest.raises(ValidationError, match=f"{tier}.*{effort}"):
        Settings(
            llm_provider=provider,
            **{tier: {"model": model, "reasoning_effort": effort}},
        )


@pytest.mark.parametrize(
    ("provider", "model", "effort"),
    [
        ("anthropic", "claude-sonnet-4-6", "high"),
        ("openai", "gpt-5.5", "minimal"),
    ],
)
def test_legal_reasoning_effort_constructs(
    provider: str, model: str, effort: str
) -> None:
    s = Settings(
        llm_provider=provider,
        qa_llm={"model": model, "reasoning_effort": effort},
    )
    assert s.qa_llm.reasoning_effort == effort


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_reasoning_effort_unset_is_always_fine_regardless_of_provider(
    provider: str,
) -> None:
    # Unset is the default for every tier and what a bare-string
    # `enrichment_llm = "..."` coerces to, so it must stay legal under either
    # provider -- the seam treats None as "send no effort kwarg" rather than
    # as a value needing a legal set.
    s = Settings(llm_provider=provider)
    assert [
        t.reasoning_effort for t in (s.enrichment_llm, s.qa_llm, s.qa_think_harder_llm)
    ] == [None, None, None]


def test_effort_levels_mapping_covers_every_llm_provider_value() -> None:
    # `EFFORT_LEVELS_BY_PROVIDER` is keyed by the same literal
    # `Settings.llm_provider` is declared with, and the validator subscripts it
    # unconditionally. Nothing else pins that: there is no mypy session in
    # noxfile.py, so a third provider added to the Literal (and to
    # build_provider) but not to the mapping would surface as a bare KeyError
    # raised inside a @model_validator on *every* Settings construction --
    # which `_resolve_settings` does not catch, so it prints a traceback rather
    # than the one-line config error. Same meta-test idiom the two level tuples
    # already use against their SDKs' own Literals (tests/test_llm_provider.py).
    import typing

    from lode.llm_provider import EFFORT_LEVELS_BY_PROVIDER

    declared = typing.get_args(Settings.model_fields["llm_provider"].annotation)
    assert declared, "llm_provider is no longer a Literal -- update this pin"
    assert set(EFFORT_LEVELS_BY_PROVIDER) == set(declared)


def test_load_settings_malformed_config_toml_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A TOML *syntax* error surfaces as TOMLDecodeError, not ValidationError.

    Distinct from the two cases above (both of which parse fine and then fail
    pydantic): this never reaches Settings at all. The CLI has to catch both
    kinds to keep a typo in the hand-edited file from becoming a traceback --
    see tests/test_cli.py::test_cli_reports_a_bad_config_file_without_a_traceback.
    """
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("refresh_ttl_s =\n", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_settings()


def test_model_ids_are_pinned() -> None:
    s = Settings()
    assert s.enrichment_llm.model == "claude-haiku-4-5"
    assert s.qa_llm.model == "claude-sonnet-4-6"
    assert s.qa_think_harder_llm.model == "claude-opus-5"
    # A bare string knob coerces to a ModelTier with no reasoning effort
    # (lode-568v.2 back-compat -- no migration required for existing configs).
    assert s.enrichment_llm.reasoning_effort is None
    assert s.qa_llm.reasoning_effort is None
    assert s.qa_think_harder_llm.reasoning_effort is None


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


# --- hf_hub_offline() — shared HF_HUB_OFFLINE truthiness check (lode-r4r2) --
# Moved here from a cli.py-private helper once lode.embedding needed the
# identical check (resolve_model_revision's offline short-circuit).


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_hf_hub_offline_true_for_recognized_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", value)
    assert hf_hub_offline() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_hf_hub_offline_false_for_falsy_or_empty(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", value)
    assert hf_hub_offline() is False


def test_hf_hub_offline_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    assert hf_hub_offline() is False


# --- config_rows() — the CLI's raw path-row builder (lode-l38d.4) -----------


def test_config_rows_and_config_lines_share_the_same_label_and_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # config_rows (the raw (label, value, note) triples the CLI's rich Table
    # renders) and config_lines (the pre-padded text the TUI's Static widget
    # renders) are two shapes of the ONE computation (config_rows, which
    # config_lines formats) -- this pins that they never drift apart.
    root = tmp_path / "root"
    monkeypatch.setenv("LODE_HOME", str(root))
    db = default_db_path()

    rows = config_rows(db)
    lines = config_lines(db)
    assert len(rows) == len(lines)
    for (label, value, note), line in zip(rows, lines, strict=True):
        assert line.startswith(label)
        assert value in line
        if note:
            assert f"({note})" in line


def test_config_rows_note_is_bare_no_parens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The parenthetical is a CLI-rendering decision (Table cell formatting),
    # not part of the raw data -- config_rows' own "note" field is bare, so
    # config_lines (and the CLI's rich Table) each choose whether/how to wrap
    # it in parens rather than getting it pre-baked.
    root = tmp_path / "root"
    monkeypatch.setenv("LODE_HOME", str(root))
    rows = config_rows(default_db_path())
    notes = {label: note for label, _, note in rows}
    assert notes["LODE_HOME"] == "$LODE_HOME"
    assert notes["config"] == "absent"
    # Every other row carries no annotation.
    for label, note in notes.items():
        if label not in ("LODE_HOME", "config"):
            assert note == ""


# --- knob_rows() — the shared CLI+TUI knob-table builder (lode-juz8.6) ------


def test_knob_rows_includes_only_runtime_and_tune_kinds() -> None:
    rows = knob_rows(Settings())
    names = {name for name, _, _ in rows}
    kinds = knob_kinds()
    # A field declared secret=True (the four Atlassian credential fields,
    # lode-gpzn.1 / lode-dx4r) still gets a ROW -- it renders a presence
    # indicator rather than being excluded outright (see
    # test_credential_fields_appear_in_knob_rows_as_presence_only below).
    expected_names = {
        name
        for name, kind in kinds.items()
        if kind in (Kind.RUNTIME.value, Kind.TUNE.value)
    }
    assert names == expected_names
    assert all(kind != Kind.BUILD.value for _, _, kind in rows)
    # A build-kind knob by name is excluded outright.
    assert "embedding_model" not in names
    assert "content_hash" not in names


def _knob_values(settings: Settings) -> dict[str, str]:
    """``{name: value}`` from :func:`knob_rows`, dropping the kind column."""
    return {name: value for name, value, _ in knob_rows(settings)}


def test_knob_rows_reads_current_resolved_value_not_bare_default() -> None:
    # The row shows load_settings()'s CURRENT value, not Settings()'s default --
    # the table exists to answer "what is it set to", including a config.toml
    # override.
    overridden = Settings(retrieval_top_k=42)
    rows = _knob_values(overridden)
    assert rows["retrieval_top_k"] == "42"


def test_knob_rows_renders_list_valued_knobs_comma_joined() -> None:
    rows = _knob_values(Settings())
    assert rows["url_tracking_param_blocklist"] == "utm_*, fbclid, gclid"


def test_knob_rows_renders_model_tier_knobs_as_bare_model_id() -> None:
    # lode-568v.2: the enrichment/qa model knobs became ModelTier pairs; str()
    # on a ModelTier is the pydantic repr ("model='...' reasoning_effort=None"),
    # which would leak into `lode config` + the TUI ConfigScreen (both feed
    # knob_rows straight to display). Default (no effort) shows the bare id.
    rows = _knob_values(Settings())
    assert rows["enrichment_llm"] == "claude-haiku-4-5"
    assert rows["qa_llm"] == "claude-sonnet-4-6"
    assert rows["qa_think_harder_llm"] == "claude-opus-5"


def test_knob_rows_appends_reasoning_effort_when_set() -> None:
    # When a tier carries a reasoning_effort, surface it alongside the model id
    # rather than hiding it or printing the pydantic repr.
    rows = _knob_values(
        Settings(qa_think_harder_llm={"model": "gpt-5.5", "reasoning_effort": "high"})
    )
    assert rows["qa_think_harder_llm"] == "gpt-5.5 (effort=high)"


def test_knob_rows_appends_max_tokens_when_set() -> None:
    # lode-d70n: same "only when set" rule reasoning_effort already
    # established, extended to the new max_tokens field.
    rows = _knob_values(
        Settings(qa_think_harder_llm={"model": "gpt-5.5", "max_tokens": 4096})
    )
    assert rows["qa_think_harder_llm"] == "gpt-5.5 (max_tokens=4096)"


def test_knob_rows_appends_both_effort_and_max_tokens_when_both_set() -> None:
    rows = _knob_values(
        Settings(
            qa_think_harder_llm={
                "model": "gpt-5.5",
                "reasoning_effort": "high",
                "max_tokens": 4096,
            }
        )
    )
    assert rows["qa_think_harder_llm"] == "gpt-5.5 (effort=high, max_tokens=4096)"


def test_knob_rows_works_with_bare_defaults_no_config_toml() -> None:
    # Acceptance: works with no config.toml present (shows defaults).
    rows = knob_rows(Settings())
    assert rows  # non-empty
    values = dict((name, value) for name, value, _ in rows)
    assert values["retrieval_top_k"] == "20"
    assert values["rerank_enabled"] == "True"


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
        {"progress_heartbeat_interval_s": 0},  # gt=0.0
        {"llm_call_timeout_s": 0},  # gt=0.0
        {"qa_call_timeout_s": 0},  # gt=0.0
        {"unknown_knob": 1},  # extra="forbid"
        {"jira_base_url": "not-a-url"},  # malformed base URL (lode-gpzn.1)
        {"confluence_base_url": "ftp://wrong-scheme.example"},  # non-http(s)
    ],
)
def test_invalid_values_fail_at_load(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        load_settings(**overrides)


# --- Atlassian connector config: flags, base URLs, credential resolution -----
# (lode-gpzn.1: per-product feature flags + env-primary token/secret resolution)


def test_atlassian_flags_default_off() -> None:
    s = Settings()
    assert s.jira_enabled is False
    assert s.confluence_enabled is False


def test_atlassian_base_urls_default_empty_meaning_infer() -> None:
    s = Settings()
    assert s.jira_base_url == ""
    assert s.confluence_base_url == ""


def test_atlassian_base_url_accepts_well_formed_http_url() -> None:
    s = Settings(
        jira_base_url="https://acme.atlassian.net",
        confluence_base_url="http://internal.example.com/wiki",
    )
    assert s.jira_base_url == "https://acme.atlassian.net"
    assert s.confluence_base_url == "http://internal.example.com/wiki"


def test_atlassian_credential_fields_default_empty() -> None:
    s = Settings()
    assert s.jira_email == ""
    assert s.jira_token == ""
    assert s.confluence_email == ""
    assert s.confluence_token == ""


def test_atlassian_credential_fields_round_trip_via_config_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(JIRA_TOKEN_ENV, raising=False)
    monkeypatch.delenv(JIRA_EMAIL_ENV, raising=False)
    monkeypatch.setenv("LODE_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        'jira_enabled = true\njira_email = "me@acme.com"\njira_token = "tok-123"\n',
        encoding="utf-8",
    )
    s = load_settings()
    assert s.jira_enabled is True
    assert s.jira_email == "me@acme.com"
    assert s.jira_token == "tok-123"


# --- credential resolution: env-first, config.toml fallback ------------------


def test_resolve_jira_credentials_env_var_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(JIRA_TOKEN_ENV, "env-token")
    monkeypatch.setenv(JIRA_EMAIL_ENV, "env@acme.com")
    s = Settings(jira_email="config@acme.com", jira_token="config-token")
    creds = resolve_jira_credentials(s)
    assert creds == AtlassianCredentials(email="env@acme.com", token="env-token")


def test_resolve_jira_credentials_falls_back_to_config_toml_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(JIRA_TOKEN_ENV, raising=False)
    monkeypatch.delenv(JIRA_EMAIL_ENV, raising=False)
    s = Settings(jira_email="config@acme.com", jira_token="config-token")
    creds = resolve_jira_credentials(s)
    assert creds == AtlassianCredentials(email="config@acme.com", token="config-token")


def test_resolve_confluence_credentials_env_var_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONFLUENCE_TOKEN_ENV, "env-token")
    monkeypatch.setenv(CONFLUENCE_EMAIL_ENV, "env@acme.com")
    s = Settings(confluence_email="config@acme.com", confluence_token="config-token")
    creds = resolve_confluence_credentials(s)
    assert creds == AtlassianCredentials(email="env@acme.com", token="env-token")


def test_resolve_confluence_credentials_falls_back_to_config_toml_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFLUENCE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONFLUENCE_EMAIL_ENV, raising=False)
    s = Settings(confluence_email="config@acme.com", confluence_token="config-token")
    creds = resolve_confluence_credentials(s)
    assert creds == AtlassianCredentials(email="config@acme.com", token="config-token")


@pytest.mark.parametrize(
    "field_overrides",
    [
        {},  # neither email nor token resolves from any source
        {"jira_email": "only-email@acme.com"},  # token still missing
        {"jira_token": "only-token"},  # email still missing
    ],
)
def test_resolve_jira_credentials_missing_piece_returns_none_not_error(
    monkeypatch: pytest.MonkeyPatch, field_overrides: dict[str, str]
) -> None:
    # Acceptance: a missing token yields a clean "connector inactive" state,
    # not an exception -- even when only one of email/token resolves.
    monkeypatch.delenv(JIRA_TOKEN_ENV, raising=False)
    monkeypatch.delenv(JIRA_EMAIL_ENV, raising=False)
    s = Settings(**field_overrides)
    assert resolve_jira_credentials(s) is None


def test_resolve_confluence_credentials_missing_returns_none_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFLUENCE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONFLUENCE_EMAIL_ENV, raising=False)
    s = Settings()
    assert resolve_confluence_credentials(s) is None


# --- jira_active / confluence_active: flag AND credentials both required ----


def test_jira_active_requires_both_flag_and_resolved_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(JIRA_TOKEN_ENV, "env-token")
    monkeypatch.setenv(JIRA_EMAIL_ENV, "env@acme.com")
    # Credentials resolve, but the flag is off (the default) -- inactive.
    assert jira_active(Settings(jira_enabled=False)) is False
    # Flag on and credentials resolve -- active.
    assert jira_active(Settings(jira_enabled=True)) is True


def test_jira_active_false_when_flagged_on_but_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(JIRA_TOKEN_ENV, raising=False)
    monkeypatch.delenv(JIRA_EMAIL_ENV, raising=False)
    assert jira_active(Settings(jira_enabled=True)) is False


def test_confluence_active_requires_both_flag_and_resolved_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONFLUENCE_TOKEN_ENV, "env-token")
    monkeypatch.setenv(CONFLUENCE_EMAIL_ENV, "env@acme.com")
    assert confluence_active(Settings(confluence_enabled=False)) is False
    assert confluence_active(Settings(confluence_enabled=True)) is True


def test_confluence_active_false_when_flagged_on_but_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFLUENCE_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONFLUENCE_EMAIL_ENV, raising=False)
    assert confluence_active(Settings(confluence_enabled=True)) is False


# --- credential value is never logged or echoed anywhere (lode-dx4r) --------
# All four Atlassian credential fields (jira_email/jira_token/confluence_email/
# confluence_token) are secret=True and, per lode-dx4r, show a PRESENCE
# INDICATOR row in knob_rows() rather than being excluded outright: the
# presence placeholder when the value resolves from any source (env var OR
# config.toml), an "unset" marker when it doesn't -- never the raw value.


ALL_CREDENTIAL_FIELDS = (
    "jira_email",
    "jira_token",
    "confluence_email",
    "confluence_token",
)
ALL_CREDENTIAL_ENV_VARS = (
    JIRA_EMAIL_ENV,
    JIRA_TOKEN_ENV,
    CONFLUENCE_EMAIL_ENV,
    CONFLUENCE_TOKEN_ENV,
)


def _clear_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in ALL_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


def test_credential_fields_appear_in_knob_rows_as_presence_only() -> None:
    s = Settings(jira_token="super-secret", confluence_token="also-secret")
    names = {name for name, _, _ in knob_rows(s)}
    # No longer excluded -- a row appears for every credential field.
    assert "jira_token" in names
    assert "confluence_token" in names
    assert "jira_email" in names
    assert "confluence_email" in names
    # The non-secret sibling fields are still surfaced too.
    assert "jira_enabled" in names
    assert "jira_base_url" in names


def test_knob_rows_shows_unset_marker_when_neither_source_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_credential_env(monkeypatch)
    rows = _knob_values(Settings())
    for name in ALL_CREDENTIAL_FIELDS:
        assert rows[name] == UNSET_PLACEHOLDER


def test_knob_rows_shows_presence_placeholder_when_resolved_via_env_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Acceptance: export the env vars only (nothing in config.toml) -> every
    # row shows the presence placeholder, not the value, not empty.
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv(JIRA_TOKEN_ENV, "env-jira-token")
    monkeypatch.setenv(JIRA_EMAIL_ENV, "env-jira@acme.com")
    monkeypatch.setenv(CONFLUENCE_TOKEN_ENV, "env-confluence-token")
    monkeypatch.setenv(CONFLUENCE_EMAIL_ENV, "env-confluence@acme.com")
    rows = _knob_values(Settings())
    for name in ALL_CREDENTIAL_FIELDS:
        assert rows[name] == REDACTED_PLACEHOLDER
    # Never the raw values.
    assert "env-jira-token" not in rows.values()
    assert "env-jira@acme.com" not in rows.values()
    assert "env-confluence-token" not in rows.values()
    assert "env-confluence@acme.com" not in rows.values()


def test_knob_rows_shows_presence_placeholder_when_resolved_via_config_toml_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Acceptance: a value in config.toml only still shows the presence
    # placeholder -- resolution is env-primary/config-fallback, so presence
    # must reflect EITHER source.
    _clear_credential_env(monkeypatch)
    s = Settings(
        jira_token="config-jira-token",
        jira_email="config-jira@acme.com",
        confluence_token="config-confluence-token",
        confluence_email="config-confluence@acme.com",
    )
    rows = _knob_values(s)
    for name in ALL_CREDENTIAL_FIELDS:
        assert rows[name] == REDACTED_PLACEHOLDER


def test_knob_rows_regression_guard_config_toml_email_never_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE BUG THIS TICKET FIXES: an email placed in config.toml used to be
    # echoed verbatim by knob_rows() (jira_email/confluence_email were not
    # secret=True). Now it must show the presence placeholder, never the
    # address.
    _clear_credential_env(monkeypatch)
    s = Settings(
        jira_email="real-jira-address@acme.com",
        confluence_email="real-confluence-address@acme.com",
    )
    rows = _knob_values(s)
    assert rows["jira_email"] == REDACTED_PLACEHOLDER
    assert rows["confluence_email"] == REDACTED_PLACEHOLDER
    assert "real-jira-address@acme.com" not in rows.values()
    assert "real-confluence-address@acme.com" not in rows.values()


def test_knob_rows_never_leaks_token_value_from_any_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(JIRA_TOKEN_ENV, "super-secret-env-token")
    s = Settings(confluence_token="super-secret-config-token")
    rows = _knob_values(s)
    assert rows["jira_token"] == REDACTED_PLACEHOLDER
    assert rows["confluence_token"] == REDACTED_PLACEHOLDER
    assert "super-secret-env-token" not in rows.values()
    assert "super-secret-config-token" not in rows.values()


def test_atlassian_credentials_repr_redacts_token() -> None:
    creds = AtlassianCredentials(email="me@acme.com", token="super-secret-token")
    rendered = repr(creds)
    assert "super-secret-token" not in rendered
    assert "me@acme.com" in rendered
    assert "redacted" in rendered


def test_settings_repr_and_str_never_echo_secret_tokens() -> None:
    # Acceptance: the credential values are NEVER logged or echoed anywhere --
    # so an incautious repr()/str()/print()/logger.debug() of the Settings
    # object itself must not surface them (secret=True => repr=False on the
    # field). Covers all four credential fields, not just the tokens
    # (lode-dx4r: jira_email/confluence_email are secret=True too now).
    s = Settings(
        jira_token="jira-super-secret",
        confluence_token="conf-super-secret",
        jira_email="jira-secret@acme.com",
        confluence_email="conf-secret@acme.com",
    )
    for rendered in (repr(s), str(s)):
        assert "jira-super-secret" not in rendered
        assert "conf-super-secret" not in rendered
        assert "jira-secret@acme.com" not in rendered
        assert "conf-secret@acme.com" not in rendered
    # A genuinely non-secret sibling remains visible in repr.
    assert "jira_base_url" in repr(Settings(jira_base_url="https://acme.atlassian.net"))
    # The values stay accessible/serializable -- only the human-facing repr
    # is suppressed.
    assert s.jira_token == "jira-super-secret"
    assert s.jira_email == "jira-secret@acme.com"
    assert s.model_dump()["confluence_token"] == "conf-super-secret"
    assert s.model_dump()["confluence_email"] == "conf-secret@acme.com"
