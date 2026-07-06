# Security Policy

## Supported versions

Hound is a single actively-maintained release line. Security fixes land on the
latest `master` and ship in the next PyPI release. There are no separate
long-term-support branches. Keep your install current:

```bash
hound -u
```

## Reporting a vulnerability

If you find a security issue in Hound, please report it privately rather than
opening a public issue.

- **Email**: bhandaribishesh999@gmail.com
- Or use GitHub's private vulnerability reporting: the *Report a vulnerability*
  button on the **Security** tab of this repository.

Include what you found, how to reproduce it, and the impact. You will get an
acknowledgement within a few days and a coordinated disclosure timeline once a
fix is ready. Please do not disclose the issue publicly until a fixed release is
published.

## Scope

Hound fetches arbitrary URLs and runs a local anti-detect browser, so the
relevant threat surface is:

- **SSRF / local-network reach**: Hound validates and blocks fetches to private
  IP ranges and loopback by default (`respect_robots` and the SSRF guard in
  `src/master_fetch/security.py`). Bypasses that let an agent reach internal
  services are in scope.
- **Arbitrary code execution from fetched content**: extraction is pure parsing
  (no `eval` of remote content). A path that executes fetched code is in scope.
- **Self-update mechanism**: the `hound -u` command runs `pip install`. Issues
  that let a network attacker alter what gets installed (e.g. a TLS or
  PyPI-redirect attack) are in scope.

Out of scope: Hound is a keyless web scraper. Sites that block scrapers, rate
limits, and CAPTCHAs are operational limits, not security vulnerabilities.
