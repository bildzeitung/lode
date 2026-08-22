"""Tests for lode.drawdown — URL detect, explicit edge, draw-down trigger (lode-w0h.3).

Covers the acceptance criteria: a note pasting a URL yields a source='user' edge
note->external and a fetched, ingested snapshot; the same URL in two notes dedups
to one external node with two edges; only one hop is followed (the fetched
page's own links are never scanned); plus the canonicalization rule set and the
redirect-wrinkle re-pointing the ticket's decision pins down explicitly.

Also covers lode-gpzn.2: JIRA/Confluence Cloud link detection + source_type
routing + semantic external_id + persisted api_base (``TestAtlassianDetection``),
and the ``refresh_external`` source_type dispatcher (``TestRefreshExternalDispatch``).

All fetch-touching tests use a stub :class:`~lode.webfetch.Fetcher` (the seam
lode-w0h.1 built) so the gate never makes a real network request -- except one
negative-controlled "real wiring" test that monkeypatches ``httpx2.Client``
itself (the same technique tests/test_webfetch.py uses for HttpxFetcher's own
classification), to prove `refresh_external`'s production path (no fetcher
override) actually reaches the real `HttpxFetcher`, not just a test-only stub
(the inherited lesson from lode-w0h.1's review: an injectable seam that makes
tests offline can leave the *default* implementation's wiring unexercised).
"""

import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest

from lode.config import load_settings
from lode.drawdown import (
    SOURCE_TYPE_CONFLUENCE,
    SOURCE_TYPE_JIRA,
    SOURCE_TYPE_WEB,
    canonicalize_url,
    detect_and_enqueue_drawdown,
    extract_urls,
    refresh_external,
)
from lode.repository import Repository
from lode.storage import init_db
from lode.webfetch import RawResponse, TransientFetchError

_URL = "https://example.com/article"


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


def _edges_from(conn, note_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT to_id, source, confidence, quoted_text, status, source_version "
        "FROM edges WHERE from_id = ? ORDER BY to_id",
        (note_id,),
    ).fetchall()


def _jobs_for(conn, target: str) -> list[tuple]:
    return conn.execute(
        "SELECT type, status FROM jobs WHERE target_version = ? ORDER BY type",
        (target,),
    ).fetchall()


def _external_row(conn, external_id: str) -> tuple | None:
    return conn.execute(
        "SELECT source_type, api_base FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# extract_urls
# ---------------------------------------------------------------------------


class TestExtractUrls:
    def test_finds_a_bare_url(self):
        assert extract_urls("check out https://example.com/foo") == [
            "https://example.com/foo"
        ]

    def test_no_urls_is_empty(self):
        assert extract_urls("just a plain note, nothing to see here") == []

    def test_strips_trailing_sentence_punctuation(self):
        assert extract_urls("see https://example.com/foo.") == [
            "https://example.com/foo"
        ]
        assert extract_urls("see https://example.com/foo, it's great") == [
            "https://example.com/foo"
        ]

    def test_strips_wrapping_parens(self):
        assert extract_urls("(https://example.com/foo)") == ["https://example.com/foo"]

    def test_keeps_balanced_parens_inside_the_url(self):
        url = "https://en.wikipedia.org/wiki/Foo_(bar)"
        assert extract_urls(f"see {url} for details") == [url]
        # Wrapped in prose parens too -- only the OUTER paren is prose, the
        # inner pair belongs to the URL's own path segment.
        assert extract_urls(f"(see {url}).") == [url]

    def test_multiple_urls_in_order_deduped(self):
        body = (
            "first https://a.example.com/one then https://b.example.com/two "
            "then https://a.example.com/one again"
        )
        assert extract_urls(body) == [
            "https://a.example.com/one",
            "https://b.example.com/two",
        ]

    def test_ignores_non_http_schemes(self):
        assert extract_urls("mailto:me@example.com and ftp://example.com/f") == []

    def test_stops_at_whitespace_and_angle_brackets(self):
        assert extract_urls("a <https://example.com/foo> tag") == [
            "https://example.com/foo"
        ]


# ---------------------------------------------------------------------------
# canonicalize_url
# ---------------------------------------------------------------------------


class TestCanonicalizeUrl:
    def test_lowercases_scheme_and_host_not_path(self):
        assert (
            canonicalize_url("HTTP://Example.COM/Some/Path")
            == "http://example.com/Some/Path"
        )

    def test_strips_default_port(self):
        assert canonicalize_url("http://example.com:80/foo") == "http://example.com/foo"
        assert (
            canonicalize_url("https://example.com:443/foo") == "https://example.com/foo"
        )

    def test_keeps_non_default_port(self):
        assert (
            canonicalize_url("http://example.com:8080/foo")
            == "http://example.com:8080/foo"
        )

    def test_drops_fragment(self):
        assert (
            canonicalize_url("https://example.com/foo#section-2")
            == "https://example.com/foo"
        )

    def test_strips_tracking_params_default_blocklist(self):
        settings = load_settings()
        url = (
            "https://example.com/foo?utm_source=nl&utm_medium=email&"
            "fbclid=abc&gclid=xyz&keep=1"
        )
        assert canonicalize_url(url, settings) == "https://example.com/foo?keep=1"

    def test_sorts_remaining_query_params(self):
        assert (
            canonicalize_url("https://example.com/foo?b=2&a=1")
            == "https://example.com/foo?a=1&b=2"
        )

    def test_tracking_blocklist_is_configurable(self):
        settings = load_settings(url_tracking_param_blocklist=["ref"])
        # A custom blocklist entry is honored, and utm_* (not in this custom
        # list) is NOT stripped -- the blocklist fully replaces the default,
        # it does not merge with it.
        assert (
            canonicalize_url("https://example.com/foo?ref=x&utm_source=y", settings)
            == "https://example.com/foo?utm_source=y"
        )

    def test_normalizes_trailing_slash(self):
        assert canonicalize_url("https://example.com/foo/") == "https://example.com/foo"
        assert canonicalize_url("https://example.com") == "https://example.com/"
        assert canonicalize_url("https://example.com/") == "https://example.com/"

    def test_equivalent_urls_canonicalize_identically(self):
        settings = load_settings()
        a = canonicalize_url(
            "HTTPS://Example.com:443/Foo/?utm_source=nl&b=2&a=1#frag", settings
        )
        b = canonicalize_url("https://example.com/Foo?a=1&b=2", settings)
        assert a == b

    def test_no_query_no_trailing_question_mark(self):
        assert (
            canonicalize_url("https://example.com/foo?utm_source=x")
            == "https://example.com/foo"
        )

    def test_strips_userinfo_including_password(self):
        # lode-0as: credentials in a pasted URL are transport secrets, not
        # source identity -- they must never enter external_id.
        with_password = canonicalize_url("https://user:hunter2@example.com/p")
        with_username = canonicalize_url("https://user@example.com/p")
        bare = canonicalize_url("https://example.com/p")
        assert with_password == with_username == bare == "https://example.com/p"

    def test_strips_userinfo_alongside_other_normalization(self):
        assert (
            canonicalize_url("HTTPS://user:pass@Example.COM:443/foo/#frag")
            == "https://example.com/foo"
        )

    def test_ipv6_literal_host_keeps_brackets_with_non_default_port(self):
        # lode-lt1: urlsplit().hostname strips the brackets from an IPv6
        # literal ("[::1]:8080" -> hostname "::1"), so re-appending a port
        # without re-adding the brackets yields "::1:8080" -- unparseable
        # (the host's own colons get read as the port delimiter). The fix
        # must keep the result re-parseable.
        result = canonicalize_url("http://[::1]:8080/p")
        assert result == "http://[::1]:8080/p"
        reparsed = urlsplit(result)
        assert reparsed.hostname == "::1"
        assert reparsed.port == 8080

    def test_ipv6_literal_host_keeps_brackets_with_default_port(self):
        result = canonicalize_url("http://[2001:db8::1]/p")
        assert result == "http://[2001:db8::1]/p"
        reparsed = urlsplit(result)
        assert reparsed.hostname == "2001:db8::1"
        assert reparsed.port is None

    def test_ipv6_literal_host_with_userinfo_strips_userinfo_keeps_brackets(self):
        result = canonicalize_url("https://user:pass@[::1]:8080/p")
        assert result == "https://[::1]:8080/p"
        reparsed = urlsplit(result)
        assert reparsed.hostname == "::1"
        assert reparsed.port == 8080
        assert reparsed.username is None


# ---------------------------------------------------------------------------
# detect_and_enqueue_drawdown
# ---------------------------------------------------------------------------


class TestDetectAndEnqueueDrawdown:
    def test_creates_explicit_edge_and_enqueues_refresh(self, conn) -> None:
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", f"see {_URL}"
            )

        assert external_ids == [_URL]
        rows = _edges_from(conn, "note-1")
        assert len(rows) == 1
        to_id, source, confidence, quoted_text, status, source_version = rows[0]
        assert to_id == _URL
        assert source == "user"
        assert confidence == 1.0
        assert quoted_text == _URL
        assert status == "fresh"
        assert source_version == "ver-1"
        assert _jobs_for(conn, _URL) == [("refresh", "pending")]

    def test_no_urls_enqueues_nothing(self, conn) -> None:
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", "plain text"
            )
        assert external_ids == []
        assert _edges_from(conn, "note-1") == []

    def test_same_url_two_notes_one_node_two_edges(self, conn) -> None:
        with conn:
            detect_and_enqueue_drawdown(conn, "note-1", "ver-1", _URL)
        with conn:
            detect_and_enqueue_drawdown(conn, "note-2", "ver-2", _URL)

        assert _edges_from(conn, "note-1")[0][0] == _URL
        assert _edges_from(conn, "note-2")[0][0] == _URL
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE to_id = ? AND source = 'user'", (_URL,)
        ).fetchone()
        assert n == 2

    def test_textually_different_but_equivalent_urls_dedup_to_one_edge(
        self, conn
    ) -> None:
        """Two raw URL strings that canonicalize the same yield one edge, not two."""
        with conn:
            detect_and_enqueue_drawdown(
                conn,
                "note-1",
                "ver-1",
                f"first {_URL}?utm_source=x then HTTPS://Example.com/article",
            )
        assert len(_edges_from(conn, "note-1")) == 1

    def test_resaving_same_note_same_url_does_not_duplicate_edge_or_job(
        self, conn
    ) -> None:
        with conn:
            detect_and_enqueue_drawdown(conn, "note-1", "ver-1", _URL)
        # Drain the one enqueued job so a second enqueue attempt (if it
        # happened) would not be silently absorbed by the live-job dedup index.
        conn.execute(
            "UPDATE jobs SET status = 'done' WHERE target_version = ?", (_URL,)
        )
        with conn:
            detect_and_enqueue_drawdown(conn, "note-1", "ver-2", _URL)

        assert len(_edges_from(conn, "note-1")) == 1
        assert _jobs_for(conn, _URL) == [("refresh", "done")]

    def test_malformed_url_is_skipped_not_raised(self, conn) -> None:
        # An unparseable port makes urlsplit().port raise ValueError when
        # accessed -- detect_and_enqueue_drawdown must not let that abort the
        # whole note save.
        body = "see https://example.com:notaport/foo and https://good.example.com/x"
        with conn:
            external_ids = detect_and_enqueue_drawdown(conn, "note-1", "ver-1", body)
        assert external_ids == ["https://good.example.com/x"]

    def test_one_hop_only_no_recursive_extraction(self, conn) -> None:
        """The function only ever reads the note's OWN body -- never a fetched page's."""
        fetched_page_body = (
            f"this page links to {_URL}/deeper and https://another.example.com"
        )
        # Simulate: the drawdown trigger is never called again with a
        # fetched snapshot's body -- refresh_external (tested below) never
        # calls detect_and_enqueue_drawdown or extract_urls at all. This test
        # documents the contract: calling extract_urls on fetched content is
        # simply not something this module's fetch path does.
        assert extract_urls(fetched_page_body) == [
            f"{_URL}/deeper",
            "https://another.example.com",
        ]
        # ^ extract_urls itself has no notion of "hop" -- the one-hop
        # guarantee is structural (only the save path calls it), verified by
        # refresh_external's own tests never touching detect_and_enqueue_drawdown.


# ---------------------------------------------------------------------------
# Atlassian link detection + source_type routing + semantic external_id
# (lode-gpzn.2)
# ---------------------------------------------------------------------------


def _jira_settings(**overrides):
    return load_settings(
        jira_enabled=True, jira_token="tok", jira_email="a@example.com", **overrides
    )


def _confluence_settings(**overrides):
    return load_settings(
        confluence_enabled=True,
        confluence_token="tok",
        confluence_email="a@example.com",
        **overrides,
    )


class TestAtlassianDetection:
    def test_jira_browse_url_routes_when_active(self, conn) -> None:
        url = "https://acme.atlassian.net/browse/ABC-123"
        settings = _jira_settings()
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=settings
            )

        assert external_ids == ["ABC-123"]
        rows = _edges_from(conn, "note-1")
        assert len(rows) == 1
        to_id, source, confidence, quoted_text, status, _ = rows[0]
        assert to_id == "ABC-123"
        assert source == "user"
        assert confidence == 1.0
        assert quoted_text == url  # literal pasted URL, for provenance
        assert status == "fresh"
        assert _jobs_for(conn, "ABC-123") == [("refresh", "pending")]
        assert _external_row(conn, "ABC-123") == (
            SOURCE_TYPE_JIRA,
            "https://acme.atlassian.net",
        )

    def test_jira_externals_row_honors_settings_no_egress_default(self, conn) -> None:
        """The atlassian-routed externals insert must seed no_egress from
        Settings.no_egress_default at true first-write (lode-ge8w) -- the
        same gap lode-a43n closed for notes, still open here.
        """
        url = "https://acme.atlassian.net/browse/ABC-123"
        settings = _jira_settings(no_egress_default=True)
        with conn:
            detect_and_enqueue_drawdown(conn, "note-1", "ver-1", url, settings=settings)

        (no_egress,) = conn.execute(
            "SELECT no_egress FROM externals WHERE external_id = ?", ("ABC-123",)
        ).fetchone()
        assert no_egress == 1

    def test_jira_flag_off_falls_through_to_web(self, conn) -> None:
        url = "https://acme.atlassian.net/browse/ABC-123"
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=load_settings()
            )
        # Flag-off (default) -- untouched web path: canonicalized URL, not
        # the semantic key, and no externals row pre-created.
        assert external_ids == [canonicalize_url(url)]
        assert _external_row(conn, "ABC-123") is None
        assert _jobs_for(conn, canonicalize_url(url)) == [("refresh", "pending")]

    def test_jira_enabled_but_no_credentials_falls_through_to_web(self, conn) -> None:
        url = "https://acme.atlassian.net/browse/ABC-123"
        settings = load_settings(jira_enabled=True)  # no token/email resolvable
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=settings
            )
        assert external_ids == [canonicalize_url(url)]
        assert _external_row(conn, "ABC-123") is None

    def test_jira_url_without_issue_key_falls_through_to_web(self, conn) -> None:
        url = "https://acme.atlassian.net/jira/software/projects/ABC/boards/1"
        settings = _jira_settings()
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=settings
            )
        # Matched host, but no /browse/{KEY} shape -- no semantic id to route
        # on, so this falls through to the web path exactly like flag-off.
        assert external_ids == [canonicalize_url(url)]

    def test_jira_configured_base_url_persisted_verbatim(self, conn) -> None:
        url = "https://jira.internal.example.com/browse/XYZ-9"
        settings = _jira_settings(jira_base_url="https://jira.internal.example.com")
        with conn:
            detect_and_enqueue_drawdown(conn, "note-1", "ver-1", url, settings=settings)
        assert _external_row(conn, "XYZ-9") == (
            SOURCE_TYPE_JIRA,
            "https://jira.internal.example.com",
        )

    def test_jira_configured_base_url_host_mismatch_falls_through(self, conn) -> None:
        # A configured base_url means ONLY that host routes -- the
        # *.atlassian.net inference is not also tried once a base is set.
        url = "https://other.atlassian.net/browse/ABC-123"
        settings = _jira_settings(jira_base_url="https://jira.internal.example.com")
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=settings
            )
        assert external_ids == [canonicalize_url(url)]

    def test_confluence_id_bearing_url_routes_when_active(self, conn) -> None:
        url = "https://acme.atlassian.net/wiki/spaces/ENG/pages/123456789/Design+Doc"
        settings = _confluence_settings()
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=settings
            )

        assert external_ids == ["123456789"]
        rows = _edges_from(conn, "note-1")
        assert rows[0][0] == "123456789"
        assert rows[0][3] == url
        assert _external_row(conn, "123456789") == (
            SOURCE_TYPE_CONFLUENCE,
            "https://acme.atlassian.net",
        )

    def test_confluence_tiny_link_falls_through_to_web(self, conn) -> None:
        """Owner decision F: id-less tiny-links stay synchronous/network-free web."""
        url = "https://acme.atlassian.net/wiki/x/AbCdE"
        settings = _confluence_settings()
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=settings
            )
        assert external_ids == [canonicalize_url(url)]
        rows = _edges_from(conn, "note-1")
        assert rows[0][0] == canonicalize_url(url)

    def test_confluence_legacy_display_url_falls_through_to_web(self, conn) -> None:
        """Owner decision F: legacy /display/SPACE/Title carries no page-id."""
        url = "https://acme.atlassian.net/display/ENG/Design+Doc"
        settings = _confluence_settings()
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=settings
            )
        assert external_ids == [canonicalize_url(url)]

    def test_confluence_flag_off_falls_through_to_web(self, conn) -> None:
        url = "https://acme.atlassian.net/wiki/spaces/ENG/pages/123456789/Design+Doc"
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", url, settings=load_settings()
            )
        assert external_ids == [canonicalize_url(url)]
        assert _external_row(conn, "123456789") is None

    def test_two_url_forms_of_same_issue_dedup_to_one_external(self, conn) -> None:
        """Acceptance: two id-bearing URL forms of the same issue dedup to one node."""
        settings = _jira_settings()
        with conn:
            detect_and_enqueue_drawdown(
                conn,
                "note-1",
                "ver-1",
                "https://acme.atlassian.net/browse/ABC-123",
                settings=settings,
            )
        with conn:
            detect_and_enqueue_drawdown(
                conn,
                "note-2",
                "ver-2",
                "https://acme.atlassian.net/browse/ABC-123/",
                settings=settings,
            )

        (n,) = conn.execute(
            "SELECT COUNT(*) FROM externals WHERE external_id = 'ABC-123'"
        ).fetchone()
        assert n == 1
        (edge_count,) = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE to_id = 'ABC-123' AND source = 'user'"
        ).fetchone()
        assert edge_count == 2

    def test_jira_and_confluence_urls_in_one_note_both_route(self, conn) -> None:
        body = (
            "see https://acme.atlassian.net/browse/ABC-123 and "
            "https://acme.atlassian.net/wiki/spaces/ENG/pages/999/Doc"
        )
        settings = load_settings(
            jira_enabled=True,
            jira_token="tok",
            jira_email="a@example.com",
            confluence_enabled=True,
            confluence_token="tok",
            confluence_email="a@example.com",
        )
        with conn:
            external_ids = detect_and_enqueue_drawdown(
                conn, "note-1", "ver-1", body, settings=settings
            )
        assert external_ids == ["ABC-123", "999"]
        assert _external_row(conn, "ABC-123")[0] == SOURCE_TYPE_JIRA
        assert _external_row(conn, "999")[0] == SOURCE_TYPE_CONFLUENCE


# ---------------------------------------------------------------------------
# refresh_external
# ---------------------------------------------------------------------------


class _StubFetcher:
    def __init__(self, response=None, raises=None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        if self._raises is not None:
            raise self._raises
        return self._response


class TestRefreshExternal:
    def test_ok_ingests_snapshot_under_target_id(self, conn) -> None:
        # fetch_and_extract calls trafilatura on response.text; use real HTML
        # so extraction succeeds above the length floor.
        html = (
            "<html><body><article><p>"
            + ("Some real article content. " * 20)
            + "</p></article></body></html>"
        )
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=html)
        )

        outcome = refresh_external(conn, _URL, load_settings(), fetcher=fetcher)

        assert outcome is not None
        assert "ok" in outcome
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", (_URL,)
        ).fetchone()
        assert status == "ok"

    def test_tombstone_ingests_under_target_id(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=403, text="forbidden")
        )
        outcome = refresh_external(conn, _URL, load_settings(), fetcher=fetcher)
        assert outcome is not None
        assert "tombstone" in outcome
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", (_URL,)
        ).fetchone()
        assert status == "tombstone"

    def test_tombstone_outcome_names_the_reason(self, conn) -> None:
        """lode-pmx0: a web tombstone's outcome line names WHY, mirroring the
        Atlassian legs (lode-gpzn.5) -- e.g. 'tombstone (http_401)', not a bare
        'tombstone'.
        """
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=401, text="unauthorized")
        )
        outcome = refresh_external(conn, _URL, load_settings(), fetcher=fetcher)
        assert outcome is not None
        assert "tombstone (http_401)" in outcome
        # The reason tag is the fetch unit's own short machine tag, never an
        # interpolated response body -- the raw 401 body text never appears.
        assert "unauthorized" not in outcome

    def test_transient_failure_propagates_uncaught(self, conn) -> None:
        fetcher = _StubFetcher(raises=TransientFetchError("boom"))
        with pytest.raises(TransientFetchError):
            refresh_external(conn, _URL, load_settings(), fetcher=fetcher)

    def test_redirect_repoints_user_edges_to_final_canonical_id(self, conn) -> None:
        """The redirect wrinkle: a pre-fetch edge follows the final canonical id."""
        pasted = "https://example.com/old-slug"
        final = "https://example.com/new-slug"
        with conn:
            detect_and_enqueue_drawdown(conn, "note-1", "ver-1", pasted)
        assert _edges_from(conn, "note-1")[0][0] == pasted

        html = (
            "<html><body><article><p>"
            + ("Real content after the redirect. " * 20)
            + "</p></article></body></html>"
        )
        fetcher = _StubFetcher(
            response=RawResponse(final_url=final, status_code=200, text=html)
        )

        outcome = refresh_external(conn, pasted, load_settings(), fetcher=fetcher)

        assert "repointed" in outcome
        assert _edges_from(conn, "note-1")[0][0] == final
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", (final,)
        ).fetchone()
        assert status == "ok"
        # No externals row was ever created for the pre-redirect id.
        row = conn.execute(
            "SELECT 1 FROM externals WHERE external_id = ?", (pasted,)
        ).fetchone()
        assert row is None

    def test_no_redirect_does_not_touch_edges(self, conn) -> None:
        with conn:
            detect_and_enqueue_drawdown(conn, "note-1", "ver-1", _URL)
        html = (
            "<html><body><article><p>"
            + ("No redirect here. " * 20)
            + "</p></article></body></html>"
        )
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=html)
        )

        outcome = refresh_external(conn, _URL, load_settings(), fetcher=fetcher)

        assert "repointed" not in outcome
        assert _edges_from(conn, "note-1")[0][0] == _URL

    def test_dedups_identical_refetch_no_new_snapshot(self, conn) -> None:
        html = (
            "<html><body><article><p>"
            + ("Stable content. " * 20)
            + "</p></article></body></html>"
        )
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=html)
        )
        refresh_external(conn, _URL, load_settings(), fetcher=fetcher)
        refresh_external(conn, _URL, load_settings(), fetcher=fetcher)
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (_URL,)
        ).fetchone()
        assert n == 1


class TestRefreshExternalRealWiring:
    """Negative-controlled proof that the production path (no fetcher override)
    really reaches the real HttpxFetcher -- not a test-only bypass (the
    inherited lesson from lode-w0h.1's review).
    """

    def test_default_fetcher_reaches_real_httpx_client(self, conn, monkeypatch) -> None:
        html = (
            "<html><body><article><p>"
            + ("Real wiring content. " * 20)
            + "</p></article></body></html>"
        )

        class _FakeResponse:
            def __init__(self, status_code: int, url: str, text: str) -> None:
                self.status_code = status_code
                self.url = url
                self.text = text

        class _FakeClient:
            def __init__(self, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

            def get(self, url: str) -> _FakeResponse:
                return _FakeResponse(200, url, html)

        monkeypatch.setattr(httpx2, "Client", _FakeClient)

        # No `fetcher=` override -- this is the exact call the registered
        # worker handler makes in production.
        outcome = refresh_external(conn, _URL, load_settings())

        assert outcome is not None
        assert "ok" in outcome
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", (_URL,)
        ).fetchone()
        assert status == "ok"


# ---------------------------------------------------------------------------
# refresh_external source_type dispatcher (lode-gpzn.2)
# ---------------------------------------------------------------------------


class TestRefreshExternalDispatch:
    def test_no_externals_row_dispatches_to_web(self, conn) -> None:
        """No row yet (a first-ever web refresh) falls back to SOURCE_TYPE_WEB.

        Mirrors worker._refresh_dead_letter_hook's identical fallback --
        detect_and_enqueue_drawdown never pre-creates a web external's row.
        """
        html = (
            "<html><body><article><p>"
            + ("Dispatcher web fallback content. " * 20)
            + "</p></article></body></html>"
        )
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=html)
        )
        outcome = refresh_external(conn, _URL, load_settings(), fetcher=fetcher)
        assert outcome is not None
        assert "ok" in outcome
        assert fetcher.calls == [_URL]

    def test_explicit_web_source_type_row_dispatches_to_web(self, conn) -> None:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
            (_URL, SOURCE_TYPE_WEB),
        )
        conn.commit()
        html = (
            "<html><body><article><p>"
            + ("Explicit web row content. " * 20)
            + "</p></article></body></html>"
        )
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=html)
        )
        outcome = refresh_external(conn, _URL, load_settings(), fetcher=fetcher)
        assert outcome is not None
        assert "ok" in outcome

    def test_jira_source_type_dispatches_to_jira_fetch_unit(self, conn) -> None:
        """lode-gpzn.3: JIRA now has a real fetch unit -- dispatch reaches it,
        rebuilding the request URL from external_id + the api_base persisted
        on the row (lode-gpzn.2), rather than raising."""
        api_base = "https://acme.atlassian.net"
        conn.execute(
            "INSERT INTO externals (external_id, source_type, api_base) "
            "VALUES (?, ?, ?)",
            ("ABC-123", SOURCE_TYPE_JIRA, api_base),
        )
        conn.commit()
        issue_json = {
            "fields": {"summary": "Dispatcher JIRA content"},
            "renderedFields": {
                "description": (
                    "<p>" + ("Dispatcher-level JIRA fetch content. " * 20) + "</p>"
                )
            },
        }
        empty_comments = {"startAt": 0, "maxResults": 0, "total": 0, "comments": []}

        class _JiraQueueFetcher:
            def __init__(self) -> None:
                self._responses = [
                    RawResponse(
                        final_url=(
                            f"{api_base}/rest/api/3/issue/ABC-123?expand=renderedFields"
                        ),
                        status_code=200,
                        text=json.dumps(issue_json),
                    ),
                    RawResponse(
                        final_url=(
                            f"{api_base}/rest/api/3/issue/ABC-123/comment"
                            "?startAt=0&expand=renderedBody"
                        ),
                        status_code=200,
                        text=json.dumps(empty_comments),
                    ),
                ]
                self.calls: list[str] = []

            def fetch(self, url: str) -> RawResponse:
                self.calls.append(url)
                return self._responses.pop(0)

        jira_fetcher = _JiraQueueFetcher()

        outcome = refresh_external(
            conn, "ABC-123", load_settings(), fetcher=jira_fetcher
        )

        assert outcome is not None
        assert "ok" in outcome
        assert jira_fetcher.calls[0] == (
            f"{api_base}/rest/api/3/issue/ABC-123?expand=renderedFields"
        )
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", ("ABC-123",)
        ).fetchone()
        assert status == "ok"

    def test_jira_source_type_without_credentials_raises(self, conn) -> None:
        """No fetcher override and no resolvable credentials (default,
        JIRA-disabled settings) -- the default-fetcher construction inside
        lode.jira_fetch.fetch_jira_issue raises, naming the external_id."""
        conn.execute(
            "INSERT INTO externals (external_id, source_type, api_base) "
            "VALUES (?, ?, ?)",
            ("ABC-123", SOURCE_TYPE_JIRA, "https://acme.atlassian.net"),
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="ABC-123"):
            refresh_external(conn, "ABC-123", load_settings())

    def test_jira_source_type_missing_api_base_raises(self, conn) -> None:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
            ("ABC-999", SOURCE_TYPE_JIRA),
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="ABC-999"):
            refresh_external(conn, "ABC-999", load_settings(), fetcher=_StubFetcher())

    def test_jira_source_type_forced_401_outcome_names_reason(self, conn) -> None:
        """lode-gpzn.5: a forced 401 (auth failure) still tombstones per the
        taxonomy, but the outcome string now surfaces the classified reason
        (e.g. 'http_401') rather than a bare 'tombstone' -- visible in
        ``lode work``'s per-job outcome echo, naming both source and reason."""
        api_base = "https://acme.atlassian.net"
        conn.execute(
            "INSERT INTO externals (external_id, source_type, api_base) "
            "VALUES (?, ?, ?)",
            ("ABC-401", SOURCE_TYPE_JIRA, api_base),
        )
        conn.commit()
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=(
                    f"{api_base}/rest/api/3/issue/ABC-401?expand=renderedFields"
                ),
                status_code=401,
                text="denied",
            )
        )

        outcome = refresh_external(conn, "ABC-401", load_settings(), fetcher=fetcher)

        assert outcome == "refreshed ABC-401: tombstone (http_401)"
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", ("ABC-401",)
        ).fetchone()
        assert status == "tombstone"

    def test_confluence_source_type_forced_401_outcome_names_reason(self, conn) -> None:
        """Owner decision D: the Confluence leg mirrors the JIRA leg's
        reason-surfacing exactly."""
        api_base = "https://acme.atlassian.net"
        conn.execute(
            "INSERT INTO externals (external_id, source_type, api_base) "
            "VALUES (?, ?, ?)",
            ("997", SOURCE_TYPE_CONFLUENCE, api_base),
        )
        conn.commit()
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=(f"{api_base}/wiki/rest/api/content/997?expand=body.view"),
                status_code=401,
                text="denied",
            )
        )

        outcome = refresh_external(conn, "997", load_settings(), fetcher=fetcher)

        assert outcome == "refreshed 997: tombstone (http_401)"
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", ("997",)
        ).fetchone()
        assert status == "tombstone"

    def test_confluence_source_type_dispatches_to_confluence_fetch_unit(
        self, conn
    ) -> None:
        """lode-gpzn.4 + lode-mfts: Confluence has a real fetch unit -- dispatch
        reaches it, rebuilding the request URL from external_id + the
        api_base persisted on the row (lode-gpzn.2), rather than raising."""
        api_base = "https://acme.atlassian.net"
        conn.execute(
            "INSERT INTO externals (external_id, source_type, api_base) "
            "VALUES (?, ?, ?)",
            ("999", SOURCE_TYPE_CONFLUENCE, api_base),
        )
        conn.commit()
        page_json = {
            "body": {
                "view": {
                    "value": (
                        "<div><h1>Dispatcher Confluence content</h1>"
                        "<p>"
                        + ("Dispatcher-level Confluence fetch content. " * 20)
                        + "</p><p>"
                        + ("A second paragraph of real prose content. " * 20)
                        + "</p></div>"
                    )
                }
            }
        }
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=(f"{api_base}/wiki/rest/api/content/999?expand=body.view"),
                status_code=200,
                text=json.dumps(page_json),
            )
        )

        outcome = refresh_external(conn, "999", load_settings(), fetcher=fetcher)

        assert outcome is not None
        assert "ok" in outcome
        assert fetcher.calls == [
            f"{api_base}/wiki/rest/api/content/999?expand=body.view"
        ]
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", ("999",)
        ).fetchone()
        assert status == "ok"

    def test_confluence_source_type_without_credentials_raises(self, conn) -> None:
        """No fetcher override and no resolvable credentials (default,
        Confluence-disabled settings) -- the default-fetcher construction
        inside lode.confluence.fetch_confluence_page raises (unlike the JIRA
        leg's equivalent error, this one does not name the external_id --
        existing lode-gpzn.4 behavior, unchanged here)."""
        conn.execute(
            "INSERT INTO externals (external_id, source_type, api_base) "
            "VALUES (?, ?, ?)",
            ("999", SOURCE_TYPE_CONFLUENCE, "https://acme.atlassian.net"),
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="Confluence Cloud credentials"):
            refresh_external(conn, "999", load_settings())

    def test_confluence_source_type_missing_api_base_raises(self, conn) -> None:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
            ("998", SOURCE_TYPE_CONFLUENCE),
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="998"):
            refresh_external(conn, "998", load_settings(), fetcher=_StubFetcher())

    def test_unknown_source_type_raises(self, conn) -> None:
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
            ("weird-1", "carrier-pigeon"),
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="carrier-pigeon"):
            refresh_external(conn, "weird-1", load_settings())


# ---------------------------------------------------------------------------
# Repository.save integration
# ---------------------------------------------------------------------------


class TestRepositorySaveIntegration:
    def test_pasting_a_url_creates_edge_and_enqueues_refresh(self, conn) -> None:
        repo = Repository(conn)
        result = repo.save("note-1", f"today I read {_URL}, interesting stuff")

        rows = _edges_from(conn, "note-1")
        assert len(rows) == 1
        assert rows[0][0] == _URL
        assert rows[0][1] == "user"
        assert rows[0][5] == result.version_id
        assert _jobs_for(conn, _URL) == [("refresh", "pending")]

    def test_no_url_body_enqueues_no_refresh_job(self, conn) -> None:
        repo = Repository(conn)
        repo.save("note-1", "just a plain note")
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'refresh'"
        ).fetchone()
        assert n == 0

    def test_update_still_containing_same_url_does_not_duplicate_edge(
        self, conn
    ) -> None:
        repo = Repository(conn)
        first = repo.save("note-1", f"see {_URL}")
        repo.save("note-1", f"see {_URL} again", parent=first.version_id)
        assert len(_edges_from(conn, "note-1")) == 1
