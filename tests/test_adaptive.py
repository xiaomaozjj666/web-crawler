"""Tests for the adaptive engine: fingerprinting, similarity, storage."""

from __future__ import annotations

from lxml import etree

from web_crawler.parser.adaptive import (
    AdaptiveStorage,
    best_match,
    compute_fingerprint,
    similarity_score,
)


def _el(html: str) -> etree._Element:
    return etree.fromstring(html)


def test_fingerprint_is_json_string() -> None:
    fp = compute_fingerprint(_el('<a class="x">hi</a>'))
    assert isinstance(fp, str)
    import json

    parsed = json.loads(fp)
    assert parsed["tag"] == "a"
    assert parsed["class_tokens"] == ["x"]
    assert parsed["text_sample"] == "hi"


def test_fingerprint_identical_elements_score_one() -> None:
    a = compute_fingerprint(_el('<a class="x">hi</a>'))
    b = compute_fingerprint(_el('<a class="x">hi</a>'))
    assert similarity_score(a, b) == 1.0


def test_fingerprint_completely_different_score_low() -> None:
    a = compute_fingerprint(_el('<a class="x">hi</a>'))
    b = compute_fingerprint(_el('<div class="y"><span>bye</span></div>'))
    assert similarity_score(a, b) < 0.5


def test_fingerprint_class_token_order_invariant() -> None:
    a = compute_fingerprint(_el('<a class="c b a">x</a>'))
    b = compute_fingerprint(_el('<a class="a b c">x</a>'))
    assert similarity_score(a, b) == 1.0


def test_similarity_score_handles_invalid_json() -> None:
    assert similarity_score("not json", "{}") == 0.0
    assert similarity_score("{}", "") == 0.0


def test_best_match_returns_best_above_threshold() -> None:
    stored = compute_fingerprint(_el('<a class="product">Product 1</a>'))
    candidates = [
        _el('<a class="product new">Product 1</a>'),
        _el("<div>unrelated</div>"),
        _el("<span>noise</span>"),
    ]
    best, score = best_match(candidates, stored, threshold=0.3)
    assert best is not None
    assert best.tag == "a"
    assert score >= 0.3


def test_best_match_returns_none_below_threshold() -> None:
    stored = compute_fingerprint(_el('<a class="product">Product 1</a>'))
    candidates = [_el("<div>totally different</div>")]
    best, _score = best_match(candidates, stored, threshold=0.9)
    assert best is None


def test_storage_save_load_roundtrip(tmp_storage: AdaptiveStorage) -> None:
    fp = compute_fingerprint(_el('<a id="x">text</a>'))
    tmp_storage.save("example.com", "#x", fp, tag="a", text="text", url="https://example.com")
    record = tmp_storage.load("example.com", "#x")
    assert record is not None
    assert record["fingerprint"] == fp
    assert record["tag"] == "a"
    assert record["text"] == "text"


def test_storage_save_overwrites_on_conflict(tmp_storage: AdaptiveStorage) -> None:
    tmp_storage.save("d", "id", "fp1", tag="a")
    tmp_storage.save("d", "id", "fp2", tag="b")
    record = tmp_storage.load("d", "id")
    assert record is not None
    assert record["fingerprint"] == "fp2"
    assert record["tag"] == "b"


def test_storage_load_missing_returns_none(tmp_storage: AdaptiveStorage) -> None:
    assert tmp_storage.load("nope", "missing") is None


def test_storage_load_all_and_delete(tmp_storage: AdaptiveStorage) -> None:
    tmp_storage.save("d1", "a", "fpa")
    tmp_storage.save("d1", "b", "fpb")
    tmp_storage.save("d2", "c", "fpc")
    assert len(tmp_storage.load_all("d1")) == 2
    assert len(tmp_storage.load_all("d2")) == 1
    assert tmp_storage.delete("d1", "a") == 1
    assert len(tmp_storage.load_all("d1")) == 1
    assert tmp_storage.delete("d2") == 1
    assert tmp_storage.load_all("d2") == []


def test_storage_context_manager(tmp_path) -> None:
    path = tmp_path / "ctx.sqlite3"
    with AdaptiveStorage(path) as s:
        s.save("d", "i", "fp")
        assert s.load("d", "i") is not None
    # After exit the connection is closed; the file still exists.
    assert path.exists()


def test_similarity_weighted_towards_text_and_tag() -> None:
    # Same tag + text but different attrs should still score high.
    a = compute_fingerprint(_el('<a id="1" class="c">Same</a>'))
    b = compute_fingerprint(_el('<a id="2" data-x="y">Same</a>'))
    assert similarity_score(a, b) > 0.7
