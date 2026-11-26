import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from app.database.database import session_scope
from app.database.models import Article, Delivery, DownloadJob, ErrorEvent, ProviderConfig, User
from app.services.pipeline import DeliveryReport
from tests.helpers import CROSSREF_WORK, DOI, UNPAYWALL_CLOSED, UNPAYWALL_OA, jresp, make_pdf, pdf_host, standard_providers


class Recorder:
    """Notifier double that records events and 'delivers' successfully."""

    def __init__(self, deliver_ok: bool = True):
        self.events: list[tuple] = []
        self.deliver_ok = deliver_ok

    async def progress(self, job, stage): self.events.append(("progress", stage))
    async def candidates(self, job, metas): self.events.append(("candidates", [m.title for m in metas]))
    async def failure(self, job, code): self.events.append(("failure", code))

    async def article(self, job, article, file, reason):
        self.events.append(("article", article.doi, bool(file), reason))
        if file is None:
            return None
        return DeliveryReport(True, 77) if self.deliver_ok else DeliveryReport(False, error="bale down")


async def new_job(text, kind="doi", user_bale=1):
    async with session_scope() as s:
        user = (await s.execute(select(User).where(User.bale_id == user_bale))).scalar_one_or_none()
        if user is None:
            user = User(bale_id=user_bale, language="en"); s.add(user); await s.flush()
        job = DownloadJob(user_id=user.id, chat_id=user_bale, lang="en", query_text=text, query_type=kind, status="queued")
        s.add(job); await s.flush()
        return job.id


async def run(svc, text, kind="doi", user_bale=1, notifier=None):
    rec = notifier or Recorder()
    svc.pipeline.notifier = rec
    jid = await new_job(text, kind, user_bale)
    await svc.pipeline.run_job(jid)
    async with session_scope() as s:
        job = await s.get(DownloadJob, jid)
    return job, rec


async def test_doi_to_delivery_records_everything(services):
    job, rec = await run(services, DOI)
    assert job.status == "sent" and job.source == "unpaywall" and job.finished_at and job.started_at
    assert ("article", DOI, True, None) in rec.events
    async with session_scope() as s:
        art = (await s.execute(select(Article))).scalars().one()
        assert art.status == "sent" and art.file.size_bytes > 0 and art.file.validation_status == "valid" and not art.file.is_permanent
        assert art.title == "Deep Learning for Open Science" and art.authors == ["Ada Lovelace", "Alan Turing"] and art.access_status == "open"
        d = (await s.execute(select(Delivery))).scalars().one()
        assert d.status == "sent" and d.bale_message_id == 77
        hits = (await s.get(ProviderConfig, "unpaywall")).total_hits
        assert hits == 1 and services.storage.exists(art.file.path, False)


async def test_second_request_reuses_stored_file_without_downloading(services):
    await run(services, DOI)
    await run(services, DOI, user_bale=2)
    assert services.mocks["downloads"].count("repo.example.org") == 1
    async with session_scope() as s:
        assert (await s.execute(select(func.count(Article.id)))).scalar_one() == 1
        assert (await s.execute(select(func.count(Delivery.id)))).scalar_one() == 2
        assert (await s.execute(select(Article))).scalars().one().request_count == 2


async def test_concurrent_identical_requests_download_once(services):
    services.pipeline.notifier = Recorder()
    ids = [await new_job(DOI, user_bale=i) for i in range(1, 5)]
    await asyncio.gather(*(services.pipeline.run_job(i) for i in ids))
    assert services.mocks["downloads"].count("repo.example.org") == 1
    async with session_scope() as s:
        assert (await s.execute(select(func.count(Article.id)))).scalar_one() == 1
        assert (await s.execute(select(func.count(Delivery.id)))).scalar_one() == 4


async def test_no_legal_copy_returns_metadata_and_official_links_only(services):
    services.mocks["providers"].on("api.unpaywall.org", lambda r: jresp(UNPAYWALL_CLOSED))
    job, rec = await run(services, DOI)
    assert job.status == "found" and job.error_code == "no_legal_pdf"
    assert ("article", DOI, False, "no_legal_pdf") in rec.events
    assert services.mocks["downloads"].calls == []          # nothing is fetched when no legal link exists
    async with session_scope() as s:
        art = (await s.execute(select(Article))).scalars().one()
        assert art.status == "failed" and art.file is None and art.doi_url == f"https://doi.org/{DOI}"


async def test_html_login_page_is_never_accepted_as_pdf(services):
    services.mocks["downloads"].handlers["repo.example.org"] = pdf_host(html=True).handlers["repo.example.org"]
    job, rec = await run(services, DOI)
    assert job.status == "found" and job.error_code == "only_publisher_page"
    assert services.storage.usage()["stored_bytes"] == 0 and list(services.settings.temp_dir.glob("*.part")) == []


async def test_corrupt_oversized_and_error_responses(services):
    for handler_kwargs, expected in [({"pdf": b"%PDF-1.4 broken " * 20}, "file_corrupt"), ({"status": 500}, "download_failed")]:
        services.mocks["downloads"].handlers["repo.example.org"] = pdf_host(**handler_kwargs).handlers["repo.example.org"]
        async with session_scope() as s:
            await s.execute(Article.__table__.delete())
        job, _ = await run(services, DOI)
        assert job.error_code == expected, (handler_kwargs, job.error_code)
    from app.core.runtime_settings import runtime
    runtime._v["max_pdf_mb"] = 1
    services.mocks["downloads"].handlers["repo.example.org"] = pdf_host(pdf=make_pdf(1, extra=b"0" * (1_100_000))).handlers["repo.example.org"]
    async with session_scope() as s:
        await s.execute(Article.__table__.delete())
    job, _ = await run(services, DOI)
    assert job.error_code == "too_large"


async def test_one_source_down_still_works_all_down_is_reported(services):
    services.mocks["providers"].on("api.openalex.org", lambda r: httpx.Response(500))
    job, _ = await run(services, DOI)
    assert job.status == "sent"
    async with session_scope() as s:
        assert (await s.get(ProviderConfig, "openalex")).total_errors >= 1
        assert (await s.execute(select(func.count(ErrorEvent.id)).where(ErrorEvent.kind == "provider"))).scalar_one() >= 1
    for host in list(services.mocks["providers"].handlers):
        services.mocks["providers"].on(host, lambda r: httpx.Response(503))
    job, rec = await run(services, "10.9999/unknown", user_bale=9)
    assert job.status == "failed" and job.error_code == "sources_unavailable" and ("failure", "sources_unavailable") in rec.events


async def test_unknown_doi_is_not_found(services):
    services.mocks["providers"].on("api.crossref.org", lambda r: httpx.Response(404))
    services.mocks["providers"].on("api.unpaywall.org", lambda r: httpx.Response(404))
    job, rec = await run(services, "10.5555/does-not-exist")
    assert job.status == "not_found" and ("failure", "not_found") in rec.events


async def test_title_search_offers_candidates_then_pick_delivers(services):
    second = dict(CROSSREF_WORK["message"], DOI="10.1234/other", title=["Deep Learning for Open Science: A Survey"])
    services.mocks["providers"].on("api.crossref.org", lambda r: jresp({"message": {"items": [CROSSREF_WORK["message"], second]}})
                                   if r.url.path == "/works" else jresp(CROSSREF_WORK))
    job, rec = await run(services, "deep learning open", kind="title")
    assert job.status == "awaiting_choice" and len(job.candidates) == 2 and rec.events[-1][0] == "candidates"
    pick_id = await new_job("pick", "pick")
    async with session_scope() as s:
        j = await s.get(DownloadJob, pick_id); j.candidates = [job.candidates[0]]
    services.pipeline.notifier = rec
    await services.pipeline.run_job(pick_id)
    async with session_scope() as s:
        assert (await s.get(DownloadJob, pick_id)).status == "sent"


async def test_exact_title_match_resolves_directly(services):
    job, rec = await run(services, "Deep Learning for Open Science", kind="title")
    assert job.status == "sent"


async def test_delivery_failure_is_recorded_and_article_kept(services):
    job, rec = await run(services, DOI, notifier=Recorder(deliver_ok=False))
    assert job.status == "found" and job.error_code == "delivery_failed"
    async with session_scope() as s:
        assert (await s.execute(select(Delivery))).scalars().one().status == "failed"
        assert (await s.execute(select(func.count(ErrorEvent.id)).where(ErrorEvent.kind == "delivery"))).scalar_one() == 1
        assert (await s.execute(select(Article))).scalars().one().status == "received"


async def test_url_input_uses_doi_in_url_and_blocks_piracy_hosts(services):
    job, _ = await run(services, f"https://publisher.example.com/doi/{DOI}", kind="url")
    assert job.status == "sent"
    job, rec = await run(services, "https://sci-hub.example/some-page", kind="url", user_bale=3)
    assert job.status == "failed" and job.error_code == "unsupported_source"


async def test_url_page_metadata_is_read_safely(services):
    html = f'<html><head><meta name="citation_doi" content="{DOI}"></head></html>'
    services.mocks["downloads"].handlers["publisher.example.com"] = lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"})
    job, _ = await run(services, "https://publisher.example.com/article/42", kind="url")
    assert job.status == "sent"
    job, _ = await run(services, "http://169.254.169.254/latest/meta-data", kind="url", user_bale=5)
    assert job.error_code == "unsafe_url"


async def test_task_manager_processes_queue_and_recovers_unfinished(services):
    services.pipeline.notifier = Recorder()
    await services.tasks.start()
    jid = await new_job(DOI)
    await services.tasks.submit(jid)
    for _ in range(100):
        await asyncio.sleep(0.05)
        async with session_scope() as s:
            if (await s.get(DownloadJob, jid)).status == "sent":
                break
    else:
        pytest.fail("job not processed")
    await services.tasks.resize(1)
    await services.tasks.stop()
    # a restart re-queues recent unfinished jobs and fails stale ones
    from datetime import timedelta
    from app.database.models import utcnow
    j_recent, j_old = await new_job("10.1234/r", user_bale=7), await new_job("10.1234/o", user_bale=8)
    async with session_scope() as s:
        (await s.get(DownloadJob, j_old)).created_at = utcnow() - timedelta(hours=3)
    await services.tasks._resume_unfinished()
    assert services.tasks.queued == 1
    async with session_scope() as s:
        assert (await s.get(DownloadJob, j_old)).error_code == "interrupted"
