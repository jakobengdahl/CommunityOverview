# Anydoc attachment extraction evaluation

## Recommendation

Keep the current extractor. This is a partial evaluation, not approval
for a switch. Anydoc is not installed in the evaluation environment. Public
release metadata and a native wheel archive were inspected over the network
for the supply-chain assessment below; no attachment content was transmitted. No candidate
conversion, hosted OCR, external service, or private document access was attempted.
A switch requires an explicit owner decision after the missing measurements and
integration checks below. There is no runtime, dependency, configuration, or
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
conversion behavior in this evaluation.

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

Public supply-chain review on 2026-11-17 scoped the compiled Python distribution
`firecrawl-anydoc==0.2.4` (import name `anydoc`), rather than an unrelated package
named `anydoc`. Only public source, registry metadata and wheel bytes were fetched;
the wheel was read as a ZIP in memory, never installed, imported or executed.

| Supply-chain item | Checked evidence and assessment |
| --- | --- |
| License | Tagged core LICENSE and Python Cargo manifest declare MIT; wheel METADATA declares `License-Expression: MIT`. The wheel's CycloneDX SBOM lists 113 components with permissive expressions, including Apache-2.0, BSD-3-Clause, Unicode-3.0, Zlib and the LLVM exception. No separate LICENSE file is present in this wheel: notice redistribution needs resolution before adoption. SBOM declarations are not an independent audit of bundled code. |
| Maintainers | Repository is owned by Firecrawl; tagged LICENSE names Sideguide Technologies Inc. The public contributor list is dominated by `tomsideguide` (118 contributions), followed by `nickscamara` (6) and `abimaelmartell` (4), indicating concentrated maintenance. PyPI author/maintainer fields are empty; its provenance identifies the Firecrawl repository's release workflow as publisher. This establishes project/publication identity, not a support commitment or independently verified human identities. |
| Release provenance | GitHub tag `v0.2.4` resolves to `42bf1c5ecdde9eb0d96d6bd75a9e6698cf93b14c`. The inspected wheel's downloaded SHA-256 matches PyPI and the attestation subject (below). PyPI provides a GitHub publisher attestation for `firecrawl/anydoc`, `release.yml`; the certificate records that tag and commit. The tagged workflow builds with maturin and `--locked`. This supports publication traceability; attestation signatures/transparency proofs were not independently cryptographically verified, nor was a reproducible rebuild or vulnerability audit performed. |
| Wheels and platforms | Registry files include `cp310-abi3` wheels for manylinux glibc 2.17+ x86_64/aarch64, musllinux 1.2 x86_64/aarch64, macOS x86_64/arm64 and Windows amd64. The Linux x86_64 stable-ABI wheel covers CPython 3.11 on CI's `ubuntu-latest` and the `python:3.11-slim` Linux/amd64 image built by CI. Compatibility is established by published tags, not an installation or native-load test in those environments. |

The inspected artifact is
`firecrawl_anydoc-0.2.4-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`,
SHA-256 `0e5aed01bf6d4e5c588d3363888293f2ceeb9feb00449a32e0ba993797cf0bd3`.

Sources: [versioned registry metadata and artifact hashes](https://pypi.org/pypi/firecrawl-anydoc/0.2.4/json),
[wheel publication provenance](https://pypi.org/integrity/firecrawl-anydoc/0.2.4/firecrawl_anydoc-0.2.4-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl/provenance),
[tagged license](https://github.com/firecrawl/anydoc/blob/v0.2.4/LICENSE),
[Python manifest](https://github.com/firecrawl/anydoc/blob/v0.2.4/python/Cargo.toml),
[release workflow](https://github.com/firecrawl/anydoc/blob/v0.2.4/.github/workflows/release.yml),
[tag reference](https://api.github.com/repos/firecrawl/anydoc/git/ref/tags/v0.2.4),
and [contributors](https://api.github.com/repos/firecrawl/anydoc/contributors).
Local platform evidence: [CI workflow](../../.github/workflows/ci.yml) and
[runtime image](../../backend/Dockerfile).

Supply-chain assessment: identifiable publisher, permissive declared licensing,
publication provenance and compatible wheel availability support further local
benchmarking. They do not yet justify adoption: resolve licence notices, verify
attestation trust and audit dependencies before integration. Do not add anydoc to
runtime requirements on the strength of this document. Candidate conversion and
sandbox measurements remain unmeasured; the owner's switch decision remains pending.

## Verification and remaining decision evidence

The existing extraction tests were run with a targeted selection covering format
output, page locators, no-text PDFs, ZIP ratios, address-space isolation and
CPU/file-size limits: 17 passed. A regression test in the existing extraction test module checks that this
evaluation retains concrete supply-chain evidence and adoption boundaries.
Production extraction and requirements files are unchanged.

Before an owner can assess a switch, the missing evidence is a fully audited candidate
artifact, valid generated examples for every claimed format (including a real
EPUB), side-by-side quality and peak-memory measurements, and verified citation
and sandbox compatibility. Real private-document evaluation was deliberately
excluded; synthetic results cannot establish quality on those documents. Keep
auto-merge off and leave the adoption decision with the owner.
