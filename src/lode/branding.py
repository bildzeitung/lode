"""lode's terminal wordmark (lode-fhql.5) -- the mark that renders where lode
actually lives: the TUI's config/diagnostics screen and the CLI's ``lode
version`` output.

Two fixed strings, not a font-rendering routine -- there are exactly two
callers and the source of truth for the *glyphs* is this module, read
verbatim. :data:`WORDMARK_UNICODE` draws "LODE" with Unicode full/half block
characters (U+2588/U+2591 family); :data:`WORDMARK_ASCII` draws the same
letterforms with plain ``*`` so it degrades on a terminal/encoding that
cannot render the block glyphs. Both are exactly 22 columns wide, five rows
tall -- comfortably inside the 80-column hard limit this ticket calls out
(``CaptureScreen`` overflowed it once, lode-3rvw; this module is checked by
``tests/test_branding.py`` so it can't repeat that silently).

**Selection is explicit, never hopeful** (the ticket's own wording):
:func:`supports_unicode` decides from the target stream's *encoding* before
anything is printed, rather than emitting the Unicode form and discovering a
``UnicodeEncodeError`` after some of it already reached the terminal.
:func:`wordmark` is the one call site both callers use; passing
``unicode=True/False`` bypasses detection entirely for a caller (e.g. a
test) that wants a specific form regardless of environment.

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

#: "LODE" drawn in Unicode full-block glyphs, 5 letter-cells wide (L O D E)
#: with a 2-column gap between letters, 22 columns x 5 rows total.
WORDMARK_UNICODE = (
    "█      ██   ███   ████\n"
    "█     █  █  █  █  █   \n"
    "█     █  █  █  █  ███ \n"
    "█     █  █  █  █  █   \n"
    "████   ██   ███   ████"
)

#: The same letterforms in plain ASCII (``*`` for the Unicode form's ``█``)
#: -- the explicit fallback for a terminal/encoding that can't render the
#: block glyphs above. Same 22x5 footprint, so callers never need to
#: special-case layout based on which form was picked.
WORDMARK_ASCII = (
    "*      **   ***   ****\n"
    "*     *  *  *  *  *   \n"
    "*     *  *  *  *  *** \n"
    "*     *  *  *  *  *   \n"
    "****   **   ***   ****"
)

#: lode's one-line tagline, shown under the wordmark. Plain ASCII already --
#: an em dash is the only non-ASCII byte a truly minimal terminal might
#: balk at, so it's spelled out as a hyphen instead rather than adding a
#: second unicode/ascii pair for one punctuation mark.
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


def wordmark(*, unicode: bool | None = None, stream: TextIO | None = None) -> str:
    """Return lode's wordmark plus :data:`TAGLINE`, ready to print as-is --
    no colour/markup embedded, so both CLI and TUI callers are free to wrap
    it in their own styling (or none).

    ``unicode`` overrides detection outright when passed explicitly (e.g. a
    test pinning one form regardless of environment); left as the default
    ``None``, the form is picked by :func:`supports_unicode` against
    ``stream`` (default :data:`sys.stdout`).
    """
    use_unicode = supports_unicode(stream) if unicode is None else unicode
    glyphs = WORDMARK_UNICODE if use_unicode else WORDMARK_ASCII
    return f"{glyphs}\n{TAGLINE}"
