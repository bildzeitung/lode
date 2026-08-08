"""``lode models pull`` -- warm the local fastembed model-weights cache."""

from collections.abc import Callable

import typer

from lode import cli
from lode.cli import app
from lode.config import hf_hub_offline

models_app = typer.Typer(
    help="Manage the local fastembed model-weights cache.",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")


#: fastembed's own catch-all failure message -- the final ``raise`` in
#: ``fastembed/common/model_management.py``'s ``download_model``, once every
#: source and retry is exhausted. It is the one stable signature left to key off
#: (see :func:`_warm`'s docstring for why fastembed leaves nothing more specific).
_FASTEMBED_EXHAUSTED_SOURCES = "from any source"


def _warm(warm: Callable[[], None], model_id: str) -> None:
    """Run one wrapper's ``warm()``, translating a download failure into an
    actionable ``lode models pull`` message instead of a raw traceback (lode-96t).

    ``lode models pull`` exists precisely to make the first-run network
    dependency explicit, so its own most likely failure path -- no network, or
    ``HF_HUB_OFFLINE=1`` against a cold cache -- must not itself be a stack
    trace. Verified empirically against the installed fastembed/huggingface_hub,
    not just read from source, because fastembed's actual behavior collapses
    more than the source alone suggests:

    - **No network reachable at all** escapes fastembed uncaught, as an
      ``httpx`` transport-level exception (``httpx.TransportError`` --
      ``ConnectError``, ``TimeoutException``, ...): fastembed's retry loop
      (``download_model``) only catches ``(EnvironmentError,
      RepositoryNotFoundError, ValueError)``, none of which an ``httpx``
      transport failure subclasses, so it is never swallowed.
    - **``HF_HUB_OFFLINE=1`` against a cold cache** and a **genuine HTTP error**
      (rate-limited / 5xx) against a reachable network both collapse -- deep
      inside fastembed's retry loop -- into the exact same generic
      ``ValueError("Could not load model {id} from any source.")``: fastembed
      catches ``HfHubHTTPError`` / ``LocalEntryNotFoundError`` /
      ``RepositoryNotFoundError`` internally on every attempt and never
      re-raises or chains the original cause, so by the time this ``ValueError``
      reaches us there is no exception-side signal left to tell the two apart.
      The only reliable signal is one *we* already have before calling in:
      whether ``HF_HUB_OFFLINE`` was set (:func:`lode.config.hf_hub_offline`). If it was,
      fastembed forced ``local_files_only=True`` throughout (mirroring the same
      env var itself) and never attempts the network at all, so a failure here
      can only be the cold-cache case; if not, this is a genuine download
      failure after retrying every source. "Every source" is HuggingFace alone
      for lode's default models (their ``sources.url`` is ``None``, so no
      mirror is attempted); but for a config-overridden, GCS-mirrored model id
      (e.g. ``BAAI/bge-base-en-v1.5``) fastembed also falls back to *its own*
      GCS mirror (``storage.googleapis.com/qdrant-fastembed`` -- not a
      HuggingFace host), and swallows that leg's failure just as silently, in a
      bare ``except Exception``. Both legs therefore collapse into this one
      ``ValueError``, carrying no signal for which of them exhausted, so the
      message names both as possible causes rather than blaming HuggingFace
      alone (lode-4hy1).

    Anything else -- a different exception entirely, or a ``ValueError`` that
    doesn't carry fastembed's specific exhausted-sources signature -- propagates
    unchanged: a real defect must never read as a network problem.
    """
    import httpx

    try:
        warm()
    except httpx.TransportError as exc:
        typer.echo(
            f"could not reach HuggingFace to download {model_id}: {exc}\n"
            "No network route to huggingface.co -- connect to the network and "
            "retry 'lode models pull'.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        if _FASTEMBED_EXHAUSTED_SOURCES not in str(exc):
            raise  # not fastembed's download-failure signature -- a real bug
        if hf_hub_offline():
            typer.echo(
                f"cache is cold for {model_id} and HF_HUB_OFFLINE=1 is set, so "
                "no download was attempted: run 'lode models pull' once without "
                "HF_HUB_OFFLINE to warm the cache, then the offline flag will "
                "work.",
                err=True,
            )
        else:
            typer.echo(
                f"failed to download {model_id} after retrying every "
                f"configured source: {exc}\nHuggingFace (and, for a model "
                "that has one, fastembed's GCS mirror) may be rate-limiting "
                "or unavailable -- check your connection and try again "
                "shortly.",
                err=True,
            )
        raise typer.Exit(code=1) from None


@models_app.command("pull")
def models_pull() -> None:
    """Warm the local model cache: download the resolved weights once, deliberately.

    On a cold cache, the first embed call otherwise downloads several
    hundred MB of model weights from HuggingFace mid-capture -- a surprise
    phone-home rather than a one-time setup cost. This forces that download
    now, up front, so a later "lode work" / "lode ask" never hits the
    network unexpectedly.

    Warms every model named by your resolved settings (a config.toml
    override of the embedding, reranker, or entailment model is honored):
    the embedder and the reranker/NLI cross-encoder (see
    docs/configuration.md "Models"). The reranker and entailment models
    default to the same id, so when they match, the second load is skipped
    as a cache hit rather than re-fetched.

    Once warmed, RETRIEVAL is fully offline: a query-only embed
    (related-notes, "lode ask") never resolves an HF revision, so it makes no
    outbound call against warm weights. INDEXING is not -- it still makes one
    read-only HuggingFace metadata call per process to stamp the vector
    provenance it records (the resolved model revision, docs/storage.md
    "Model provenance"), even against a fully warm cache. Warming here cannot
    prepay that call: the revision it resolves is per-embedder, in-memory
    state that nothing persists to disk, so a later "lode work" process's own
    embedder re-probes regardless (lode-r4r2, lode-j5r2). Set
    HF_HUB_OFFLINE=1 -- fastembed's own offline flag, not lode-specific -- to
    force fastembed's local-weights-only load AND skip that metadata call
    outright, recording model_revision = NULL for those vectors instead.

    A bad config.toml gives the same clean stderr message and exit 1 every
    other command gives, not a raw traceback. On its most likely failure
    path -- no network, HF_HUB_OFFLINE=1 against a cold cache, or a
    HuggingFace rate limit or error -- this exits non-zero with a clear,
    actionable message instead.
    """
    from lode.embedding import FastEmbedEmbedder
    from lode.faithfulness import FastEmbedEntailmentScorer
    from lode.retrieval import FastEmbedCrossEncoder

    # _resolve_settings() (not bare Settings()) so a config-file override of
    # embedding_model/rerank_model/entailment_model actually reaches this
    # command (lode-og3) -- otherwise 'models pull' warms the pinned defaults
    # while 'lode work'/'lode ask' (which DO resolve settings) still hit the
    # network mid-capture for the user's actual configured models, exactly the
    # surprise phone-home this command exists to prevent.
    settings = cli._resolve_settings()
    cache_dir = cli.model_cache_dir()
    typer.echo(f"pulling model weights into {cache_dir} ...")

    typer.echo(f"  embedder: {settings.embedding_model}")
    _warm(FastEmbedEmbedder(settings).warm, settings.embedding_model)

    typer.echo(f"  reranker: {settings.rerank_model}")
    _warm(FastEmbedCrossEncoder(settings).warm, settings.rerank_model)

    # rerank_model and entailment_model default to the same pinned id (lode-txh.6),
    # so the second load would be a pure cache hit -- skip it, but still say so
    # rather than silently omitting the model from the report.
    same_as_reranker = settings.entailment_model == settings.rerank_model
    suffix = " (same model as reranker -- already cached)" if same_as_reranker else ""
    typer.echo(f"  entailment (NLI): {settings.entailment_model}{suffix}")
    if not same_as_reranker:
        _warm(FastEmbedEntailmentScorer(settings).warm, settings.entailment_model)

    typer.echo(f"done: model weights cached at {cache_dir}")
