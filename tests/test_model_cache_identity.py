"""Drift guard: the pinned model-cache identity map still matches fastembed.

lode-l38d.6's review escalation found that `lode status`'s cold-cache probe
reached its (cheap) filesystem check only via `import fastembed`, which drags
in onnxruntime + numpy (~866 modules) and measurably slowed a pure-sqlite-read
command 2-4x on the WARM path. The fix (human decision, 2026-07-16): pin the
three models' on-disk HuggingFace cache identity -- the `sources.hf` repo id
and `model_file` from fastembed's own `list_supported_models()` -- as build
constants in :data:`lode.config._MODEL_CACHE_IDENTITY`, so `lode status`
answers "is this cached?" via `huggingface_hub.try_to_load_from_cache` alone.

That pin can silently drift from fastembed's actual registry on a fastembed
upgrade (a repo rename, a changed weight-file path). THIS test is the guard:
it imports fastembed (production code -- `lode.config`, `lode.cli` -- never
does) and asserts the pinned identity for each of lode's two distinct pinned
model ids (`embedding_model`; `rerank_model`/`entailment_model` share one id,
lode-txh.6) still matches what `list_supported_models()` reports today.
"""

from lode.config import Settings, _MODEL_CACHE_IDENTITY, model_cache_identity


def test_pinned_identity_covers_every_resolved_model_id() -> None:
    # The two distinct pinned ids Settings() resolves by default (rerank_model
    # and entailment_model share one id, lode-txh.6) must each have a pinned
    # cache identity -- otherwise the probe silently falls back to importing
    # fastembed for lode's OWN default settings, defeating the pin's purpose.
    settings = Settings()
    for model_id in {
        settings.embedding_model,
        settings.rerank_model,
        settings.entailment_model,
    }:
        assert model_cache_identity(model_id) is not None, (
            f"{model_id!r} has no pinned cache identity -- lode status would "
            "silently fall back to `import fastembed` for lode's own default "
            "settings"
        )


def test_pinned_embedding_identity_matches_fastembed() -> None:
    from fastembed import TextEmbedding

    settings = Settings()
    entry = next(
        m
        for m in TextEmbedding.list_supported_models()
        if m["model"].lower() == settings.embedding_model.lower()
    )
    assert model_cache_identity(settings.embedding_model) == (
        entry["sources"]["hf"],
        entry["model_file"],
    )


def test_pinned_cross_encoder_identity_matches_fastembed() -> None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    settings = Settings()
    # rerank_model and entailment_model default to the same pinned id
    # (lode-txh.6) and always share the cross_encoder registry.
    assert settings.rerank_model.lower() == settings.entailment_model.lower()
    entry = next(
        m
        for m in TextCrossEncoder.list_supported_models()
        if m["model"].lower() == settings.rerank_model.lower()
    )
    assert model_cache_identity(settings.rerank_model) == (
        entry["sources"]["hf"],
        entry["model_file"],
    )


def test_pinned_identity_map_has_no_stray_entries() -> None:
    # Every key pinned in the map is exercised by the tests above -- catches a
    # copy-paste entry for a model lode doesn't actually resolve by default.
    settings = Settings()
    resolved = {
        settings.embedding_model.lower(),
        settings.rerank_model.lower(),
        settings.entailment_model.lower(),
    }
    assert set(_MODEL_CACHE_IDENTITY) <= resolved
