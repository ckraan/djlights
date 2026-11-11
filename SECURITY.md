# Security

## Your API key

- djlights stores your Anthropic API key **only locally**, in `ai_config.json`
  in the project folder.
- That file is listed in `.gitignore`. **Never commit it**, attach it to an
  issue, or share it in a zip.
- The test scripts read the key from `--key` or the `ANTHROPIC_API_KEY`
  environment variable. Don't hard-code it.
- If a key has been exposed, revoke it right away in the
  [Anthropic Console](https://console.anthropic.com/) and create a new one.

Before you push, check that nothing secret is staged:

```bash
git status --ignored        # ai_config.json should be listed under "Ignored files"
git grep -n "sk-ant-"       # should only find documentation examples
```

## Network exposure

djlights opens these local ports:

| Port | Bound to | Purpose |
|---|---|---|
| 9000/udp | `0.0.0.0` | OSC from Beat Link Trigger |
| 9001/udp | `0.0.0.0` | Track metadata from Beat Link Trigger |
| 5050/tcp | LAN | Phone feedback page (no authentication) |

Beat Link Trigger (with the djlights show loaded) also opens:

| Port | Purpose |
|---|---|
| 17081/tcp | BLT web server |
| 17082/tcp | Phrase overlay page |
| 17001/tcp | nREPL, bound to `127.0.0.1` only (it can run code) |

Use djlights on a trusted DJ/booth network, not on public Wi-Fi.

## Reporting a vulnerability

Please report security issues privately to the repository owner. Use
GitHub's *Report a vulnerability* button on the Security tab, rather than a
public issue.
