# Security Policy

## Supported versions

knowbase is pre-1.0 and under active development. Security fixes are applied
only to the latest `master`; there are no maintained release branches yet.
Please reproduce any report against current `master` before filing it.

| Version            | Supported          |
| ------------------ | ------------------ |
| `master` (latest)  | :white_check_mark: |
| Tagged `0.x` / older commits | :x:      |

## Reporting a vulnerability

Please report security vulnerabilities **privately**. Do not open a public
issue, pull request, or discussion for anything you believe is a security
problem.

Use either of the following:

- **GitHub private vulnerability reporting** — go to the repository's
  [Security tab](https://github.com/v0ropaev/knowbase/security/advisories) and
  choose *Report a vulnerability*. This is the preferred channel.
- **Email** — send the details to **dy.voropaev@gmail.com**. If you wish to
  encrypt the report, request our public key in your first message.

A useful report includes:

- the affected component or module (for example `kb.store`, `kb.git`, the
  daemon, or a future MCP/introspection feature),
- the commit SHA you tested against,
- a description of the impact and, where possible, a minimal proof of concept
  or reproduction steps,
- any relevant configuration (database URL form, environment variables, etc.)
  with secrets redacted.

## Response window

This is a volunteer-maintained project, so timelines are best-effort:

- **Acknowledgement:** within **5 business days** of your report.
- **Initial assessment** (severity and whether it is in scope): within
  **10 business days**.
- **Fix or mitigation:** prioritized by severity; we will keep you updated on
  progress and coordinate a disclosure date with you.

We will credit reporters in the advisory unless you ask to remain anonymous.

## Scope and threat-model notes

knowbase reads a git repository and stores derived knowledge in PostgreSQL.
A few characteristics are worth calling out so reports can be triaged quickly.

**In scope**

- Code-execution, injection, or data-corruption issues in the indexing and
  extraction pipeline (`kb.git`, `kb.structural`, `kb.extract.*`, `kb.store`).
- SQL injection or unsafe query construction reaching PostgreSQL. The daemon
  **connects to a database** using a caller-supplied connection URL; that URL
  and any credentials in it are trusted input, but the data written to and read
  from the database must never allow a crafted repository to corrupt the store
  or escape parameterization.
- Breaking the **provenance / anti-hallucination invariant** in a way that lets
  ungrounded or forged knowledge be stored or served (this is enforced in-app
  and by a database trigger; bypasses are treated as security issues).
- Path-traversal, resource-exhaustion, or denial-of-service vectors triggered
  by a malicious repository, blob, or git history during indexing.
- Vulnerabilities in the future read-only MCP server and HTTP/API surface once
  those land on `master`.

**Out of scope (or known limitations)**

- The PostgreSQL instance, its network exposure, and its credentials are the
  operator's responsibility. Pointing the daemon at an untrusted or hostile
  database server is not a supported configuration.
- Indexing a repository you do not trust on a host you care about: extractors
  parse untrusted source but the security boundary is the OS/database, not the
  parser. Run untrusted indexing in an isolated environment.
- Vulnerabilities solely in third-party dependencies should be reported
  upstream; we will, however, take updating or pinning them seriously and
  appreciate a heads-up.

**Planned, security-sensitive feature**

A future **runtime-introspection oracle** is on the roadmap. Unlike the current
deterministic and parse-only extractors, that component would **execute code
from the target repository** to observe real behavior. It MUST run inside a
sandbox (no ambient network, filesystem, or credential access; resource limits
enforced) and will ship disabled by default. Until it exists and is documented
as sandboxed, treat any code path that imports or runs target-repository code
as a serious finding and report it.

## Disclosure

We follow coordinated disclosure. Please give us a reasonable window to ship a
fix before any public discussion. We will work with you on timing and publish
an advisory once a fix or mitigation is available.
