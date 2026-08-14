# Security Policy

This document describes how to report security vulnerabilities in this project
and what you can expect from us in return. It applies to the open-source core of
Community Knowledge Sharing published in this repository
(`jakobengdahl/CommunityOverview`).

We follow a **coordinated vulnerability disclosure** model: report privately,
give us a reasonable window to investigate and remediate, and we will work with
you toward a coordinated public disclosure. This policy is also part of how the
project works toward the reporting and handling expectations set out in the EU
Cyber Resilience Act (CRA).

## Reporting a Vulnerability

**Please do not open a public issue, pull request, or discussion for security
vulnerabilities.** Public reports expose other users before a fix is available.

Report privately through **GitHub's private vulnerability reporting**:

1. Go to the [**Security** tab](https://github.com/jakobengdahl/CommunityOverview/security)
   of this repository.
2. Click **Report a vulnerability** to open a private advisory that is visible
   only to you and the maintainers.

If you are unable to use GitHub's private reporting, open a regular GitHub issue
that contains **no technical details** — simply ask a maintainer for a private
channel — and we will follow up.

### What to include

A good report helps us reproduce and fix the issue quickly. Where possible,
please include:

- The affected component, endpoint, or file, and the version, branch, or commit
  you tested.
- A description of the vulnerability and its potential impact.
- Step-by-step reproduction instructions or a proof of concept.
- Any relevant configuration, logs, or screenshots (with secrets redacted).
- Suggested remediation, if you have one.

Please report in **English** where possible.

## Our Commitment — Response Times

We aim to meet the following targets. They are goals, not contractual
guarantees; this is a maintainer-led open-source project and response speed may
vary with maintainer availability.

| Stage | Target |
|---|---|
| **Acknowledgement** of your report | within **3 business days** |
| **Initial assessment** (triage, severity, validity) | within **10 business days** |
| **Status updates** during remediation | at least every **14 days** |
| **Fix or mitigation** for confirmed valid reports | as soon as practical, prioritised by severity |

We will keep you informed of our progress and let you know when the issue is
resolved.

## Coordinated Disclosure

- We ask that you give us a reasonable period to investigate and release a fix
  before any public disclosure. A commonly used window is **90 days** from the
  acknowledgement date; we are happy to agree on a different timeline with you
  based on severity and complexity.
- We will coordinate the timing of any public disclosure with you and, where
  appropriate, publish a **GitHub Security Advisory** and release notes.
- With your consent, we will **credit you** for the discovery in the advisory.
  If you prefer to remain anonymous, let us know.
- If a vulnerability is being actively exploited, we may accelerate disclosure to
  protect users.

## Scope

### In scope

- Source code in this repository (backend services, frontend, packages, the MCP
  OAuth gateway, and build/config code under version control here).
- Vulnerabilities that affect the confidentiality, integrity, or availability of
  a deployment running this code — for example authentication or authorization
  flaws, injection, remote code execution, server-side request forgery, insecure
  deserialization, or exposure of sensitive data.

### Out of scope

The following are generally **not** eligible under this policy. Reports may still
be closed as informative:

- Findings that require physical access to a user's device, or that rely on a
  compromised host, browser, or already-privileged account.
- Social engineering, phishing, or attacks against project maintainers or
  contributors.
- Denial of service through volumetric traffic, or resource-exhaustion issues
  that require unrealistic request volumes.
- Vulnerabilities exclusively in third-party dependencies — please report those
  upstream. If a dependency issue is exploitable **through this project's own
  code**, we do want to hear about it.
- Missing security hardening headers, cookie flags, or best-practice
  recommendations with no demonstrated concrete impact.
- Issues affecting only unsupported, self-modified, or end-of-life
  configurations.
- Reports from automated scanners without a validated, reproducible finding.

Operators are responsible for the security of their own deployments, including
secret management, network exposure, transport security, and the configuration
of any external LLM or storage providers they connect. See
[`.env.example`](.env.example) and the documentation under
[`docs/`](docs/) for configuration guidance.

## Safe Harbor

We support good-faith security research and will not pursue or support legal
action against you for security research and vulnerability disclosure conducted
in accordance with this policy. Specifically, if you make a good-faith effort to
comply with this policy during your research, we will consider your research to
be **authorized**, and we will:

- Not initiate or recommend legal action against you for accidental, good-faith
  violations of this policy.
- Work with you to understand and resolve the issue promptly.
- Recognize your contribution, with your permission, if you are the first to
  report a previously unknown, valid issue.

To stay within this safe harbor, you must:

- Make a good-faith effort to **avoid privacy violations, data destruction, and
  interruption or degradation** of services and systems.
- Only interact with systems and accounts **you own or are explicitly authorized
  to test.** Do not access, modify, or exfiltrate data belonging to others.
- **Not exploit** a vulnerability beyond the minimum necessary to demonstrate it,
  and stop and report as soon as you confirm it.
- **Not disclose** the vulnerability publicly until we have coordinated a
  disclosure with you.
- Comply with all applicable laws.

This safe harbor applies to the source code published in this repository. It does
**not** authorize testing against third-party services, or against hosted or
production systems that you do not own or operate — obtain separate authorization
from the relevant operator before testing any live deployment.

## Supported Versions

This is an actively developed project. Security fixes are applied to the latest
released version on the default branch. We do not, as a general rule, backport
fixes to older versions; operators are encouraged to track the latest release.

---

Thank you for helping keep Community Knowledge Sharing and its users safe.
