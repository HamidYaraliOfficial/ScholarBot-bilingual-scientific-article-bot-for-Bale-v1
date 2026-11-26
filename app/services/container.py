"""Wires all long-lived services together."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.bot.runner import BotNotifier, BotRunner
from app.core.config import Settings, get_settings
from app.providers.registry import ProviderManager
from app.services.article_search import ArticleSearch
from app.services.library import Library
from app.services.maintenance import Maintenance
from app.services.pdf_downloader import PDFDownloader
from app.services.pipeline import ArticlePipeline
from app.services.storage_manager import StorageManager
from app.services.task_manager import TaskManager


@dataclass
class Services:
    settings: Settings
    providers: ProviderManager
    search: ArticleSearch
    downloader: PDFDownloader
    storage: StorageManager
    library: Library
    pipeline: ArticlePipeline
    tasks: TaskManager
    runner: BotRunner
    maintenance: Maintenance


def build_services(settings: Settings | None = None, *, provider_transport: httpx.AsyncBaseTransport | None = None,
                   download_transport: httpx.AsyncBaseTransport | None = None,
                   bale_transport: httpx.AsyncBaseTransport | None = None, concurrency: int = 3) -> Services:
    settings = settings or get_settings()
    storage = StorageManager(settings)
    providers = ProviderManager(settings, provider_transport)
    search = ArticleSearch(providers, settings, download_transport)
    downloader = PDFDownloader(storage, settings, download_transport)
    library = Library(storage)
    pipeline = ArticlePipeline(providers, search, downloader, library)
    tasks = TaskManager(pipeline, concurrency)
    runner = BotRunner(settings, tasks, library, storage, bale_transport)
    pipeline.notifier = BotNotifier(runner)
    maintenance = Maintenance(library, settings)
    runner.providers, runner.maintenance = providers, maintenance
    return Services(settings, providers, search, downloader, storage, library, pipeline, tasks, runner, maintenance)
