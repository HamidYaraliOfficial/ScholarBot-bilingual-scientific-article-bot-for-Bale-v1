import json

import httpx
import pytest

from app.core.runtime_settings import runtime
from app.providers.base import ArticleMeta, OALink, ProviderError, ProviderHTTP, Ref
from app.providers.registry import ProviderManager
from app.services.article_metadata import title_hash, to_bibtex, to_ris, usable_links
from tests.helpers import CROSSREF_WORK, DOI, HostMock, UNPAYWALL_OA, jresp, standard_providers

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/1706.03762v7</id><published>2017-06-12T17:57:34Z</published><title>Attention Is All You Need</title>
<summary>The dominant sequence transduction models...</summary><author><name>Ashish Vaswani</name></author><author><name>Noam Shazeer</name></author>
<category term="cs.CL"/><arxiv:journal_ref>NeurIPS 2017</arxiv:journal_ref></entry></feed>"""
OPENALEX = {"results": [{"id": "https://openalex.org/W123", "doi": "https://doi.org/10.1234/ABC", "title": "Deep Learning for Open Science",
    "publication_year": 2024, "authorships": [{"author": {"display_name": "Ada Lovelace"}}], "type": "article",
    "primary_location": {"source": {"display_name": "Journal of Open Research", "issn_l": "1234-5678", "host_organization_name": "Open Press", "type": "journal"},
                         "landing_page_url": "https://pub.example.com/1"},
    "biblio": {"volume": "12", "issue": "3", "first_page": "100", "last_page": "110"}, "open_access": {"is_oa": True},
    "best_oa_location": {"license": "cc-by"}, "abstract_inverted_index": {"Open": [0], "science": [1], "rocks": [2]},
    "keywords": [{"display_name": "open science"}], "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/999", "pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC555"},
    "locations": [{"is_oa": True, "pdf_url": "https://repo.example.org/oa.pdf", "license": "cc-by", "version": "publishedVersion", "source": {"type": "repository"}},
                  {"is_oa": True, "pdf_url": None, "landing_page_url": "https://repo.example.org/landing", "source": {"type": "journal"}},
                  {"is_oa": False, "pdf_url": "https://closed.example.com/x.pdf"}]}]}
EPMC = {"resultList": {"result": [{"id": "999", "source": "MED", "pmid": "999", "pmcid": "PMC555", "doi": "10.1234/abc", "title": "Deep Learning for Open Science.",
    "authorList": {"author": [{"fullName": "Lovelace A"}]}, "pubYear": "2024", "isOpenAccess": "Y", "license": "cc by",
    "journalInfo": {"volume": "12", "issue": "3", "journal": {"title": "Journal of Open Research", "issn": "1234-5678"}},
    "fullTextUrlList": {"fullTextUrl": [{"availabilityCode": "OA", "documentStyle": "pdf", "url": "https://europepmc.org/articles/PMC555?pdf=render"},
                                        {"availabilityCode": "S", "documentStyle": "pdf", "url": "https://paywalled.example.com/x.pdf"}]}}]}}
DOAJ = {"results": [{"bibjson": {"title": "Deep Learning for Open Science", "author": [{"name": "Ada Lovelace"}], "year": "2024",
    "journal": {"title": "Journal of Open Research", "publisher": "Open Press", "license": [{"type": "CC BY"}]},
    "identifier": [{"type": "doi", "id": "10.1234/abc"}], "link": [{"type": "fulltext", "url": "https://journal.example.com/a1", "content_type": "HTML"},
                                                                 {"type": "fulltext", "url": "https://journal.example.com/a1.pdf", "content_type": "PDF"}]}}]}
CORE = {"results": [{"title": "Deep Learning for Open Science", "authors": [{"name": "Ada Lovelace"}], "doi": "10.1234/abc", "yearPublished": 2024,
                     "downloadUrl": "https://core.example.org/download/1.pdf"}]}


def mgr(mock: HostMock, settings) -> ProviderManager:
    return ProviderManager(settings, mock.transport())


async def test_crossref_parsing_and_polite_params(settings):
    m = standard_providers(); pm = mgr(m, settings)
    meta = (await pm.providers["crossref"].lookup(Ref(doi=DOI)))
    assert meta.title == "Deep Learning for Open Science" and meta.authors == ["Ada Lovelace", "Alan Turing"] and meta.year == 2024
    assert meta.abstract == "An abstract." and meta.volume == "12" and meta.issn == "1234-5678" and meta.doi == DOI
    assert "mailto=ops%40example.org" in str(m.calls[0].url)
    found = await pm.providers["crossref"].search("deep learning", "title", 5)
    assert found[0].doi == DOI
    await pm.aclose()


async def test_openalex_key_abstract_and_oa_links(settings):
    settings.openalex_api_key = "KEY123"
    m = HostMock().on("api.openalex.org", lambda r: jresp(OPENALEX)); pm = mgr(m, settings)
    meta = await pm.providers["openalex"].lookup(Ref(doi=DOI))
    assert meta.abstract == "Open science rocks" and meta.pages == "100-110" and meta.pmcid == "PMC555" and meta.openalex_id == "W123"
    assert {(l.url, l.kind) for l in meta.links} == {("https://repo.example.org/oa.pdf", "pdf"), ("https://repo.example.org/landing", "landing")}
    assert "api_key=KEY123" in str(m.calls[0].url) and "filter=doi" in str(m.calls[0].url)
    await pm.providers["openalex"].search("Ada Lovelace", "author", 3)
    assert "raw_author_name.search" in str(m.calls[-1].url)
    await pm.aclose()


async def test_unpaywall_requires_email_and_parses_locations(settings):
    m = standard_providers(); pm = mgr(m, settings)
    meta = await pm.providers["unpaywall"].lookup(Ref(doi=DOI))
    assert meta.access_status == "open" and meta.links[0].url == "https://repo.example.org/paper.pdf" and meta.links[0].license == "cc-by"
    settings.contact_email = ""
    assert not pm.providers["unpaywall"].is_configured() and pm.providers["unpaywall"] not in pm.active()
    await pm.aclose()


async def test_europe_pmc_only_uses_open_access_pdf_links(settings):
    m = HostMock().on("www.ebi.ac.uk", lambda r: jresp(EPMC)); pm = mgr(m, settings)
    meta = await pm.providers["europe_pmc"].lookup(Ref(doi=DOI))
    urls = [l.url for l in meta.links]
    assert "https://paywalled.example.com/x.pdf" not in urls
    assert "https://europepmc.org/articles/PMC555?pdf=render" in urls and any("ptpmcrender" in u and "PMC555" in u for u in urls)
    assert meta.title == "Deep Learning for Open Science" and meta.pmcid == "PMC555"
    await pm.aclose()


async def test_arxiv_doaj_core(settings):
    settings.core_api_key = "corekey"
    m = (HostMock().on("export.arxiv.org", lambda r: httpx.Response(200, text=ATOM)).on("doaj.org", lambda r: jresp(DOAJ))
         .on("api.core.ac.uk", lambda r: jresp(CORE) if r.headers.get("authorization") == "Bearer corekey" else httpx.Response(401)))
    pm = mgr(m, settings)
    ax = await pm.providers["arxiv"].lookup(Ref(arxiv_id="1706.03762"))
    assert ax.title == "Attention Is All You Need" and ax.links[0].url == "https://arxiv.org/pdf/1706.03762" and ax.doi == "10.48550/arxiv.1706.03762"
    dj = await pm.providers["doaj"].lookup(Ref(doi=DOI))
    assert [l.kind for l in dj.links] == ["landing", "pdf"] and dj.license == "CC BY" and dj.access_status == "open"
    co = await pm.providers["core"].lookup(Ref(doi=DOI))
    assert co.links[0].url.endswith("1.pdf")
    await pm.aclose()


async def test_http_wrapper_retries_and_error_mapping(settings, monkeypatch):
    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr("app.providers.base.asyncio.sleep", no_sleep)
    runtime._v["provider_retries"] = 2
    calls = {"n": 0}

    def flaky(r):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else jresp({"ok": 1})
    http = ProviderHTTP("t", settings=settings, transport=HostMock().on("x.org", flaky).transport())
    assert (await http.get_json("https://x.org/a")) == {"ok": 1} and calls["n"] == 3
    for status, kind in [(401, "auth"), (403, "auth"), (400, "bad_response"), (503, "unavailable")]:
        h = ProviderHTTP("t", settings=settings, transport=HostMock().on("x.org", lambda r, s=status: httpx.Response(s)).transport())
        with pytest.raises(ProviderError) as e:
            await h.get_json("https://x.org/a")
        assert e.value.kind == kind
    def boom(r): raise httpx.ConnectTimeout("slow")
    h = ProviderHTTP("t", settings=settings, transport=HostMock().on("x.org", boom).transport())
    with pytest.raises(ProviderError) as e:
        await h.get_json("https://x.org/a")
    assert e.value.kind == "timeout"
    assert await ProviderHTTP("t", settings=settings, transport=HostMock().transport()).get_json("https://nowhere.org/a", allow_404=True) is None


async def test_manager_merges_sources_and_survives_one_outage(db, settings):
    m = standard_providers()
    m.on("api.openalex.org", lambda r: httpx.Response(500))     # an outage must not fail the request
    m.on("www.ebi.ac.uk", lambda r: jresp(EPMC))
    pm = mgr(m, settings); await pm.load()
    res = await pm.lookup(Ref(doi=DOI))
    assert res.meta.title == "Deep Learning for Open Science" and res.meta.pmcid == "PMC555"
    assert {e.provider for e in res.errors} == {"openalex"}
    urls = [l.url for l in res.meta.links]
    assert "https://repo.example.org/paper.pdf" in urls and any("ptpmcrender" in u for u in urls)
    ranked = pm.rank_links(res.meta.links)
    assert ranked[0].kind == "pdf"
    await pm.aclose()


async def test_disabled_provider_is_not_called_and_priority_orders_links(db, settings):
    m = standard_providers(); pm = mgr(m, settings); await pm.load()
    pm.config["unpaywall"]["enabled"] = False
    await pm.lookup(Ref(doi=DOI))
    assert m.count("api.unpaywall.org") == 0 and m.count("api.crossref.org") >= 1
    a = OALink("https://a.org/a.pdf", "core", "pdf", "publishedVersion", "cc-by"); b = OALink("https://b.org/b.pdf", "europe_pmc", "pdf", "publishedVersion", "cc-by")
    pm.config["core"]["priority"], pm.config["europe_pmc"]["priority"] = 1, 2
    assert pm.rank_links([b, a])[0] is a
    pm.config["core"]["priority"] = 99
    assert pm.rank_links([a, b])[0] is b
    await pm.aclose()


async def test_search_dedupes_and_ranks(db, settings):
    m = standard_providers()
    m.on("doaj.org", lambda r: jresp(DOAJ))
    m.on("api.openalex.org", lambda r: jresp(OPENALEX))
    pm = mgr(m, settings); await pm.load()
    results, errors = await pm.search("Deep Learning for Open Science", "title", 5)
    assert len(results) == 1 and results[0].doi == DOI and {"crossref", "openalex", "doaj"} <= set(results[0].sources)
    await pm.aclose()


def test_link_policy_and_citation_formats():
    links = [OALink("https://a/x.pdf", "u", license="all-rights-reserved"), OALink("https://b/x.pdf", "u", license="cc-by"), OALink("https://c/x.pdf", "u")]
    assert [l.url for l in usable_links(links, False)] == ["https://b/x.pdf", "https://c/x.pdf"]
    assert [l.url for l in usable_links(links, True)] == ["https://b/x.pdf"]
    art = ArticleMeta(title="A & B: 50% {Test}", authors=["Ada Lovelace"], year=2024, journal="J_1", doi="10.1/x", pages="10-20", volume="2")
    bib = to_bibtex([art]); ris = to_ris([art])
    assert "@article{lovelace2024" in bib and "A \\& B: 50\\% Test" in bib and "pages = {10--20}" in bib and "doi = {10.1/x}" in bib
    assert "TY  - JOUR" in ris and "AU  - Ada Lovelace" in ris and "SP  - 10" in ris and "EP  - 20" in ris and ris.rstrip().endswith("ER  -")
    assert title_hash("A Long Enough Title", ["Ada Lovelace"], 2020) == title_hash("a long enough title!", ["Ada Lovelace"], 2020)
    assert title_hash("short", None, None) is None


async def test_provider_defaults_can_be_seeded_from_config_file_once(db, settings, tmp_path):
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"crossref": {"enabled": False, "priority": 99}, "doaj": {"enabled": True, "priority": 3},
                               "arxiv": {"enabled": "yes", "priority": 5}, "bogus": {"priority": 1}, "core": {"priority": 0}}))
    settings.providers_config = cfg
    pm = mgr(standard_providers(), settings); await pm.load()
    assert pm.config["crossref"] == {"enabled": False, "priority": 99} and pm.config["doaj"]["priority"] == 3
    assert pm.config["arxiv"]["priority"] == 60 and pm.config["core"]["priority"] == 70      # invalid entries are ignored
    cfg.write_text(json.dumps({"crossref": {"enabled": True, "priority": 1}}))
    pm2 = mgr(standard_providers(), settings); await pm2.load()
    assert pm2.config["crossref"] == {"enabled": False, "priority": 99}                    # the database wins after first run
    settings.providers_config = tmp_path / "missing.json"
    await pm.aclose(); await pm2.aclose()
