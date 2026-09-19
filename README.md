# Meta Page Token Generator

Cross-platform desktop app (Python + Tkinter) for Facebook Page administrators — works on **macOS** and **Windows** (and Linux with Tk installed).

It converts a short-lived **User Access Token** into a long-lived User token (*intermediate*),
lists Facebook Pages via `/me/accounts`, and gives you the **Page Access Token** to paste into
your WordPress Facebook Page plugin.

**Do not** put the Long-Lived User Token into the plugin — Meta will return `(#210) A page access token is required`.

This tool talks **only** to the official Meta Graph API (`graph.facebook.com`). It does not run a local web server, does not send analytics/telemetry, and does not save secrets to disk unless you copy them yourself.

## Requirements

- Python **3.11+** (3.12 / 3.13 recommended)
- `requests`
- Tkinter (usually bundled with Python)

### Check Tkinter

```bash
python3 -m tkinter
```

On Windows (Command Prompt / PowerShell):

```bat
python -m tkinter
```

A small test window should open.

**macOS:** If you see `ModuleNotFoundError: No module named '_tkinter'`:

- Homebrew Python often ships **without** Tk. Prefer the installer from [python.org](https://www.python.org/downloads/), **or**
- Install a Tk-enabled package: `brew install python-tk@3.13` (match your Python version).

**Windows:** Install Python from [python.org](https://www.python.org/downloads/) and leave **tcl/tk and IDLE** enabled (default). Avoid stripped embeds that omit Tk.

## Installation

### macOS / Linux

```bash
cd meta-token-generator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Windows

```bat
cd meta-token-generator
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

In PowerShell, if script activation is blocked:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then run `.venv\Scripts\Activate.ps1` again.
## Diagnostics / DEBUG MODE

If token exchange fails while the same token works in Graph API Explorer:

1. Fill App ID, App Secret, and User Access Token.
2. Click **Run Diagnostics**.
3. Read the **Diagnostics** panel (checklist + report).
4. Click **Open Log File** → `logs/meta_token_generator.log`.

Logs include HTTP status, sanitized params, and Meta JSON errors. **Full secrets are never written** (masked as `EAAB...9XYZ`).

## How to use

1. Create a **Meta App** in [Meta for Developers](https://developers.facebook.com/).
2. Configure Facebook Login / permissions for Pages as needed (see below).
3. Generate a **User Access Token** (Graph API Explorer or your app’s token tools) with the required permissions and Page access.
4. Run this application (`python main.py`).
5. Enter **Meta App ID**.
6. Enter **Meta App Secret**.
7. Enter the short-lived **User Access Token**.
8. Click **Generate Long-Lived Token + Load Pages**.
9. Select a Page under **Available Facebook Pages**.
10. In **FINAL TOKEN FOR PLUGIN**, click **Copy Token** (this is the Page Access Token).
11. Optionally **Test Token** / **Test Last 3 Posts** (`published_posts`).
12. Paste into the plugin as: `PAGE_ID|PAGE_ACCESS_TOKEN`.

### Permissions

Grant permissions legally through Meta’s normal flows (App Review / Business Verification when required). This app does not bypass Meta’s permission system.

Typically you need at least:

- `pages_show_list` — list Pages (`/me/accounts`)
- `pages_read_engagement` — read Page posts / engagement-related fields

Optionally, depending on what you do with the token elsewhere:

- `pages_read_user_content`
- `pages_manage_posts`
- `pages_manage_engagement`
- `business_management`

### Token naming

- **Long-Lived User Access Token** — intermediate only (used to call `/me/accounts`; often ~60 days).
- **Page Access Token** — from `/me/accounts` → `data[].access_token` — **this goes into the plugin**.

Meta may invalidate access tokens when permissions, passwords, security settings, app access, Page access, or other account conditions change. Do not treat Page tokens as unconditionally permanent.

## Security

- App Secret and access tokens are **never** printed to the terminal by this app.
- Secrets are **not** written to files automatically.
- Secrets are **not** embedded in source code.
- HTTP calls use `requests` with `params=` (proper encoding); secrets are not hand-stitched into URLs.
- Use **Clear Sensitive Data** to wipe secrets and results from the GUI memory when finished.
- Do not paste secrets into README, tickets, or chat logs.

## Project layout

```text
meta-token-generator/
├── main.py            # Tkinter GUI
├── meta_api.py        # Graph API client
├── requirements.txt
├── README.md
└── .gitignore
```

Graph API version is defined once in `meta_api.py` (`GRAPH_API_VERSION`).

## Optional: packaged app (PyInstaller)

Normal use does **not** require PyInstaller.

### macOS `.app`

```bash
source .venv/bin/activate
pip install pyinstaller
pyinstaller --windowed --name "Meta Page Token Generator" main.py
```

Output: `dist/Meta Page Token Generator.app`

### Windows `.exe`

```bat
.venv\Scripts\activate
pip install pyinstaller
pyinstaller --windowed --name "Meta Page Token Generator" main.py
```

Output: `dist\Meta Page Token Generator\Meta Page Token Generator.exe` (or a one-folder build under `dist\`).

## Troubleshooting

| Symptom | What to try |
|--------|-------------|
| Invalid App ID / Secret | Confirm values from the Meta App dashboard. |
| Code 190 | User token expired — generate a new short-lived User Access Token. |
| Empty Pages list | Missing `pages_show_list`, or the user has no Page roles. |
| Cannot load posts | Missing `pages_read_engagement` (or related), or no posts. |
| Timeout / no internet | Check connectivity to `graph.facebook.com`. |
| Tkinter missing | See “Check Tkinter” above (Homebrew on macOS, or reinstall Python with Tcl/Tk on Windows). |

## License

Use at your own risk. You are responsible for complying with [Meta Platform Terms](https://developers.facebook.com/terms/) and your local laws when accessing Facebook data.
