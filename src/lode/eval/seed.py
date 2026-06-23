"""Deterministic seed corpus for the eval harness (lode-5y8.4).

The eval harness golden set (lode-5y8.3) pins known-good citations to
content-hash ``version_id``s plus verbatim spans (the faithfulness schema,
``lode.answer``). But a ``version_id`` does not exist until a note is saved, and
re-saving or re-chunking would change it -- so the golden citations would have
nothing stable to reference. This module fixes that: it commits a **fixed set of
note bodies** (the ``corpus/*.md`` files) and loads them deterministically into
reproducible ``version_id``s the golden set can cite.

Reproducibility rests on two things:

* **The bodies are committed verbatim** as ``corpus/*.md`` (pinned to LF by
  ``corpus/.gitattributes``) and loaded in sorted-filename order, so the same
  notes appear in the same order with the same bytes on every run and machine.
* **The ``version_id`` is the storage core's content hash** --
  :func:`lode.hashing.content_version_id`, the single source of truth for lode's
  content-address ids (``docs/storage.md``). The fixture *imports* it rather than
  reimplementing the framing, so the ids this fixture produces are exactly the
  ids the real version-save path will produce for the same bodies, and they
  cannot drift.

Each seed note is a single ``create`` version (no parent, no update chain): the
fixture is the *input* corpus and the golden citations target the head version,
so a linear depth of one is all step-1 eval needs. ``note_id`` is the file stem
-- stable and human-readable -- and is folded into the hash exactly as the
storage core folds it, so the fixture must supply these exact ``note_id``s for
the ids to match.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources

from lode.hashing import NO_PARENT, content_version_id


@dataclass(frozen=True)
class SeedNote:
    """One fixed seed note: its logical id, its committed body, and the version id.

    ``version_id`` is derived from ``note_id`` and ``body`` via
    :func:`lode.hashing.content_version_id` (the note is a root ``create``, so its
    parent is :data:`lode.hashing.NO_PARENT`). It is the stable id the golden set
    (lode-5y8.3) cites.
    """

    note_id: str
    body: str
    version_id: str


def _corpus_bodies() -> Iterator[tuple[str, str]]:
    """Yield ``(note_id, body)`` for every committed corpus note, in stable order.

    The bodies are the ``corpus/*.md`` package resources, read verbatim (a body is
    the exact file text). Notes are yielded in sorted-filename order so the corpus
    is identical across runs and machines; ``note_id`` is the file stem.
    """
    corpus = resources.files(__package__).joinpath("corpus")
    names = sorted(
        entry.name for entry in corpus.iterdir() if entry.name.endswith(".md")
    )
    for name in names:
        note_id = name.removesuffix(".md")
        body = corpus.joinpath(name).read_text(encoding="utf-8")
        yield note_id, body


def seed_notes() -> tuple[SeedNote, ...]:
    """Load the committed seed corpus into reproducible ``SeedNote``s.

    Deterministic by construction: the same committed bodies, in the same order,
    hashed by the same content-address function, yield the same ``version_id``s on
    every call and every machine. This is the fixture the golden Q&A set cites and
    the harness loads into a fresh store.
    """
    return tuple(
        SeedNote(
            note_id=note_id,
            body=body,
            version_id=content_version_id(note_id, NO_PARENT, body),
        )
        for note_id, body in _corpus_bodies()
    )
