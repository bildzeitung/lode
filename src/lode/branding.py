"""lode's terminal wordmark (lode-fhql.5) -- the mark that renders where lode
actually lives: the TUI's help overlay
(:class:`~lode.tui.screens.help.HelpScreen`, which carries the placement
rationale) and the CLI's ``lode version`` output.

One hand-drawn string, not a font-rendering routine -- there are exactly two
callers and the source of truth for the *glyphs* is
:data:`WORDMARK_UNICODE`, read verbatim: "LODE" in the Unicode full block
character (U+2588). :data:`WORDMARK_ASCII` is **derived** from it by a
single glyph substitution rather than hand-copied, so the two forms cannot
drift apart into different letterforms or different widths. Both are
therefore exactly 22 columns wide and five rows tall -- comfortably inside
the 80-column hard limit this ticket calls out (``CaptureScreen`` overflowed
it once, lode-3rvw; ``tests/test_branding.py`` pins the footprint so it
can't repeat that silently).

**Selection is explicit, never hopeful** (the ticket's own wording):
:func:`supports_unicode` decides from :data:`sys.stdout`'s *encoding* before
anything is printed, rather than emitting the Unicode form and discovering a
``UnicodeEncodeError`` after some of it already reached the terminal. That
detection is for the **CLI** path; a caller that already knows its render
target's capability (the TUI does -- Textual owns the terminal and paints
non-ASCII chrome unconditionally) passes ``unicode=`` and bypasses it.

**No colour, ever, in this module.** The glyphs are plain characters with no
ANSI escape codes attached -- NO_COLOR and a non-TTY stdout need no special
handling here because there is no colour to suppress; a pipe receiving this
string gets exactly the same bytes a terminal does. Callers that want colour
around the wordmark (e.g. a themed CLI banner) apply it externally through
their own ``rich`` ``Console``, which already handles NO_COLOR/TTY detection
for everything else that Console prints.
"""

from __future__ import annotations

import sys
from typing import TextIO

#: "LODE" drawn in Unicode full-block glyphs -- four letter-cells (L O D E)
#: with a 2-column gap between letters, 22 columns x 5 rows total.
WORDMARK_UNICODE = (
    "█      ██   ███   ████\n"
    "█     █  █  █  █  █   \n"
    "█     █  █  █  █  ███ \n"
    "█     █  █  █  █  █   \n"
    "████   ██   ███   ████"
)

#: The same letterforms in plain ASCII -- the explicit fallback for a
#: terminal/encoding that can't render the block glyphs above. DERIVED from
#: :data:`WORDMARK_UNICODE` by one glyph substitution rather than copied by
#: hand, so the 22x5 footprint is identical by construction and callers
#: never need to special-case layout based on which form was picked.
WORDMARK_ASCII = WORDMARK_UNICODE.replace("█", "*")

#: lode's one-line tagline, shown under the wordmark. Deliberately its own
#: string, NOT the package description (``pyproject.toml``) or the CLI root
#: ``help=`` -- this is the mark's tagline and is free to be shorter than
#: either. Deliberately plain ASCII too -- no em dash, no typographic quotes
#: -- so it needs no unicode/ascii pair of its own and prints identically
#: under both forms of the wordmark above (``tests/test_branding.py`` pins
#: that).
TAGLINE = "capture fast, retrieve cited"


def supports_unicode(stream: TextIO | None = None) -> bool:
    """Return whether ``stream`` (default :data:`sys.stdout`) declares a
    UTF-* encoding -- the explicit check :func:`wordmark` uses to pick
    :data:`WORDMARK_UNICODE` vs :data:`WORDMARK_ASCII`, rather than emitting
    the Unicode form and hoping it lands intact.

    A stream with no ``encoding`` attribute (or an empty one -- some
    in-memory/test streams report this) is treated as NOT supporting
    Unicode: the ASCII form is the safe default when the encoding is
    unknown, not the Unicode one.
    """
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or ""
    return "utf" in encoding.lower()


def wordmark(*, unicode: bool | None = None) -> str:
    """Return lode's wordmark plus :data:`TAGLINE`, ready to print as-is --
    no colour/markup embedded, so both CLI and TUI callers are free to wrap
    it in their own styling (or none).

    ``unicode`` overrides detection outright when passed explicitly -- for a
    caller that already knows its render target's capability (the TUI) or a
    test pinning one form regardless of environment. Left as the default
    ``None``, the form is picked by :func:`supports_unicode` against
    :data:`sys.stdout`, which is the right target for the CLI path.
    """
    use_unicode = supports_unicode() if unicode is None else unicode
    glyphs = WORDMARK_UNICODE if use_unicode else WORDMARK_ASCII
    return f"{glyphs}\n{TAGLINE}"
