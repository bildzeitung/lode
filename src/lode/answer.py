"""Verifiable answer schema (lode-1k3.1).

``docs/retrieval.md`` ("Make the answer schema verifiable"): the Q&A LLM does
not return prose + ``[note_id]``. It returns a list of **claims**, each carrying
the exact evidence it rests on -- pinned to a **version** (not the logical note,
because bytes drift across versions) and to a verbatim **span** (not the whole
note)::

    answer = [
      { text: "<one factual claim>",
        support: [ { version_id | snapshot_id,
                     quoted_span: "<verbatim text from that version>" } ] },
      ...
    ]

This module owns only the **shape**. Enforcing the schema is necessary but not
sufficient for faithfulness: a well-formed citation can still be wrong, so the
faithfulness gate verifies the evidence app-side before display -- the
verbatim-span check (lode-1k3.2), extractive coupling, then NLI entailment.
Accordingly this module deliberately never reads a version/snapshot body; whether
a ``quoted_span`` actually occurs in its cited target is the span check's job.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator
from pydantic.json_schema import SkipJsonSchema


class Support(BaseModel):
    """One piece of evidence a claim rests on: a verbatim span of one cited target.

    The target is pinned to **exactly one** of a note ``version_id`` or an
    external ``snapshot_id`` (``docs/storage.md`` -- bytes drift across versions,
    so the citation is to the immutable version, never the logical note). The span
    must be verbatim text copied from that target; whether it *occurs* in the
    target body is the verbatim-span check's job (lode-1k3.2), not the schema's.
    """

    model_config = ConfigDict(extra="forbid")

    version_id: str | None = Field(
        default=None,
        min_length=1,
        description="Cited note version id (mutually exclusive with snapshot_id).",
    )
    snapshot_id: str | None = Field(
        default=None,
        min_length=1,
        description="Cited external snapshot id (mutually exclusive with version_id).",
    )
    quoted_span: str = Field(
        ...,
        min_length=1,
        description="Verbatim text copied from the cited target.",
    )
    body_offset: Annotated[int | None, SkipJsonSchema()] = Field(
        default=None,
        ge=0,
        description="App-side only: stamped after the faithfulness gate.",
    )
    """Char offset of this span's occurrence in the cited body, when known (lode-hruz).

    App-side only: never supplied by the model (the LLM has no notion of body
    offsets), stamped after the gate by ``cited_answer._stamp_body_offsets``
    against the retrieved passage the span actually came from, which overwrites
    whatever the model put here. It disambiguates *which* occurrence a repeated
    ``quoted_span`` renders context from; ``None`` when no retrieved passage
    matched, in which case renderers fall back to the first occurrence.

    It rides on this model rather than a parallel app-side type because
    ``Support`` is what every consumer already threads through. ``Support``
    doubles as the structured-output response shape (``qa._ClaimsEnvelope``),
    so ``SkipJsonSchema`` drops this app-side field from the JSON schema handed
    to the provider while leaving it a normal field everywhere else -- the
    invariant rests on the type, not on prose (lode-9nmk). The ``description``
    is for readers of this class; the model never sees the field.
    """

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Support:
        if (self.version_id is None) == (self.snapshot_id is None):
            raise ValueError(
                "support must cite exactly one of version_id or snapshot_id"
            )
        return self

    @property
    def target_id(self) -> str:
        """The cited target id -- exactly one of the two is set, by construction."""
        return self.version_id if self.version_id is not None else self.snapshot_id


class Claim(BaseModel):
    """One factual claim plus the evidence it rests on (at least one support)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, description="A single factual claim.")
    support: list[Support] = Field(
        ...,
        min_length=1,
        description="Evidence the claim rests on; at least one support.",
    )


class Answer(RootModel[list[Claim]]):
    """A Q&A answer: a list of claims (``docs/retrieval.md`` ``answer = [...]``).

    An empty list is a valid answer -- the model asserted nothing; the
    faithfulness gate's abstention path ("your notes don't answer this") then
    reports that to the user.
    """

    root: list[Claim] = Field(default_factory=list)

    @property
    def claims(self) -> list[Claim]:
        """The claims, in order."""
        return self.root
