import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = "calibre_web_multisource_testpkg"


def load_module(name: str):
    package = sys.modules.get(PKG)
    if package is None:
        package = types.ModuleType(PKG)
        package.__path__ = [str(ROOT)]
        sys.modules[PKG] = package

    full_name = f"{PKG}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    path = ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


book_record = load_module("book_record")
matcher_mod = load_module("matcher")
proxy_manager = load_module("proxy_manager")
source_openlibrary = load_module("source_openlibrary")
source_googlebooks = load_module("source_googlebooks")

BookRecord = book_record.BookRecord
MergedBook = book_record.MergedBook
normalize_date = book_record.normalize_date
normalize_text = book_record.normalize_text
canonical_isbn = book_record.canonical_isbn
BookMatcher = matcher_mod.BookMatcher
clean_author_name = matcher_mod.clean_author_name
levenshtein_ratio = matcher_mod.levenshtein_ratio


class BookRecordTests(unittest.TestCase):
    def test_normalize_date(self):
        self.assertEqual(normalize_date("2024"), "2024-01-01")
        self.assertEqual(normalize_date("2024-3"), "2024-03-01")
        self.assertEqual(normalize_date("2024.3"), "2024-03-01")
        self.assertEqual(normalize_date("2024-03-18"), "2024-03-18")

    def test_normalize_text(self):
        self.assertEqual(normalize_text("ＡＢＣ 书 名！"), "abc 书 名")
        self.assertEqual(normalize_text(" Hello, World "), "hello world")

    def test_canonical_isbn(self):
        self.assertEqual(canonical_isbn("978-7-302-12345-6"), "9787302123456")
        self.assertEqual(canonical_isbn("7-302-12345-5"), "9787302123453")
        self.assertEqual(canonical_isbn("bad"), "")

    def test_fingerprint_changes_with_core_fields(self):
        r1 = BookRecord(source_id="douban", source_name="豆瓣", title="Python 入门", authors=["张三"], publisher="人民邮电")
        r2 = BookRecord(source_id="douban", source_name="豆瓣", title="Python 入门", authors=["张三"], publisher="人民邮电")
        r3 = BookRecord(source_id="douban", source_name="豆瓣", title="Python 进阶", authors=["张三"], publisher="人民邮电")
        self.assertEqual(r1.compute_fingerprint(), r2.compute_fingerprint())
        self.assertNotEqual(r1.compute_fingerprint(), r3.compute_fingerprint())


class MatcherTests(unittest.TestCase):
    def test_clean_author_name(self):
        self.assertEqual(clean_author_name("张三 著"), "张三")
        self.assertEqual(clean_author_name("李四,编"), "李四")

    def test_levenshtein_ratio(self):
        self.assertEqual(levenshtein_ratio("abc", "abc"), 1.0)
        self.assertLess(levenshtein_ratio("abc", "abd"), 1.0)
        self.assertEqual(levenshtein_ratio("", ""), 1.0)

    def test_merge_prefers_high_priority_fields(self):
        records = [
            BookRecord(
                source_id="openlibrary",
                source_name="OpenLibrary",
                title="Python Basics",
                authors=["Alice 著"],
                publisher="OL Press",
                description="short",
                cover_url="https://example.com/ol.jpg",
                isbn="9787302123456",
                identifiers={"ol": "1"},
            ),
            BookRecord(
                source_id="douban",
                source_name="豆瓣",
                title="Python Basics",
                authors=["Alice 著", "Bob 编"],
                publisher="DB Press",
                description="a much longer douban description",
                cover_url="https://example.com/db.jpg",
                rating=4.2,
                tags=["编程", "Python"],
                isbn="9787302123456",
                identifiers={"db": "2"},
            ),
        ]
        merged = BookMatcher().merge(records)
        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item.title, "Python Basics")
        self.assertEqual(item.publisher, "DB Press")
        self.assertEqual(item.description, "a much longer douban description")
        self.assertEqual(item.cover_url, "https://example.com/db.jpg")
        self.assertEqual(item.rating, 4.2)
        self.assertEqual(item.isbn, "9787302123456")
        self.assertEqual(item.identifiers["isbn"], "9787302123456")
        self.assertEqual(item.sources, ["豆瓣", "OpenLibrary"])
        self.assertEqual(item.field_sources["description"], "豆瓣")

    def test_merge_has_stable_source_and_cover_priority(self):
        records = [
            BookRecord(
                source_id="googlebooks",
                source_name="Google Books",
                title="Same Book",
                authors=["Alice"],
                publisher="GB Press",
                cover_url="https://example.com/google.jpg",
                isbn="9787302123456",
            ),
            BookRecord(
                source_id="weread",
                source_name="微信读书",
                title="Same Book",
                authors=["Alice"],
                publisher="WR Press",
                cover_url="https://example.com/weread.jpg",
                isbn="9787302123456",
            ),
            BookRecord(
                source_id="openlibrary",
                source_name="OpenLibrary",
                title="Same Book",
                authors=["Alice"],
                publisher="OL Press",
                cover_url="https://example.com/openlibrary.jpg",
                isbn="9787302123456",
            ),
        ]
        item = BookMatcher().merge(records)[0]
        self.assertEqual(item.publisher, "WR Press")
        self.assertEqual(item.cover_url, "https://example.com/weread.jpg")
        self.assertEqual(item.sources, ["微信读书", "OpenLibrary", "Google Books"])

    def test_single_book_keeps_book(self):
        record = BookRecord(source_id="douban", source_name="豆瓣", title="Only One", isbn="9787302123456")
        merged = BookMatcher().merge([record])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].confidence, "high")


class ProxyManagerTests(unittest.TestCase):
    def test_probe_best_proxy_prefers_direct_when_fast_enough(self):
        class Resp:
            def __init__(self, status_code):
                self.status_code = status_code

        calls = []

        def fake_get(url, params=None, headers=None, proxies=None, verify=None, timeout=None):
            calls.append(proxies)
            return Resp(200)

        fake_requests = types.SimpleNamespace(get=mock.Mock(side_effect=fake_get))
        with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
             mock.patch("calibre_web_multisource_testpkg.proxy_manager.time.time", side_effect=[1.0, 1.1, 2.0, 3.0, 4.0, 5.0]):
            proxies, name = proxy_manager.probe_best_proxy(timeout=1)

        self.assertIsNone(proxies)
        self.assertEqual(name, "direct")
        self.assertEqual(calls[0], None)


class SourceProxyCacheTests(unittest.TestCase):
    def test_openlibrary_proxy_cache_has_ttl(self):
        source = source_openlibrary.OpenLibrarySource()
        with mock.patch.object(source_openlibrary, "probe_best_proxy", side_effect=[({"http": "first"}, "first"), ({"http": "second"}, "second")]), \
             mock.patch.object(source_openlibrary.time, "time", side_effect=[100.0, 120.0, 170.1]):
            self.assertEqual(source._get_best_proxies(), {"http": "first"})
            self.assertEqual(source._get_best_proxies(), {"http": "first"})
            self.assertEqual(source._get_best_proxies(), {"http": "second"})

    def test_googlebooks_proxy_cache_has_ttl(self):
        source = source_googlebooks.GoogleBooksSource()
        with mock.patch.object(source_googlebooks, "probe_best_proxy", side_effect=[({"http": "first"}, "first"), ({"http": "second"}, "second")]), \
             mock.patch.object(source_googlebooks.time, "time", side_effect=[100.0, 120.0, 170.1]):
            self.assertEqual(source._get_best_proxies(), {"http": "first"})
            self.assertEqual(source._get_best_proxies(), {"http": "first"})
            self.assertEqual(source._get_best_proxies(), {"http": "second"})


if __name__ == "__main__":
    unittest.main()
