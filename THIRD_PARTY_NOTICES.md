# Third-party notices

djlights is MIT-licensed (see [LICENSE](LICENSE)). It includes or depends on
the third-party work listed below. That work keeps its own license.

## Bundled code

### python-prodj-link: `prodj/`

- Source: https://github.com/flesniak/python-prodj-link
- License: Apache License 2.0 — https://www.apache.org/licenses/LICENSE-2.0
- Vendored copy (it may contain small local changes). This code is used
  only by the legacy direct Pro DJ Link path (`main.py`, `app.py`, `engine.py`).

## External tools (not bundled)

| Project | License | Use |
|---|---|---|
| [Beat Link Trigger](https://github.com/Deep-Symmetry/beat-link-trigger) | EPL-2.0 | Pro DJ Link bridge. The expressions in `blt/` run inside it. |
| [WLED](https://github.com/Aircoookie/WLED) | EUPL-1.2 | LED controller firmware (receives DDP) |

## Python dependencies

`install.sh` installs the dependencies listed in
[`requirements.txt`](requirements.txt). Each one is under its own license.
They include customtkinter (MIT), numpy and scipy (BSD), anthropic (MIT),
python-osc (Unlicense), mido (MIT), bleak (MIT), pyobjc (MIT) and Pillow
(MIT-CMU).

## Trademarks

Pioneer DJ, CDJ, DJM and rekordbox are trademarks of AlphaTheta Corporation.
Claude is a trademark of Anthropic. djlights is not affiliated with or endorsed
by these companies.
