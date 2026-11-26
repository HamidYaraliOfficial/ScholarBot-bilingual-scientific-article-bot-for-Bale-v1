# ScholarBot — bilingual scientific-article bot for Bale

**Created and developed by Hamid Yarali** · Platform: Bale Messenger · Languages: فارسی / English · License: MIT

ScholarBot lets Bale users find scientific articles by **DOI, title, author, article link, arXiv / PubMed / PMC identifier**.
It checks **legal open-access sources**, downloads a PDF only when its access is legitimately open, validates it and sends it
in the chat. A web dashboard lets administrators see every request, select articles with checkboxes, keep them in a
permanent library, delete them, export them and manage users, sources, settings and backups.

> **Legal by design.** ScholarBot never bypasses paywalls, logins, CAPTCHAs or access controls and never uses Sci-Hub or
> similar sites (such hosts are refused outright). If no legal copy exists, the user receives the bibliographic details and the
> official DOI / publisher link instead.

نسخه فارسی: [README_FA.md](README_FA.md) · Step-by-step install: [INSTALL.md](INSTALL.md)

## Contents
1. [Features](#features) 2. [Architecture](#architecture) 3. [Prerequisites](#prerequisites) 4. [Install & run](#install--run)
5. [Bale bot setup](#bale-bot-setup) 6. [Scientific sources](#scientific-sources) 7. [Using the dashboard](#using-the-dashboard)
8. [Saving & deleting articles](#saving--deleting-articles) 9. [Backup & restore](#backup--restore) 10. [Security](#security)
11. [Troubleshooting](#troubleshooting) 12. [Tests & verification status](#tests--verification-status) 13. [Legal notes](#legal-notes-and-license)

## Features
**Bot (Persian + English, language stored per user)**
- Main menu: 🔎 search · 📄 DOI · 🔗 link · 👨‍🔬 author · 📚 recent · ⭐ my saved · 📖 help · ⚙️ settings · 🌐 language.
- Any free text also works: the bot detects DOI / arXiv / PMID / PMC / URL / title automatically.
- Menu buttons are matched by their words (emoji, variation selectors and ZWNJ are ignored; Persian and English labels both work), so they keep working even if a Bale client sends the emoji slightly differently.
- **In-bot admin panel** for configured admins (statistics, sources on/off, users block/unblock, errors, requests, backup, cleanup) and `/myid` for everyone — see [Bot buttons, commands and admin panel](#bot-buttons-commands-and-admin-panel).
- Title or author searches return up to 5 candidates as buttons; an exact title match is resolved directly.
- Status message ("searching…"), article card (title, authors, year, journal, DOI, access status, source, size) and the PDF.
- Localized errors for: not found, invalid DOI, no legal PDF, publisher page only, source down, corrupt file, file too large,
  Bale delivery failure, rate limit, daily quota, blocked user.
- Per-user rate limit and daily quota, duplicate in-flight request protection, async queue with bounded concurrency.

**Dashboard (RTL/LTR, light/dark, responsive, collapsible sidebar, Persian-calendar dates)**
- *Dashboard*: 12 live statistics + 4 charts (requests/day, delivered/day, active users/week, top sources, success rate).
- *Articles*: filter by title / DOI / author / year / source / status / library state, sortable columns, pagination,
  per-row checkboxes, "select page" and "select all filtered results", detail view with operation history,
  bulk **save / remove from library / delete / ZIP download / export Excel · CSV · BibTeX · RIS**.
- *Saved library*, *Download history* (auto-refresh), *Users* (block, quota, history), *Scientific sources* (enable, priority,
  connection test, API keys), *Administrators* (roles, audit log), *Reports*, *Settings*, *System logs*, backup/restore.

## Architecture
```
Bale ──getUpdates──▶ BotRunner ─▶ handlers ─▶ DownloadJob (DB) ─▶ TaskManager (N workers)
                                                                        │
                       ┌────────────────────────────────────────────────▼──────────────┐
                       │ ArticlePipeline: parse input → ArticleSearch → ProviderManager │
                       │  (Crossref · OpenAlex · Unpaywall · Europe PMC · DOAJ · arXiv · CORE, parallel, retries, stats)
                       │  → rank legal OA links → PDFDownloader (SSRF-safe, size cap, %PDF check, structure check)
                       │  → Library/StorageManager (temp cache ↔ permanent library, SHA-256 dedupe) → BotNotifier → PDF in Bale
                       └──────────────────────────────────────────────────────────────┘
FastAPI dashboard (Jinja2 + HTMX, vendored assets, no CDN) ── same DB / services ── Alembic migrations (SQLite | PostgreSQL)
```
```
app/bot         Bale client, handlers, keyboards, middleware, poller + notifier
app/core        config, security, SSRF-safe HTTP, localization, logging, runtime settings, secrets store
app/database    models, async engine, repositories
app/providers   one module per scientific source + registry/manager
app/services    parsing, search, pipeline, downloader, validator, storage, library, backup, exports, task manager, maintenance
app/dashboard   routes, Jinja2 templates, static (css/js/vendor)      app/api  health endpoint
migrations/     Alembic          localization/  fa.json, en.json       tests/  pytest (+ optional jsdom UI test)
```
Adding a language = drop `localization/<code>.json`. Adding a source = subclass `Provider` and register it in `registry.py`.

## Prerequisites
- Python **3.8 or newer** (tested on 3.8, 3.9, 3.10, 3.11, 3.12, 3.13 and 3.14). Internet access on first run (dependencies) and for the sources.
- A Bale bot token (see below). No administrator/root rights are needed.
- Optional: PostgreSQL (`pip install -r requirements-postgres.txt`, then `DATABASE_URL=postgresql://…`).

## Install & run
Everything (virtualenv, dependencies, `.env`, migrations, first admin) is automated by one script per OS.
The scripts reuse an existing `.venv` and only reinstall dependencies when `requirements*.txt` changes.

**Windows** — double-click `start_windows.bat` (or run it in `cmd`/PowerShell). Install Python 3.8+ first and tick *Add python.exe to PATH*. The script tries `py -3`, `py`, `python`, `python3` and tells you which versions it found.

**Linux / macOS**
```bash
chmod +x start_unix.sh
./start_unix.sh
```
On first run you are asked for the first administrator's username and password (min. 10 characters, not only digits).
The dashboard address is printed (default **http://127.0.0.1:8080**). Press **Ctrl+C** to stop.
Without a bot token the dashboard still starts and shows the bot as *disabled*.

Manual commands (inside the activated `.venv`): `python -m app.cli migrate`, `python -m app.cli create-admin`,
`python -m app.cli check`, `python run.py`.

## Bale bot setup
1. In Bale open **@botfather**, create a bot and copy its token.
2. Put it in `.env` → `BALE_BOT_TOKEN=...` **or** in *Dashboard → Settings → Bale bot* (super admin; write-only, stored in `.env`, never displayed).
3. Restart (the dashboard button *Save and restart bot* does it for you). The dashboard then shows `@your_bot` as *running*.
4. **Become a bot admin (optional):** send `/myid` to the bot, then put the number in `ADMIN_BALE_IDS` in `.env` (several IDs: comma-separated) **or** in *Dashboard → Settings → Bot admin Bale IDs* (applies immediately). Admins get an extra **🛠 Admin panel** button.

The client talks to the official Bot API (`https://tapi.bale.ai/bot<token>/…`) and only uses documented methods
(`getMe`, `getUpdates`, `sendMessage`, `sendDocument`, `editMessageText`, `deleteMessage`); `deleteWebhook` and `answerCallbackQuery`
are best-effort. Bale-API compatibility with Telegram is **not** assumed. Upload limit: 50 MB (configurable lower).

## Bot buttons, commands and admin panel
**Everyone**

| Button / command | What it does |
|---|---|
| 🔎 Search articles (`/search`) | asks for a title, DOI or link, then sends the article (or up to 5 choices as buttons) |
| 📄 Get article by DOI (`/doi`) · 🔗 by link (`/link`) | validates the input and fetches the legal PDF |
| 👨‍🔬 Search by author (`/author`) | lists matching articles as buttons; tap one to receive it |
| 📚 Recent articles | your last 10 articles; tap to receive again (no new download) |
| ⭐ My saved articles | your personal list; each article message has a ⭐ save / ✖ remove button and an official-page link button |
| 📖 Help (`/help`) · ⚙️ Settings (`/settings`) · 🌐 Change language (`/language`) | help, today's usage vs. quota, Persian/English switch |
| `/menu` `/cancel` `/start` `/myid` | main menu, cancel the current input, restart, show your numeric Bale ID |

**Admins only** (IDs from `ADMIN_BALE_IDS` / Settings; checked on the server for every tap and command — callback data is never trusted, regular users just get "not allowed")

| Button / command | What it does |
|---|---|
| 🛠 Admin panel (`/admin`) | opens the panel |
| 📊 Statistics (`/stats`) | users, searches, success rate, errors, queue, bot status, articles, storage |
| 🧩 Sources | status of all 7 sources with request/error/PDF counters; tap a source to switch it on/off immediately |
| 👥 Users | recent users → details → ⛔ block / ✅ unblock |
| ⚠️ Recent errors · 📚 Recent requests | last 10 error events / requests across all users |
| 💾 Backup · 🧹 Clean temp files | create a backup (download it from the dashboard) / delete expired temporary PDFs |
| `/block <id>` `/unblock <id>` `/quota <id> <n\|default>` | manage a user by numeric Bale ID. Admins cannot be blocked; every action is written to the audit log as `bale:<id>` |

## Scientific sources
| Source | Role | Needs |
|---|---|---|
| Crossref | authoritative metadata | optional `CONTACT_EMAIL` (polite pool) |
| OpenAlex | metadata + OA locations | free `OPENALEX_API_KEY` (required by OpenAlex since Feb 2026) |
| Unpaywall | legal OA locations by DOI | **`CONTACT_EMAIL` required** |
| Europe PMC | PubMed Central OA full text | — |
| DOAJ | fully-OA journals | — |
| arXiv | preprints | — |
| CORE | institutional repositories | free `CORE_API_KEY` (disabled without it) |

Set keys in `.env` (see `.env.example`) or write-only on *Dashboard → Scientific sources*. Priority and enable/disable are in the same page;
`config/providers.example.json` can seed them before the first start (copy to `config/providers.json`). A source being down never fails
a request: the others are used, errors are recorded in *Reports*. Only links that a source declares as open access are downloaded;
`Settings → Require an explicit licence` additionally demands licence metadata.

## Using the dashboard
Sign in with the admin account. Roles: **viewer** (read-only, may export), **admin** (articles, users, sources, backups, logs),
**super admin** (everything incl. settings, secrets, administrators, restore). Language and theme switches are in the top bar.
Initial administrator: created by the start script, or `python -m app.cli create-admin`.

## Saving & deleting articles
- Every request is stored as a **temporary** article (PDF in `storage/temp/files`, removed after the retention time).
- Select rows → **Save selected** moves the PDF to the **permanent library** (`storage/pdfs`). *Remove from library* returns it to temporary.
- **Delete** always asks for confirmation and offers *record only* or *record + file*. A PDF shared with another live article is never removed.
- De-duplication: DOI → title/author/year hash → file SHA-256. Deleted records are hidden (status *deleted*) and revived if requested again.

## Backup & restore
*Settings → Backup*: **Create backup** produces `storage/backups/scholarbot_backup_<time>.zip` (database as portable JSON + permanent PDFs; admin accounts
are excluded). It runs without pausing the bot; optional scheduling via *Automatic backup interval*. Download from the page; **Restore** (super admin, with confirmation)
replaces data from a listed or uploaded backup. Works for SQLite and PostgreSQL.

## Security
Argon2 password hashing · session cookies signed with `SECRET_KEY` (HttpOnly, SameSite=Lax, `COOKIE_SECURE=true` for HTTPS) · CSRF tokens on every POST ·
CSP `script-src 'self'` and no external assets · login rate limit + account lockout · role checks on every route · audit log · parameterized SQL (SQLAlchemy) ·
Jinja2 auto-escaping · **SSRF protection** (http/https only, standard ports, every redirect hop validated, all resolved IPs must be public, connection pinned to the validated IP with SNI) ·
path-traversal-safe storage · PDF magic/structure validation and size caps · tokens/keys never logged (redaction) and write-only in the UI · CSV/Excel formula-injection neutralised.
For public deployments run behind HTTPS (reverse proxy) and set `COOKIE_SECURE=true`, `TRUSTED_HOSTS`.
The only secret generated automatically is the local session-signing `SECRET_KEY`; bot tokens, API keys and passwords are never invented.

## Troubleshooting
| Symptom | Fix |
|---|---|
| "Python 3.8 or newer was not found" | Install Python from python.org (Windows: tick *Add to PATH*; Debian/Ubuntu: also install `python3-venv`). The launcher lists every Python it tried and its version; on Windows `py -0p` shows what is installed. The Microsoft-Store `python` shortcut does not count. |
| Dependency install fails | Check internet/proxy and run the script again. |
| Bot shows *error*: unauthorized | Wrong/revoked token → re-enter it in Settings. |
| Bot silent | Check *Dashboard* bot card, *Reports → Bot*, and that the token belongs to this bot. |
| "No legal PDF" for a paywalled paper | Expected. Official link is sent. Make sure `CONTACT_EMAIL` (Unpaywall) and `OPENALEX_API_KEY` are set. |
| Source shows *error* | *Scientific sources → Test connection*; see the message and *Reports*. |
| Port already in use | Change `DASHBOARD_PORT` in `.env`. |
| "SECRET_KEY is empty" | Run the start script once (it creates it) or set it in `.env`. |
| Forgot admin password | `python -m app.cli create-admin --username <new>` (or have a super admin reset it). |

## Tests & verification status
Run: `pip install -r requirements-dev.txt && pytest` (takes a few minutes) (optional UI test: `cd tests/js && npm install`, then `pytest tests/test_js_ui.py`).

**Result on the build machine (Linux): 141 passed on every tested Python version — 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14** (with the optional jsdom test; 140 + 1 skipped without it). The bot tests were also stress-run under heavy CPU contention.
Python 3.8/3.9 are end-of-life: pip automatically installs the newest dependency versions that still support them (e.g. FastAPI 0.124, SQLAlchemy 2.0, Pydantic 2.10 on 3.8). Prefer Python 3.11+ where you can.

| Area | Tests |
|---|---|
| Input parsing, passwords, CSRF helpers, redaction, safe paths | 11 |
| SSRF guard incl. real-socket and real-TLS (hostname verification with pinned IP) | 36 |
| PDF validation, storage lifecycle, library save/unsave/delete, shared-file protection, cleanup, ZIP | 7 |
| Providers (parsing fixtures for all 7), retries/error mapping, merge, priority, failure isolation | 11 |
| Pipeline: DOI→PDF→delivery, cache reuse, concurrent identical requests, no legal copy, HTML-instead-of-PDF, corrupt/oversized, outages, title pick, URLs | 14 |
| Bot basics: Bale client, language persistence, menu, quota/rate/block, bookmarks, full poller→PDF flow in Persian and English | 13 |
| **Every bot button and section through the real poller, as a regular user and as an admin, in fa and en**: search/DOI/link/author flows, recent/saved lists, ⭐ buttons, language switch, button-text variants, admin panel (stats, sources toggle with real effect, users block/unblock, errors, requests, backup, cleanup), admin commands, permission checks (regular users cannot reach anything admin), multiple admin IDs, revocation | 21 |
| Dashboard: auth, lockout, CSRF, roles, filters, bulk actions, exports, ZIP, users, sources, settings/secrets, admins, i18n/RTL, static assets | 15 |
| Backup/restore round-trip, malicious archives, translations completeness, migrations = models, CLI, start-script syntax and Python-version detection (3.7 rejected, 3.8+ accepted) | 12 |
| Dashboard JavaScript in jsdom (selection, bulk, dialogs, downloads, charts) | 1 (20 checks) |

**What was and was not verified (please read):**
- ✔ Linux: `start_unix.sh` was executed end-to-end on real Python 3.12 **and Python 3.8** (fresh venv, install, migrations, server start, Ctrl+C shutdown).
- ✔ External services are exercised through mocked HTTP using the documented response shapes. The build sandbox could **not** reach Bale, Crossref, OpenAlex, Unpaywall etc.
  Before going live run *Dashboard → Scientific sources → Test connection* for each source and send one real DOI to the bot.
- ✖ `start_windows.bat` and macOS were **not** executed (no such machines); the batch file uses only basic `cmd` constructs and its labels are checked by a test, and the macOS script uses only POSIX/Bash features. Please report issues together with the console output.
- ✖ PostgreSQL was not run (code is dialect-neutral, `requirements-postgres.txt` provided). Dashboard screenshots are not included (no browser available).
- Bale's `getUpdates` is documented with `offset`/`limit`; ScholarBot also sends `timeout=20` and throttles empty polls to ≥1 s, so it works whether or not Bale honours it.

## Legal notes and license
Respect publishers' terms and copyright law in your country. ScholarBot relays only copies that open-access services declare as freely available and keeps
the licence/source with every record; the operator is responsible for how the bot is used. Licensed under the [MIT License](LICENSE) © Hamid Yarali.
Bundled: htmx, Chart.js (MIT / BSD), Vazirmatn (SIL OFL 1.1).
