# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — 2026-11-11

First shared release.

### Added
- **SETTINGS tab** (`settings.py`, `settings_tab.py`) for network ports, BLT
  URL, render FPS, AI models, and phrase brightness and palette defaults,
  stored in `settings.json`.
- Configurable OSC/UDP ports and CDJ network interface in
  `blt/vp_blt_code.clj`. The SETTINGS tab keeps the ports in sync.
- `blt/djlights.blt.template`: a Beat Link Trigger show that `install.sh` turns
  into a working show for any install location.
- `install.sh` picks a Python 3.10+ that has tkinter. `run.sh` starts the app.
- Documentation: README, `docs/` (installation, configuration, Beat Link
  Trigger protocol, architecture, troubleshooting), CONTRIBUTING, SECURITY,
  third-party notices, and GitHub issue and pull request templates.

### Changed
- Removed hard-coded user paths. Logs and caches now live next to the code.

### Security
- The API key is only stored in a git-ignored `ai_config.json`. The repo
  contains no keys, logs or personal session data.
