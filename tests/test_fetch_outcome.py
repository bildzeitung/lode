"""Tests for the connector-neutral HTTP fetch-outcome classifier (lode-gpzn.13)."""

import pytest

from lode.fetch_outcome import HttpOutcome, classify_http_status


@pytest.mark.parametrize("status_code", [200, 201, 204, 301, 302, 304, 399])
def test_2xx_3xx_is_ok(status_code):
    assert classify_http_status(status_code) is HttpOutcome.OK


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 599])
def test_transient_statuses(status_code):
    assert classify_http_status(status_code) is HttpOutcome.TRANSIENT


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410, 499])
def test_other_4xx_is_tombstone(status_code):
    """Every 4xx except the 408/429 transient carve-out is a permanent tombstone."""
    assert classify_http_status(status_code) is HttpOutcome.TOMBSTONE


def test_408_and_429_are_the_only_transient_4xx():
    """Sanity check on the carve-out itself, not just spot-checked codes above."""
    transient_4xx = {
        code
        for code in range(400, 500)
        if classify_http_status(code) is HttpOutcome.TRANSIENT
    }
    assert transient_4xx == {408, 429}
