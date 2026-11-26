# Installation & start-up guide · راهنمای نصب و راه‌اندازی

## English

1. **Install Python 3.8 or newer (any newer version works; 3.11+ recommended)** — https://www.python.org/downloads/ (Windows: tick *Add python.exe to PATH*; Debian/Ubuntu also needs the `python3-venv` package; macOS: `brew install python`).
2. **Unzip** `ScholarBot_Hamid_Yarali.zip` somewhere you have write access (the app stores its database, PDFs, logs and backups inside the folder).
3. **Start**
   - Windows: double-click `start_windows.bat`.
   - Linux / macOS: open a terminal in the folder and run
     ```bash
     chmod +x start_unix.sh
     ./start_unix.sh
     ```
4. On the first run the script creates `.venv`, installs `requirements.txt`, creates `.env` from `.env.example`, applies the migrations and asks for the **first administrator** (username + password ≥ 10 characters).
5. Open the printed address (default http://127.0.0.1:8080) and sign in.
6. **Connect Bale:** create a bot with @botfather, then either paste the token into `.env` (`BALE_BOT_TOKEN=`) and restart, or use *Dashboard → Settings → Bale bot*.
7. **Recommended `.env` values:** `CONTACT_EMAIL` (required by Unpaywall), `OPENALEX_API_KEY`, optional `CORE_API_KEY`. Then check each source on *Dashboard → Scientific sources → Test connection*.
8. Send a DOI such as `10.1038/nature12373` to your bot.

Stop with **Ctrl+C**. Later runs reuse `.venv` and are fast. Update: replace the code, keep `.env` and `storage/`, run the script again (migrations run automatically).
Run as a service/behind HTTPS: put a reverse proxy (nginx, Caddy) in front, set `COOKIE_SECURE=true`, `TRUSTED_HOSTS=your.domain`, `DASHBOARD_HOST=127.0.0.1`.

## فارسی

۱. **Python 3.8 یا جدیدتر را نصب کنید (هر نسخه‌ی جدیدتر هم کار می‌کند؛ ۳.۱۱ به بالا پیشنهاد می‌شود)** — https://www.python.org/downloads/ (ویندوز: گزینه *Add python.exe to PATH*؛ دبیان/اوبونتو: بسته `python3-venv` هم لازم است؛ macOS: `brew install python`).
۲. فایل `ScholarBot_Hamid_Yarali.zip` را در مسیری با دسترسی نوشتن باز کنید (پایگاه داده، PDFها، لاگ‌ها و پشتیبان‌ها داخل همین پوشه ذخیره می‌شوند).
۳. **اجرا**
   - ویندوز: روی `start_windows.bat` دابل‌کلیک کنید.
   - لینوکس / macOS: در ترمینال داخل پوشه این دستورها را بزنید:
     ```bash
     chmod +x start_unix.sh
     ./start_unix.sh
     ```
۴. در اولین اجرا اسکریپت `.venv` را می‌سازد، `requirements.txt` را نصب می‌کند، `.env` را از `.env.example` می‌سازد، مایگریشن‌ها را اجرا می‌کند و **مدیر اولیه** را می‌پرسد (نام کاربری و رمز حداقل ۱۰ نویسه).
۵. آدرس چاپ‌شده (پیش‌فرض http://127.0.0.1:8080) را باز کنید و وارد شوید.
۶. **اتصال به بله:** با @botfather ربات بسازید و توکن را در `.env` (`BALE_BOT_TOKEN=`) بگذارید و دوباره اجرا کنید، یا از *داشبورد ← تنظیمات ← ربات بله* استفاده کنید.
۷. **مقادیر پیشنهادی `.env`:** `CONTACT_EMAIL` (برای Unpaywall الزامی است)، `OPENALEX_API_KEY` و در صورت نیاز `CORE_API_KEY`. سپس در *داشبورد ← منابع علمی ← آزمایش اتصال* هر منبع را بررسی کنید.
۸. یک DOI مانند `10.1038/nature12373` برای ربات بفرستید.

توقف با **Ctrl+C**. اجراهای بعدی از `.venv` موجود استفاده می‌کنند و سریع‌اند. به‌روزرسانی: کد را جایگزین کنید، `.env` و `storage/` را نگه دارید و اسکریپت را دوباره اجرا کنید (مایگریشن‌ها خودکار اجرا می‌شوند).
برای اجرای دائمی/HTTPS: پشت یک Reverse Proxy (nginx یا Caddy) اجرا کنید و `COOKIE_SECURE=true`، `TRUSTED_HOSTS=دامنه_شما` و `DASHBOARD_HOST=127.0.0.1` را تنظیم کنید.
