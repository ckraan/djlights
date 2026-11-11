# Contributing to djlights

Thanks for helping out! This is a hobby-scale project, so the process is light.

## Getting set up

```bash
git clone https://github.com/ckraan/djlights.git
cd djlights
./install.sh
./run.sh
```

Without CDJs, use **▶ DEMO** in the title bar to exercise the render path.

## Workflow

1. Open an issue first for anything larger than a small fix, so we can agree on
   the approach.
2. Create a branch: `feature/<short-name>` or `fix/<short-name>`.
3. Keep each pull request focused on one change.
4. Fill in the pull request template, including how you tested.

## Guidelines

- **Never commit secrets or personal data.** `ai_config.json`, logs, session
  data and your own `setups.json` are git-ignored. Keep it that way.
- **Keep the render thread fast.** No network calls, file I/O or new imports
  in the frame loop. Put API calls on a worker thread.
- **The BLT protocol is a contract.** If you change a message in
  `blt/vp_blt_code.clj`, update `blt_client.py` *and*
  [docs/beat-link-trigger.md](docs/beat-link-trigger.md) in the same pull
  request.
- **New settings** go into `settings.py` (`DEFAULTS`) and `settings_tab.py`, and
  get documented in [docs/configuration.md](docs/configuration.md).
- **New effects** subclass `effects/base.py` and are registered in
  `effects/__init__.py`.
- Code style: PEP 8, type hints where they help, and a short module docstring
  explaining *why* the module exists.

## Testing

There is no automated test suite yet. Before you open a pull request:

```bash
venv/bin/python -m compileall -q .        # syntax check
./run.sh                                  # app starts, all four tabs open
```

Then check your change with **▶ DEMO**, or with a CDJ and Beat Link Trigger if
you have them. AI changes can be exercised with `test_ai.py` (it needs
`ANTHROPIC_API_KEY`).

## Commit messages

Use the imperative mood with a short subject line: `Add globe sparkle effect`,
`Fix phrase retry when BLT restarts`.
