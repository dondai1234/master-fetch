# scripts/verify/

Dev-time smoke scripts. **Not** auto-run by CI. Move any test that should
run on every commit into `tests/` so pytest discovers it there.

## Why this directory exists

`tests/` is picked up by `pytest tests/` in CI. Filenames with dots
(`test_v3.4.0_verify.py`) made pytest try to import them as
`tests.test_v3.4.0_verify`. Python treats dots in module names as
package separators, so `tests.test_v3` was tried first and failed.
This killed collection on every CI cell. 6 of 6 cells failed on every
push since 2026-06-08.

Renaming dots to underscores would have fixed the import, but the
scripts used Chrome subprocesses that require a runtime install of
Chromium, which CI does not set up. The renamed files would have
started failing 16 fresh tests. They were dev-time smoke artefacts and
did not belong in automated test discovery.

## Removed in v3.6.1

The two historical smoke scripts (`verify_v3_4_0_pre_browser_consolidation.py`
and `verify_v3_4_1_no_prewarm.py`) were removed. They targeted v3.4.x
internals that were reworked in v3.5.x (the README already warned they
"may not pass against current master_fetch internals"), and they imported
`master_fetch.domain_intel`, which was deleted in v3.6.1 as dead code.
If a smoke-check is genuinely useful as a CI gate, port it into a real
pytest test under `tests/` and let it run there.
