import pytest
from sqlalchemy import select

from app.database.database import session_scope
from app.database.models import Article, ArticleFile
from app.database.repositories.articles import upsert_from_meta
from app.providers.base import ArticleMeta, OALink
from app.services.library import Library
from app.services.pdf_downloader import DownloadResult
from app.services.pdf_validator import sha256_file, validate_pdf
from app.services.storage_manager import StorageManager
from tests.helpers import make_pdf


def test_pdf_validation(tmp_path):
    good = tmp_path / "g.pdf"; good.write_bytes(make_pdf(3))
    r = validate_pdf(good, 10_000_000)
    assert r.ok and r.pages == 3
    cases = {"html.pdf": (b"<html>login</html>" * 20, "bad_signature"), "empty.pdf": (b"", "empty"),
             "trunc.pdf": (good.read_bytes()[:-60], "truncated"), "junk.pdf": (b"%PDF-1.4\n" + b"garbage" * 50 + b"\n%%EOF\n", "corrupt")}
    for name, (data, reason) in cases.items():
        p = tmp_path / name; p.write_bytes(data)
        assert validate_pdf(p).reason == reason, name
    assert validate_pdf(good, 100).reason == "too_large"
    assert len(sha256_file(good)) == 64


def test_storage_manager_lifecycle_and_traversal(settings):
    sm = StorageManager(settings)
    part = sm.part_path(); part.write_bytes(make_pdf())
    sha = sha256_file(part)
    rel = sm.adopt(part, sha, "A title / with ../ slashes")
    assert not part.exists() and sm.exists(rel, False) and ".." not in rel
    assert sm.move(rel, False, True) and sm.exists(rel, True) and not sm.exists(rel, False)
    assert sm.usage()["library_files"] == 1
    assert sm.delete(rel, True) and not sm.exists(rel, True)
    with pytest.raises(ValueError):
        sm.abs_path("../../etc/passwd", False)
    assert sm.exists("../../etc/passwd", False) is False and sm.delete("../../etc/passwd", False) is False


async def _article_with_file(session, lib, doi, title, pdf=None):
    meta = ArticleMeta(title=title, doi=doi, authors=["A B"], year=2024)
    art = await upsert_from_meta(session, meta, None)
    part = lib.storage.part_path(); data = pdf or make_pdf(1, extra=doi.encode()); part.write_bytes(data)
    dl = DownloadResult(part, sha256_file(part), len(data), 1, OALink("https://repo.example.org/x.pdf", "unpaywall"), "https://repo.example.org/x.pdf")
    await lib.attach_file(session, art, dl)
    return art


async def test_upsert_dedupes_by_doi_and_title_hash(db):
    async with session_scope() as s:
        a1 = await upsert_from_meta(s, ArticleMeta(title="Same Title Here", doi="10.1/a", authors=["X Y"], year=2020), None)
        a2 = await upsert_from_meta(s, ArticleMeta(title="Different", doi="10.1/a"), None)
        b1 = await upsert_from_meta(s, ArticleMeta(title="No DOI Paper Title", authors=["Z W"], year=2021), None)
        b2 = await upsert_from_meta(s, ArticleMeta(title="No DOI paper title!", authors=["Z W"], year=2021), None)
        b3 = await upsert_from_meta(s, ArticleMeta(title="No DOI Paper Title", authors=["Z W"], year=2021, doi="10.2/b"), None)
        assert a1.id == a2.id and a2.request_count == 2
        assert b1.id == b2.id == b3.id and b3.doi == "10.2/b"
        assert len((await s.execute(select(Article))).scalars().all()) == 2


async def test_identical_files_are_stored_once(settings, db):
    lib = Library(StorageManager(settings))
    same = make_pdf(1, extra=b"same")
    async with session_scope() as s:
        a = await _article_with_file(s, lib, "10.1/one", "One", same)
        b = await _article_with_file(s, lib, "10.1/two", "Two", same)
        assert a.file_id == b.file_id
        assert len((await s.execute(select(ArticleFile))).scalars().all()) == 1


async def test_save_unsave_delete_with_shared_file_protection(settings, db):
    lib = Library(StorageManager(settings))
    shared = make_pdf(1, extra=b"shared")
    async with session_scope() as s:
        a = await _article_with_file(s, lib, "10.1/one", "One", shared)
        b = await _article_with_file(s, lib, "10.1/two", "Two", shared)
        c = await _article_with_file(s, lib, "10.1/three", "Three")
        r = await lib.save(s, [a, c], "root")
        assert r == {"saved": 2, "already": 0, "no_file": 0}
        assert a.file.is_permanent and lib.storage.exists(a.file.path, True)
        assert (await lib.save(s, [a], "root"))["already"] == 1
        # deleting A with its file must keep the shared PDF because B still uses it
        res = await lib.delete_articles(s, [a], "info_and_file")
        assert res["deleted"] == 1 and res["shared_files_kept"] == 1 and res["files_removed"] == 0
        assert a.status == "deleted" and b.file is not None and lib.storage.exists(b.file.path, b.file.is_permanent or False)
        # unsaving C moves its file back to the temporary area; deleting with file removes it
        assert await lib.unsave(s, [c]) == 1 and not c.file.is_permanent and lib.storage.exists(c.file.path, False)
        path = c.file.path
        res = await lib.delete_articles(s, [c], "info_and_file")
        assert res["files_removed"] == 1 and not lib.storage.exists(path, False)


async def test_cleanup_removes_only_expired_temporary_files(settings, db):
    from datetime import timedelta
    from app.database.models import utcnow
    lib = Library(StorageManager(settings))
    async with session_scope() as s:
        old = await _article_with_file(s, lib, "10.1/old", "Old")
        keep = await _article_with_file(s, lib, "10.1/keep", "Keep")
        fresh = await _article_with_file(s, lib, "10.1/fresh", "Fresh")
        await lib.save(s, [keep], "root")
        old.file.last_used_at = utcnow() - timedelta(hours=48)
        keep.file.last_used_at = utcnow() - timedelta(hours=48)
        old_path = old.file.path
    async with session_scope() as s:
        res = await lib.cleanup(s, 24)
        assert res["temp_files_removed"] == 1
    assert not lib.storage.exists(old_path, False)
    async with session_scope() as s:
        files = (await s.execute(select(ArticleFile))).scalars().all()
        assert {f.is_permanent for f in files} == {True, False} and len(files) == 2


async def test_zip_export(settings, db):
    import zipfile
    lib = Library(StorageManager(settings))
    async with session_scope() as s:
        a = await _article_with_file(s, lib, "10.1/one", "One")
        z = await lib.zip_files([a], "bundle")
    assert z and zipfile.ZipFile(z).namelist() == ["10.1_one.pdf"]
