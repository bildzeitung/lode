"""Shared real-SDK Anthropic batch-results test rig (lode-a9x3, lode-qnfp).

Moved here from `tests/conftest.py` rather than left there: none of these six
names is a pytest fixture -- they are plain functions imported by an explicit
`from _anthropic_rig import ...`, exactly like `tests/_gitrepo.py`'s `_git`
and `tests/_hookharness.py`'s harness, this repo's precedent for a
cross-module SHARED TEST HELPER that needs none of conftest's collection
magic. Living in conftest.py meant ~130 lines of Anthropic-batch-wire fixture
trivia in the one module auto-loaded for every test in the suite; a dedicated
module costs one file plus two import lines and keeps conftest.py's contents
meaning "fixtures and collection hooks."

Both callers drive a REAL `anthropic.Anthropic` client answered in-process by
`httpx.MockTransport`, because a MagicMock-based fake client can't reproduce
the SDK's own `construct_type_unchecked` leniency for a wrong-shape line --
`_wrong_shape_result`'s docstring in src/lode/llm_provider.py owns that
reasoning, deferring in turn to docs/stack.md "Error contract".

Hoisted at TWO copies, under the three-copy bar tests/_gitrepo.py records:
the second copy was a ~40-line hand-duplication of SDK-shaped fixture data
that has to be updated in lockstep when the pinned SDK's MessageBatch
required fields change, not two helpers encoding different contracts. Read
the caller roster off the code (`grep -rl '_real_anthropic_client' tests/`).
"""

import copy
import json
from collections.abc import Callable
from typing import Any

import httpx


def _real_anthropic_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Any:
    """A REAL SDK client answered in-process by ``handler``.

    The ``MagicMock`` ``collect_batch`` tests raise from the call by
    construction, so they cannot see *when* the SDK resolves status or decodes
    a line -- and that timing is the entire subject of the real-SDK tests. Each
    caller carries ``@pytest.mark.network`` to lift the autouse
    real-client-construction guard above (lode-85q); ``httpx.MockTransport``
    answers in-process, so no socket is ever opened.
    """
    import anthropic

    return anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,  # keep the SDK's own retry ladder out of the assertion
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _results_handler(
    batch_id: str, make_results: Callable[[], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    """Route ``/results`` to ``make_results()``, everything else to ``retrieve``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/results"):
            return make_results()
        return httpx.Response(200, json=_ended_batch_body(batch_id))

    return handler


def _ended_batch_body(batch_id: str) -> dict:
    """The ``retrieve`` body for an "ended" batch with the given id.

    Only the fields the SDK's own ``MessageBatch`` model requires. The 200
    ``retrieve`` leg is load-bearing in every caller: ``batches.results``
    retrieves the batch itself first, so a transport that errored on *every*
    path would assert against that call instead of the decoder-returning one.
    """
    return {
        "id": batch_id,
        "type": "message_batch",
        "processing_status": "ended",
        "results_url": (
            f"https://api.anthropic.com/v1/messages/batches/{batch_id}/results"
        ),
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-02T00:00:00Z",
        "request_counts": {
            "canceled": 0,
            "errored": 0,
            "expired": 0,
            "processing": 0,
            "succeeded": 1,
        },
    }


def _succeeded_payload(
    custom_id: str = "ver-shape", tool_input: dict | None = None
) -> dict:
    """The success payload as a plain dict, pre-JSON-encoding (lode-i821).

    Dict-returning (rather than pre-encoded) so :func:`_payload_without` can
    delete a field from it before :func:`_jsonl` encodes it.

    ``tool_input`` overrides the generic ``_Widget``-shaped tool payload, so a
    caller needing a domain-shaped one (enrichment) asks for it rather than
    reaching into ``result.message.content[0]`` to swap it (lode-a9x3).
    """
    return {
        "custom_id": custom_id,
        "result": {
            "type": "succeeded",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "emit",
                        "input": (
                            {"name": "w", "count": 1}
                            if tool_input is None
                            else tool_input
                        ),
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    }


def _payload_without(payload: dict, *path: str | int) -> dict:
    """A deep copy of ``payload`` with the field at ``path`` deleted (lode-i821).

    Every wrong-shape test case is built as :func:`_succeeded_payload` minus
    exactly one field, via this helper, so a case cannot drift into failing
    over a different field than the one it names.
    """
    result = copy.deepcopy(payload)
    node = result
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return result


def _jsonl(payload: dict) -> bytes:
    """Encode ``payload`` as one JSONL line, the wire shape ``batches.results`` streams."""
    return (json.dumps(payload) + "\n").encode()
