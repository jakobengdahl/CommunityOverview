# Anydoc attachment extraction evaluation

## Recommendation

Keep the current extractor. This is a partial, offline evaluation, not approval
for a switch. Anydoc is not installed in the evaluation environment; installing
or checking upstream artifacts needs network access and was skipped. No candidate
conversion, hosted OCR, external service, or private document access was attempted.
A switch requires an explicit owner decision after the missing measurements and
supply-chain checks below. There is no runtime, dependency, configuration, or
rollout change in this evaluation.

## Evidence and sample boundaries

Measured on 2026-11-17 with local Python 3.12 on Linux. The checkout matched the
locally available `origin/main`; no existing anydoc evaluation was found there.
Refreshing remote main was skipped because it requires network access.

All inputs were generated locally, using fixture builders in
[the existing extraction tests](../../backend/core/tests/test_attachment_extract.py),
plus a PDF assembled with pypdf text operators and an image-only PDF made with
Pillow. No public downloads or private attachments were used. The PDF table had a
heading, three columns, a header row and 30 data rows. The scan contained raster
text only. DOCX had two paragraphs and a two-column table; XLSX a named sheet;
PPTX two slides; ODT a heading and paragraph; ODS a named two-cell table; ODP one
slide; CSV a header and one data row. Office fixtures other than DOCX are minimal
parser fixtures, not representative exports from an office application. RTF was
a tiny text document; EPUB was only a MIME-bearing ZIP stub, not a valid book.
Their unsupported baseline results do not evaluate document fidelity.

Each sample was passed three times to the public `extract` function with default
limits. Times below are median elapsed milliseconds, including interpreter and
parser startup, without a warmup. These tiny samples and one machine do not
establish throughput or comparative performance. Fixtures and the temporary
measurement harness are not shipped.

| Generated sample | Current result | Segments / locator | Median ms |
| --- | --- | --- | ---: |
| Table-heavy PDF | `ok` | 1 / page 1 | 192.9 |
| Image-only PDF | `no_text_layer` | 0 | 209.0 |
| DOCX | `ok` | 3 / paragraphs 1–2, table 1 | 224.3 |
| XLSX | `ok` | 1 / sheet Data | 96.6 |
| PPTX | `ok` | 2 / slides 1–2 | 108.3 |
| ODT | `ok` | 2 / paragraphs 1–2 | 90.3 |
| ODS | `ok` | 1 / sheet Data | 91.7 |
| ODP | `ok` | 1 / slide 1 | 93.7 |
| CSV | `ok` | 1 / line 1 | 84.2 |
| RTF / EPUB stub | `unsupported_type` | 0 | <0.1 each |
| Office-shaped compression bomb | `too_large`, `zip_ratio` | 0 | 90.7 |

The bomb is a 20 MiB zero-filled ZIP member alongside Office-shaped metadata,
compressed to about 20 KiB. It is a rejection probe, not a valid Office document.

## Comparison and integration constraints

The candidate brief describes a local Rust converter with Python bindings,
Markdown output, PDF/Word/PowerPoint/Excel/OpenDocument/RTF/EPUB/CSV support, and
`NeedsOcr` for scans. These are supplied claims, not independently verified
upstream facts in this offline evaluation.

| Dimension | Current extractor evidence | Anydoc evidence / missing work |
| --- | --- | --- |
| Text and headings | Generated text retained; PDF and ODT headings are plain text, without heading semantics | Not measured; compare heading hierarchy, reading order, Unicode and repeated headers |
| Tables | PDF rows and values retained as space-separated text; DOCX/XLSX/ODS retain tab-separated cells | Not measured; check Markdown cell association, merged cells, multiline cells and escaping |
| Speed | Startup-inclusive sample timings above | Not measured; run identical valid files, interpreter and limits before comparing |
| Memory | Default address-space limit enabled; peak RSS not measured | Neither peak RSS nor native allocation behavior measured |
| Scans | Raster-only PDF returns `no_text_layer` | `NeedsOcr` expected from brief, not observed; must map to `no_text_layer` without retrying remotely |
| Limits and hostile files | Bomb refused; targeted existing isolation tests pass | Native import, conversion, output growth and bomb behavior not measured |
| Segments and citations | Page, paragraph, table, sheet, slide and line locators observed | Markdown can be split into bounded text blocks, but page provenance is unverified |

The current child has a 30 s wall-clock budget, CPU limit of 31 s, 1 GiB address
space, zero file-size allowance, 25 MiB input cap, one million output characters
and 10,000 segments. Its reply cap is `6 * max_chars + 512 * max_segments + 4096`
bytes. ZIP checks cap members at 2,000, declared expansion at 100 MiB and ratios
at 100, including per-member checks above 1 MiB. These must remain in front of
any candidate parser. Check XML DTD/entity handling and external relationships as
well; a new parser must not bypass the current bounded member reads. Test whether
native loading or temporary-file creation conflicts with address-space and
file-size limits. None of these candidate compatibility checks has been run.

Markdown headings or blank lines are not evidence of PDF page boundaries. A
future adapter needs verified page metadata or conversion per original page;
never infer page numbers from heading counts. Likewise, retain verified sheet
and slide identities. Apply segment, locator, character and reply bounds before
returning plain text to the parent. Markdown output alone does not establish
citation compatibility or a safe replacement for the existing parsers.

## Security and supply-chain assessment

[ADR 0010 D1/D2 and extraction isolation](../adr/0010-attachment-content-access-for-agents-and-assistant.md)
require local extraction and local embeddings. Hosted OCR must be impossible to
select: a future adapter should expose no OCR mode, endpoint, credential, or
arbitrary converter options; use only a reviewed local API and fail closed on
`NeedsOcr`. Do not forward environment variables or document-controlled options.
An explicit local flag alone is insufficient: verify the pinned native code has
no automatic remote fallback, and enforce denied network egress for the child.
The current empty environment and process/resource limits do not themselves
isolate networking. Verify attempted egress is blocked and scans never trigger
remote conversion before considering integration.

| Supply-chain item | Finding and adoption requirement |
| --- | --- |
| License | MIT is claimed in the candidate brief; package license, native core and bundled dependency licenses were not inspected |
| Maintainers | Candidate is identified as firecrawl/anydoc in the brief; publisher identity, maintainer history and continuity were not verified |
| Release provenance | No release, source revision, artifact hash, build attestation, dependency inventory or vulnerability audit was obtained; require source-to-wheel traceability and review native/transitive code before adoption |
| Wheels and platforms | CI uses `ubuntu-latest` with Python 3.11; runtime uses `python:3.11-slim`. Actual wheel tags, architecture/glibc compatibility and installation inside the runtime image were not verified; local Python 3.12 results do not establish that compatibility |

Supply-chain approval is blocked, not passed. Do not add anydoc to runtime
requirements on the strength of this document. Network-dependent artifact review
and candidate benchmarks were skipped; no release or checksum is guessed here.

## Verification and remaining decision evidence

The existing extraction tests were run with a targeted selection covering format
output, page locators, no-text PDFs, ZIP ratios, address-space isolation and
CPU/file-size limits: 17 passed. No tests were added for this documentation-only
change, and production extraction and requirements files are unchanged.

Before an owner can assess a switch, the missing evidence is an audited candidate
artifact, valid generated examples for every claimed format (including a real
EPUB), side-by-side quality and peak-memory measurements, and verified citation
and sandbox compatibility. Real private-document evaluation was deliberately
excluded; synthetic results cannot establish quality on those documents. Keep
auto-merge off and leave the adoption decision with the owner.
