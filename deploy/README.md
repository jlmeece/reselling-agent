# VPS Deployment — Reselling Agent + Hermes Gateway

## Overview

Hermes Agent runs as the orchestration layer on Hostinger VPS. The reselling agent's modes are exposed as Hermes skills and controlled via Telegram.

## Prerequisites

- Hostinger VPS (Ubuntu)
- Docker + Docker Compose installed on VPS
- Telegram bot token (create via @BotFather)
- All credentials from `.env.template` ready to fill in

## Setup Steps

1. **SSH into VPS**
   ```bash
   ssh user@your-vps-ip
   ```

2. **Clone the repo**
   ```bash
   git clone https://github.com/jlmeece/reselling-agent.git
   cd reselling-agent
   ```

3. **Create .env from template**
   ```bash
   cp .env.template .env
   nano .env   # Fill in all values
   ```

4. **Upload Google credentials**
   ```bash
   # From local machine:
   scp google_credentials.json user@your-vps-ip:~/reselling-agent/
   ```

5. **Start Hermes**
   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   ```

6. **Verify Telegram connection**
   Send `/status` to your Telegram bot — should respond with last run times.

## Updating the Agent

```bash
git pull
docker compose -f deploy/docker-compose.yml restart
```

## Logs

```bash
docker compose -f deploy/docker-compose.yml logs -f
```

---

## Linux Chrome Port (Required for active/daily/research modes)

### Why These Modes Cannot Run on Ubuntu Today

`tools/costco_scraper.py` uses Windows-only system calls. The following lines crash immediately on Ubuntu:

| Line | Code | Why it's Windows-only |
|------|------|-----------------------|
| 32 | `CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"` | Hardcoded Windows path — no equivalent exists at this path on Ubuntu |
| 36 | `AGENT_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""), ...)` | `LOCALAPPDATA` is a Windows-only env var; returns `""` on Linux, breaking the profile path |
| 109 | `subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"])` | `taskkill` is a Windows command — does not exist on Ubuntu |
| 121 | `subprocess.run(["wmic", "process", "where", ...])` | `wmic` (Windows Management Instrumentation) does not exist on Ubuntu |

**Affected modes:** `active`, `daily`, `research`, `discovery`, `recheck` — all call `make_browser()` which calls `_ensure_chrome()`, which fails at line 109/121.

**Safe on VPS today (no Chrome needed):** `rotation`, `refresh-notes`

---

### Why Real Chrome Is Required (Not Playwright Bundled Chromium)

Costco's website is protected by **Akamai Bot Manager**. Akamai detects automation by inspecting:

- **TLS fingerprint** — the browser's TLS handshake signature at the network layer. Playwright's bundled Chromium has a known automation fingerprint.
- **`navigator.webdriver`** — standard Playwright/Selenium sets this flag to `true`, which Akamai reads via JavaScript.
- **`--enable-automation` Chrome flag** — injected by Playwright by default; detectable via `window.chrome.runtime`.
- **Behavioral signals** — absence of real mouse movement, consistent timing, no GPU rendering artifacts.

When any of these are detected, Costco returns a 403 or a CAPTCHA page. The scraper returns `CHECK FAILED` for every product.

The current scraper bypasses Akamai by launching the **user's real installed Chrome** without any automation flags, then connecting to it via Chrome DevTools Protocol (CDP). Costco sees a legitimate Chrome TLS fingerprint with valid authenticated session cookies — indistinguishable from a real browser.

---

### What Needs to Change to Run on Ubuntu

To port the Chrome-dependent modes to Linux, replace the following in `tools/costco_scraper.py`:

**1. Chrome executable path** — make it configurable via env var:
```python
# Current (Windows-only):
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Required (cross-platform):
CHROME_EXE = os.getenv("CHROME_EXE", "/usr/bin/google-chrome")
```

**2. Agent profile path** — replace `LOCALAPPDATA` with an env var or XDG path:
```python
# Current (Windows-only):
AGENT_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CostcoAgentProfile")

# Required (Linux):
_default_profile = os.path.expanduser("~/.config/CostcoAgentProfile")
AGENT_PROFILE = os.getenv("AGENT_PROFILE", _default_profile)
```

**3. Process kill** — replace `taskkill` and `wmic` with POSIX signals:
```python
# Current (Windows-only — lines 109-123):
subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
subprocess.run(["wmic", "process", "where", ..., "call", "terminate"], capture_output=True)

# Required (Linux):
import signal
try:
    os.kill(pid, signal.SIGKILL)
except ProcessLookupError:
    pass
```

**4. Install real Google Chrome on the VPS** (not Chromium — fingerprint differs):
```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo dpkg -i google-chrome-stable_current_amd64.deb
sudo apt-get install -f -y
```
Then add to `.env`: `CHROME_EXE=/usr/bin/google-chrome`

---

### Akamai Risk on a VPS (Even With Real Chrome)

Even after the port, a VPS environment may still be detected by Akamai because:

- **Datacenter IP ranges** — Hostinger, AWS, DigitalOcean, and other VPS providers have their IP blocks flagged as non-residential. Akamai can block by IP reputation alone.
- **No GPU** — Most VPS instances have no GPU, so WebGL fingerprinting returns a software renderer. Real browsers on laptops return hardware renderer signatures.
- **No real input events** — Akamai's behavioral scoring tracks mouse movement, scroll velocity, and click patterns. A headless VPS session has none of these.

**The highest-reliability path is to keep Chrome-dependent modes running locally on Windows** and use the VPS only for API-safe modes (`rotation`, `refresh-notes`). Cookie sync via `tools/cookie_sync.py` keeps the VPS cookies fresh without needing a browser on the VPS at all.
