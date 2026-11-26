"""ScholarBot entry point: python run.py"""
import uvicorn

from app.cli import migrate
from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    migrate()
    print(f"\n  ScholarBot dashboard: http://{settings.dashboard_host}:{settings.dashboard_port}\n")
    uvicorn.run("app.main:create_app", factory=True, host=settings.dashboard_host, port=settings.dashboard_port,
                log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()
