"""Optional: runs app.js inside jsdom against the real rendered pages. Skipped unless node + jsdom are installed
(`cd tests/js && npm install`)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import login
from tests.test_dashboard import seed_articles

JS_DIR = Path(__file__).parent / "js"
pytestmark = pytest.mark.skipif(not shutil.which("node") or not (JS_DIR / "node_modules" / "jsdom").exists(),
                                reason="node/jsdom not installed (cd tests/js && npm install)")


async def test_dashboard_javascript_in_jsdom(client, services, tmp_path):
    await login(client)
    await seed_articles(services, 12, with_files=False)
    (tmp_path / "articles.html").write_text((await client.get("/articles?per_page=10")).text, encoding="utf-8")
    (tmp_path / "dashboard.html").write_text((await client.get("/")).text, encoding="utf-8")
    app_js = Path(__file__).parents[1] / "app" / "dashboard" / "static" / "app.js"
    r = subprocess.run(["node", str(JS_DIR / "ui.test.js"), str(tmp_path / "articles.html"), str(app_js), str(tmp_path / "dashboard.html")],
                       capture_output=True, text=True, cwd=JS_DIR, timeout=60)
    report = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"stdout": r.stdout, "stderr": r.stderr}
    assert r.returncode == 0, json.dumps(report, indent=1, ensure_ascii=False)
    assert report["total"] >= 20
