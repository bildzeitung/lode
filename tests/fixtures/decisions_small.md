# Fixture decisions log

Preamble line one, not an entry.
A second preamble line with **Update (lode-pre1)** text that is not migrated.

- **Cache sizing: open (lode-aaa1, 2026-01-05).** Leaning on a small cache.
  The details run on for a second line.
  **Update (lode-aaa2, 2026-01-09):** decided, a small cache.

- **Retry policy (lode-bbb1).** Retry twice, then give up. No date in the head.
  Later the body mentions 2026-02-03 once.

- **Open question: log rotation (lode-ccc1).** Nobody has ruled yet.

- **Fenced example (lode-ddd1, 2026-01-20).** The fence below holds lines that
  look like entry starts and must not split the entry:

  ```
  - **not an entry**
  ## not a section either
  ```

  End of the fenced example.

## First section (decided, lode-eee1)

The section text, dated 2026-03-01.

  **Update (lode-eee2, 2026-03-02):** an indented marker under the section.

- **A bullet inside the section (lode-eee3).** A column-zero bullet after a
  section's own text is a sub-point of the section, part of its entry.

## Second section (lode-fff1)

Text of the second section; no date anywhere.

  **Update (lode-aaa3, 2026-03-05):** the cache ruling above was refined. This
  marker was appended here by mistake and belongs to the lode-aaa1 entry.
  Its second line.
  **Update (lode-fff2, 2026-03-06):** pointer saying the lode-aaa3 marker is
  misfiled.

  **Update (lode-fff3, 2026-03-07):** this one is the section's own.

- **Update (lode-ggg1, 2026-03-10) — a correction filed as its own entry.** It
  corrects the retry policy entry above.

## First section (decided, lode-eee1)

A heading repeated word for word, so its anchor takes the -1 suffix.
