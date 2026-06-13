# scripts/verify/

Dev-time smoke scripts. **Not** auto-run by CI. Move any test that should
run on every commit into `tests/` so pytest discovers it there.

## Contents

- `verify_v3_4_0_pre_browser_consolidation.py` (was `tests/test_v3.4.0_verify.py`).
  Smoke-checks session coalescing behavior introduced in v3.4.0. May not
  pass against current master_fetch internals because consolidation was
  reworked in v3.5.x.
- `verify_v3_4_1_no_prewarm.py` (was `tests/test_v3.4.1_verify.py`).
  Smoke-checks that removing browser pre-warming did not leave a second
  Chrome process running. Spawns a real `hound` subprocess and counts
  Chrome processes via `tasklist`. Requires a working `hound.exe`
  on PATH.

## Why these moved out of `tests/`

`tests/` is picked up by `pytest tests/` in CI. Filenames with dots
(`test_v3.4.0_verify.py`) made pytest try to import them as
`tests.test_v3.4.0_verify`. Python treats dots in module names as
package separators, so `tests.test_v3` was tried first and failed.
This killed collection on every CI cell. 6 of 6 cells failed on every
push since 2026-06-08.

Renaming dots to underscores would have fixed the import, but the
scripts use Chrome subprocesses that require a runtime install of
Chromium, which CI does not set up. The renamed files would have
started failing 16 fresh tests. They are dev-time smoke artefacts and
do not belong in automated test discovery.

## Running manually

```bash
# Requires playwright chromium installed + a running hound.exe
python scripts/verify/verify_v3_4_1_no_prewarm.py

# Does not require a running hound. Tests session helpers directly.
python scripts/verify/verify_v3_4_0_pre_browser_consolidation.py
```

If a smoke-check is genuinely useful as a CI gate, port it into a real
pytest test under `tests/` and let it run there.
