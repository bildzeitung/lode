"""Unit-test the docs-site staging rules (lode-fhql.9).

The Mermaid pre-render needs Docker and is exercised by the CI job itself; what
is tested here is the part that is pure, subtle, and silently wrong when it
drifts -- ``docs/stack.md``'s ONE link-rewrite rule, the published set the rule
is keyed on, and the pins the workflow duplicates. A rewrite bug does not fail
the build: it ships a live 404, or leaks a rewritten URL into a code example,
which nothing else reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

build_docs_site = load_module_from_path(
    "build_docs_site", REPO_ROOT / "scripts" / "build_docs_site.py"
)

# A stand-in published set: deliberately NOT `_published_set(docs/)`, so a
# rename under docs/how-to/ can never quietly turn these cases into a test of
# a different rule. The real set is checked separately, below.
PUBLISHED = {
    "design.md",
    "retrieval.md",
    "storage.md",
    "externals.md",
    "brand.md",
    "how-to/README.md",
    "how-to/config-change.md",
}
BASE = build_docs_site.GITHUB_BASE

# docs/stack.md "Published / excluded page sets" -- the EXCLUDED half, spelled
# out so the partition test below can force an explicit call on any NEW
# top-level docs page rather than silently dropping it from the site.
EXCLUDED = {
    "decisions.md",
    "agents-workflow.md",
    "stack.md",
    "conventions.md",
    "release.md",
    "test-suite-audit.md",
    "onboarding.md",
    "keybindings.md",
    "tui.md",
    "editing.md",
    "configuration.md",
    # lode-fhql.15's derived reference pages -- exist in docs/ but are not
    # yet wired into the PUBLISHED set. docs/stack.md ("Publish-scope wiring
    # is a follow-up") explicitly defers that to lode-gecm, blocked on both
    # this ticket and .15; unpublished-for-now, not an oversight.
    "keymap.md",
    "settings.md",
}


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        # Published -> published: left alone, both on GitHub and on the site.
        ("design.md", "storage.md", None),
        ("design.md", "storage.md#the-async-work-queue", None),
        ("how-to/README.md", "config-change.md", None),
        ("how-to/README.md", "../design.md", None),
        # Absolute / non-file targets are never touched.
        ("design.md", "https://example.com/x", None),
        ("design.md", "mailto:someone@example.com", None),
        ("design.md", "#a-same-page-anchor", None),
        # Published -> unpublished: the one rewrite rule.
        ("design.md", "decisions.md", f"{BASE}/docs/decisions.md"),
        (
            "design.md",
            "configuration.md#models",
            f"{BASE}/docs/configuration.md#models",
        ),
        ("how-to/README.md", "../stack.md", f"{BASE}/docs/stack.md"),
        # Repo-root and source files -- one level up out of docs/.
        ("design.md", "../README.md", f"{BASE}/README.md"),
        ("design.md", "../src/lode/config.py", f"{BASE}/src/lode/config.py"),
        ("design.md", "./decisions.md", f"{BASE}/docs/decisions.md"),
        # Unenumerated material is unpublished by default (stack.md: PUBLISHED
        # is the authoritative list, EXCLUDED is only commentary).
        ("design.md", "research/notes.md", f"{BASE}/docs/research/notes.md"),
        # Escapes the repo: no blob URL can express it, so leave it verbatim
        # rather than emit a confidently-wrong link.
        ("design.md", "../../elsewhere/x.md", None),
    ],
)
def test_rewrite_target(current: str, target: str, expected: str | None) -> None:
    assert build_docs_site._rewrite_target(current, target, PUBLISHED) == expected


def test_links_in_code_are_never_rewritten() -> None:
    """A markdown example inside a fence or backticks is documentation.

    Rewriting it would corrupt the published page, and nothing downstream
    reports that -- the site would simply show the wrong thing.
    """
    text = (
        "Real: [decisions](decisions.md)\n"
        "Inline: `[decisions](decisions.md)` stays verbatim\n"
        "```markdown\n"
        "[decisions](decisions.md)\n"
        "```\n"
    )
    out = build_docs_site._process_links(text, "design.md", PUBLISHED)
    assert f"Real: [decisions]({BASE}/docs/decisions.md)" in out
    assert "Inline: `[decisions](decisions.md)` stays verbatim" in out
    assert out.count("(decisions.md)") == 2  # the inline span and the fenced one


def test_real_links_survive_the_code_skip() -> None:
    """The two forms the code-skip must NOT swallow.

    Both were live regressions caught against the real docs corpus: a link
    whose LABEL is backticked (the dominant form in these docs) starts outside
    code even though it contains code, and a link whose label wraps across
    lines is only matchable against the whole text, never line by line.
    """
    backticked = "see [`decisions.md`](decisions.md) for what is open\n"
    assert (
        f"[`decisions.md`]({BASE}/docs/decisions.md)"
        in build_docs_site._process_links(backticked, "design.md", PUBLISHED)
    )
    wrapped = "see the [two-phase batch\ncontract](stack.md#llm-provider-seam) here\n"
    assert (
        f"contract]({BASE}/docs/stack.md#llm-provider-seam)"
        in build_docs_site._process_links(wrapped, "design.md", PUBLISHED)
    )


def test_published_and_excluded_partition_the_top_level_docs() -> None:
    """Every top-level docs page is an explicit publish-or-exclude call.

    The dangerous direction is ADDING a page: a removed one already fails the
    build loudly (``build()`` raises on a listed-but-missing file), while a new
    maintainer doc would otherwise be dropped from the site with nothing said.
    """
    on_disk = {p.name for p in (REPO_ROOT / "docs").glob("*.md")}
    unclassified = on_disk - set(build_docs_site.PUBLISHED_TOP_LEVEL) - EXCLUDED
    assert not unclassified, (
        f"docs/{{{', '.join(sorted(unclassified))}}} is neither in "
        "build_docs_site.PUBLISHED_TOP_LEVEL nor in this test's EXCLUDED set. "
        "Decide which it is (docs/stack.md 'Published / excluded page sets') "
        "and add it to one of them."
    )


def test_published_set_matches_the_docs_on_disk() -> None:
    """Every enumerated page exists, and how-to/ is published as a directory.

    docs/stack.md publishes ``docs/how-to/`` as a DIRECTORY, not a frozen file
    list -- a guide added there later must be picked up automatically.
    """
    docs_dir = REPO_ROOT / "docs"
    published = build_docs_site._published_set(docs_dir)
    for rel in published:
        assert (docs_dir / rel).is_file(), f"published page missing on disk: {rel}"
    on_disk = {
        p.relative_to(docs_dir).as_posix() for p in (docs_dir / "how-to").rglob("*.md")
    }
    assert on_disk <= published
    assert EXCLUDED & published == set()


def test_mermaid_fences_are_replaced_by_image_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rendered fence must become an image link, never survive as a fence.

    Docker is not available in every test environment, so the renderer itself
    is stubbed -- what is pinned here is the substitution and the page-relative
    SVG path, which is what a client-side-Mermaid regression would break.
    """
    rendered: list[Path] = []
    monkeypatch.setattr(
        build_docs_site,
        "_render_mermaid_svg",
        lambda code, out: rendered.append(out),
    )
    out = build_docs_site._process_mermaid(
        "intro\n\n```mermaid\ngraph TD;\nA-->B;\n```\n",
        "how-to/README.md",
        tmp_path / "assets" / "mermaid",
        tmp_path,
    )
    assert "```mermaid" not in out
    assert "![Diagram](../assets/mermaid/how-to-README-1.svg)" in out
    assert rendered == [tmp_path / "assets" / "mermaid" / "how-to-README-1.svg"]


def test_workflow_pins_match_their_sources() -> None:
    """docs.yml duplicates two pins; nothing but this test keeps them honest.

    A bumped ``MERMAID_IMAGE`` that misses the workflow's ``docker pull`` makes
    CI pull one image and render with another -- silently, since ``docker run``
    fetches on demand.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "docs.yml").read_text(
        encoding="utf-8"
    )
    assert build_docs_site.MERMAID_IMAGE in workflow, (
        f"docs.yml must pull {build_docs_site.MERMAID_IMAGE} -- the pin "
        "scripts/build_docs_site.py renders with."
    )
    lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    typer_pin = next(
        line.split()[0] for line in lock.splitlines() if line.startswith("typer==")
    )
    assert typer_pin in workflow, (
        f"docs.yml installs typer for scripts/build_docs_site.py; pin it at "
        f"{typer_pin} to match requirements.lock."
    )


def test_docs_workflow_never_deploys_off_trunk() -> None:
    """The Pages deploy must be unreachable from a PR or a land/** push."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
    )
    expected = "github.event_name == 'push' && github.ref == 'refs/heads/trunk'"
    assert workflow["jobs"]["deploy"]["if"] == expected
    upload = [
        step
        for step in workflow["jobs"]["build"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-pages-artifact")
    ]
    assert [step["if"] for step in upload] == [expected], (
        "the artifact-upload step's condition must stay byte-identical to the "
        "deploy job's -- see the comment on both."
    )
