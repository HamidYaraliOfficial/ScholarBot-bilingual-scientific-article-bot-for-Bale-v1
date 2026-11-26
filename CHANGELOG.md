# Changelog

## 1.0.1 — 2026-11-17
- **Python 3.8+ support** (tested on 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14): the launchers no longer demand 3.11.
  Fixed `X | None` annotations that SQLAlchemy/FastAPI evaluate at runtime and replaced `asyncio.to_thread` (3.9+) with a compatible helper.
- `start_windows.bat`: more robust interpreter detection (`py -3`, `py`, `python`, `python3`), prints every candidate and its version,
  ignores the Microsoft-Store shortcut and explains what to do. `start_unix.sh` also tries `python3.8` … `python3.14`.
- **Bot:** in-bot admin panel for admin Bale IDs (`ADMIN_BALE_IDS` or dashboard setting; several IDs allowed): statistics, source on/off, user block/unblock/quota,
  recent errors and requests, backup, cleanup, plus `/stats /block /unblock /quota`; `/myid` for everyone; every admin tap/command is re-authorised on the server and audited.
  Menu buttons now match by their words, so emoji/variation-selector/ZWNJ differences from a client no longer break them.
- 141 automated tests, including every bot button and section driven through the real poller as a regular user and as an admin (fa + en).

## 1.0.0 — 2026-11-15
First complete release of **ScholarBot** by Hamid Yarali.

- Bilingual (Persian / English) Bale bot: DOI, title, author, article link, arXiv / PubMed / PMC identifiers.
- Legal-sources-only retrieval: Crossref, OpenAlex, Unpaywall, Europe PMC (PubMed Central), DOAJ, arXiv, CORE.
- PDF validation (signature, structure, SHA-256), de-duplication by DOI / title hash / file hash.
- Admin dashboard (FastAPI + Jinja2 + HTMX): statistics and charts, article management with checkbox bulk actions,
  permanent library, history, users, sources, administrators with roles, reports, settings, logs, backup & restore.
- Security: Argon2 passwords, CSRF, CSP, SSRF-safe downloader with IP pinning, rate limits, account lockout, audit log,
  write-only secrets, formula-injection-safe exports.
- Start scripts for Windows (`start_windows.bat`) and Linux/macOS (`start_unix.sh`), Alembic migrations, automated tests.
