import pytest

from app.core.logging_config import redact
from app.core.security import RateLimiter, hash_password, password_problem, safe_filename, safe_join, tokens_match, verify_password
from app.providers.base import normalize_doi
from app.services.parsing import InvalidInput, parse_input


@pytest.mark.parametrize("text,expected", [
    ("10.1038/nature12373", "10.1038/nature12373"),
    ("https://doi.org/10.1038/NATURE12373", "10.1038/nature12373"),
    ("doi:10.1000/xyz123.", "10.1000/xyz123"),
    ("DOI: 10.1016/j.cell.2020.01.001)", "10.1016/j.cell.2020.01.001"),
    ("https://link.springer.com/article/10.1007/s00134-020-06022-5", "10.1007/s00134-020-06022-5"),
])
def test_doi_detection(text, expected):
    assert normalize_doi(text) == expected
    q = parse_input(text)
    assert q.kind == "doi" and q.value == expected


def test_arxiv_pmc_pmid_url_title_author():
    assert parse_input("arXiv:1706.03762v5").kind == "arxiv" and parse_input("arXiv:1706.03762v5").value == "1706.03762"
    assert parse_input("https://arxiv.org/abs/1706.03762").value == "1706.03762"
    assert parse_input("https://arxiv.org/pdf/1706.03762.pdf").value == "1706.03762"
    assert parse_input("PMC1234567").kind == "pmcid"
    assert parse_input("https://pubmed.ncbi.nlm.nih.gov/12345678/").kind == "pmid"
    assert parse_input("https://example.org/some/paper").kind == "url"
    assert parse_input("Attention is all you need").kind == "title"
    assert parse_input("Geoffrey Hinton", "author").kind == "author"
    assert parse_input("10.48550/arXiv.1706.03762").ref.arxiv_id == "1706.03762"


def test_invalid_inputs():
    for bad, hint, code in [("", None, "empty_query"), ("x" * 600, None, "query_too_long"), ("not a doi", "doi", "invalid_doi"),
                            ("just words", "link", "invalid_link"), ("ab", "author", "query_too_short"), ("hi", None, "query_too_short"),
                            ("www.example.org/x", None, "invalid_link")]:
        with pytest.raises(InvalidInput) as e:
            parse_input(bad, hint)
        assert e.value.code == code


def test_password_hashing_and_policy():
    h = hash_password("Correct-Horse-9")
    assert h.startswith("$argon2") and verify_password(h, "Correct-Horse-9") and not verify_password(h, "wrong")
    assert not verify_password("garbage", "x")
    assert password_problem("short1A") == "err_password_short"
    assert password_problem("1234567890123") == "err_password_weak"
    assert password_problem("Correct-Horse-9") is None


def test_rate_limiter():
    rl = RateLimiter(2, 60)
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a") and rl.allow("b")
    assert rl.retry_after("a") > 0


def test_filename_and_path_safety(tmp_path):
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("مقاله فارسی") == "article"
    assert "/" not in safe_filename("10.1038/nature") and safe_filename("a" * 300, 40) == "a" * 40
    assert safe_join(tmp_path, "ab", "x.pdf").parent.parent == tmp_path.resolve()
    for evil in ("../x", "/etc/passwd", "ab/../../x"):
        with pytest.raises(ValueError):
            safe_join(tmp_path, evil)


def test_tokens_and_redaction():
    assert tokens_match("abc", "abc") and not tokens_match("abc", "abd") and not tokens_match("", "") and not tokens_match(None, "x")
    out = redact("GET https://tapi.bale.ai/bot123456:ABCDEFGHIJKLMNOP/getMe?api_key=SECRET&email=a@b.c")
    assert "ABCDEFGHIJKLMNOP" not in out and "SECRET" not in out and "a@b.c" not in out
