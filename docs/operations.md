# Operations

## Install and files

Use Python 3.11 or newer on Windows, Linux or macOS. The Python package has no runtime dependencies. Source installation and the demo commands are in the [README](../README.md). Tested platform versions and artifact hashes will accompany the release.

The default operational ledger is `.citeafter/ledger.sqlite`; the fixture demo uses `.citeafter/demo.sqlite`. An explicit `--db PATH` precedes the subcommand. `init` refuses existing files. Back up the database while no writer is active; preserve the original when investigating an integrity failure. This version has no migration, automatic repair or retention/deletion command. Choose a new demo path to repeat its initial journey.

The local UI opens the printed numeric loopback address; `localhost`, forwarded hosts and remote exposure are outside its server profile. Ctrl+C stops that owned server. No service is installed. Synthetic demo time remains fixed between its version steps and resumes from the last stored event; operational mode always uses the machine's current UTC clock.

## Publisher contract

The operator pins one endpoint at ledger initialization. HTTPS uses normal certificate verification. Plain HTTP is accepted only for numeric IPv4 loopback or `::1`; no DNS alias bypass. URLs cannot contain credentials, query or fragment. No redirects, retries, proxy configuration, ambient credentials or document-selected endpoints are used.

Supported 200 responses have JSON MIME (`application/json` or `application/*+json`), UTF-8, identity coding and exactly one decimal `Content-Length`. The declared body must arrive completely within the byte limit and wall deadline. **All Transfer-Encoding, including chunked, and close-delimited responses are refused.** Configure the publisher to return Content-Length. This deliberately narrow client is not a general web scraper.

Maximum body: 256 KiB. Default fetch deadline: 2 seconds, including the isolated worker's DNS, TLS, headers and body. The parent terminates and reaps only its own worker on expiry; process scheduling and cleanup can add overhead. No hard real-time bound is claimed. A 304 needs a prior conditional cache; it preserves the original source bytes and first observation. A concurrent changed cache requires another refresh.

The body must match the typed policy schema. Unknown fields, duplicate keys, nonfinite numbers, invalid Unicode and malformed dates are refused. Oversized imports retain a bounded rejected prefix, explicitly labelled as such, and block new release until a successful check. Checking an old version does not establish freshness for a newer selected descendant; recheck the current representation. Policy observation time cannot be supplied in the body. Discount rates and money are bounded nonnegative decimal strings with at most two places; rates cannot exceed 100. Use explicit-offset timestamps.

## Recovery

| State | Next action |
| --- | --- |
| `stale` | Inspect changed required fields; recheck the supported pending action, then obtain a new preview. |
| `needs_review` | Inspect exact reason/source. Manual semantics and ambiguous lineage are not automatically releasable. |
| `source_unavailable` | Fix publisher/transport or reimport the correct original bytes, refresh successfully, then preview again. |
| `stale_preview` | Inspect current state and obtain a fresh preview. A changed generation can invalidate a preview without changing semantic support. |
| `clock_rollback` | Correct the operational clock; historical queries remain read-only. |
| `integrity` / `database_format` | Preserve the file for inspection; use the correct database/version. No automatic repair is attempted. |

An existing released action retains its original receipt during later network failures. Replay is idempotent; it does not resend or rewrite the receipt. Rechecking creates a new pending revision and never sends anything. A future business date is a selection context, not a delivery schedule or promise about sources not yet observed.

## Development

```sh
python -m pip install -e . -r requirements-dev.txt
python -m ruff check citeafter tests scripts
python -m unittest discover -s tests -v
python -m build --no-isolation
```

Transport tests launch their own numeric-loopback publishers and processes. A negative self-signed TLS check uses OpenSSL when available; a skip is reported explicitly. No existing desktop application, call, service or unrelated process is changed.
