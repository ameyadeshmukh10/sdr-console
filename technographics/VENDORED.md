# Vendored: technographics

Standalone technographic-detection package vendored from the
`ameyadeshmukh10/technographic-signals` repo (DNS + web fingerprinting against a
Wappalyzer-derived signature catalogue; MIT / catalogue data GPLv3 — see
`signatures/master/NOTICE.md`).

- **Source repo:** https://github.com/ameyadeshmukh10/technographic-signals
- **Vendored commit:** `6acbfde4af97fff55ce034542711319e5555138d` (2026-07)
- **What was copied (unmodified):** `technographics/src/technographics/*.py` + `py.typed`,
  `technographics/signatures/**` (curated + full master catalogue + selection files),
  and two test fixtures under `tests/fixtures/` (used by
  `.claude/skills/sdr-pipeline/scripts/tech_signals.py --self-test`).
- **What was NOT copied:** `pyproject.toml`, `technographics/scripts/` (catalogue build
  tools), docs, unit tests. The upstream repo's HubSpot-coupled `src/` layer was not
  vendored either — its bucket-mapping/formatting logic is ported into
  `tech_signals.py` instead.

## Re-sync

```bash
cp <upstream>/technographics/src/technographics/*.py technographics/src/technographics/
cp <upstream>/technographics/src/technographics/py.typed technographics/src/technographics/
rm -rf technographics/signatures && cp -r <upstream>/technographics/signatures technographics/
cp <upstream>/technographics/tests/fixtures/sample_{dns_records,page_data}.json technographics/tests/fixtures/
```
Then update the commit SHA above and re-run
`python3 .claude/skills/sdr-pipeline/scripts/tech_signals.py --self-test`.

## Dependencies

- **Runtime (prod):** `dnspython` only (declared in the repo-root `requirements.txt`;
  imported by `dns_collector.py`). All imports of this package MUST stay lazy so the
  web server boots without it.
- **Optional, never installed in the Railway image:** `click` (only `cli.py`, which
  nothing imports) and `playwright` (only `web_collector.py`, lazy import — used by
  `tech_signals.py --rendered` in Claude sessions where Chromium is preinstalled).
