import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = "calibre_web_multisource_testpkg"


def install_calibre_web_stubs():
    flask_mod = types.ModuleType("flask")
    flask_mod.request = types.SimpleNamespace(host_url="http://localhost/")

    class Response:
        def __init__(self, response=None, status=None, mimetype=None):
            self.response = response
            self.status = status
            self.mimetype = mimetype

    flask_mod.Response = Response
    sys.modules["flask"] = flask_mod

    cps = sys.modules.setdefault("cps", types.ModuleType("cps"))

    logger_mod = types.ModuleType("cps.logger")
    logger_mod.create = lambda: types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    sys.modules["cps.logger"] = logger_mod
    cps.logger = logger_mod

    services_mod = sys.modules.setdefault("cps.services", types.ModuleType("cps.services"))
    metadata_mod = types.ModuleType("cps.services.Metadata")

    class Metadata:
        pass

    class MetaSourceInfo:
        def __init__(self, id=None, description=None, link=None):
            self.id = id
            self.description = description
            self.link = link

    class MetaRecord:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    metadata_mod.Metadata = Metadata
    metadata_mod.MetaSourceInfo = MetaSourceInfo
    metadata_mod.MetaRecord = MetaRecord
    sys.modules["cps.services.Metadata"] = metadata_mod
    services_mod.Metadata = metadata_mod

    search_metadata_mod = sys.modules.setdefault("cps.search_metadata", types.ModuleType("cps.search_metadata"))
    search_metadata_mod.meta = types.SimpleNamespace(route=lambda *a, **k: (lambda fn: fn))


def install_source_stubs():
    source_classes = {
        "source_douban": "DoubanSource",
        "source_nlc": "NLCSource",
        "source_dangdang": "DangdangSource",
        "source_weread": "WeReadSource",
    }
    for module_name, class_name in source_classes.items():
        full_name = f"{PKG}.{module_name}"
        mod = types.ModuleType(full_name)
        mod.__dict__[class_name] = type(class_name, (), {"SOURCE_NAME": class_name, "search": lambda self, query, is_isbn=False: []})
        sys.modules[full_name] = mod



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


install_calibre_web_stubs()
install_source_stubs()
book_record = load_module("book_record")
matcher_mod = load_module("matcher")
proxy_manager = load_module("proxy_manager")
source_openlibrary = load_module("source_openlibrary")
source_googlebooks = load_module("source_googlebooks")
multisource_mod = load_module("MultiSource")

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


class MultiSourceFallbackTests(unittest.TestCase):
    def test_search_returns_raw_records_when_merge_fails(self):
        record = BookRecord(
            source_id="openlibrary",
            source_name="OpenLibrary",
            title="Fallback Book",
            authors=["Alice"],
            publisher="Fallback Press",
            isbn="9787302123456",
        )
        source = types.SimpleNamespace(SOURCE_NAME="Fake Source", search=lambda query, is_isbn: [record])
        provider = multisource_mod.MultiSource.__new__(multisource_mod.MultiSource)
        provider.active = True
        provider.sources = [source]
        provider._cascade_sources = []
        provider.circuit_breaker = None
        provider.matcher = types.SimpleNamespace(merge=mock.Mock(side_effect=RuntimeError("merge failed")))

        with mock.patch.object(multisource_mod.proxy_manager, "get_proxies", return_value=None), \
             mock.patch.object(multisource_mod.proxy_manager, "get_current_proxy_info", return_value="direct"):
            results = provider.search("Fallback")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Fallback Book")
        self.assertEqual(results[0].publisher, "Fallback Press")
        self.assertEqual(results[0].source.description, "MultiSource (OpenLibrary)")


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
        proxy_manager.PROXY_DIAGNOSTIC_ENABLED = True
        try:
            with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
                 mock.patch("calibre_web_multisource_testpkg.proxy_manager.time.time", side_effect=[1.0, 1.1, 2.0, 3.0, 4.0, 5.0]):
                proxies, name = proxy_manager.probe_best_proxy(timeout=1)
        finally:
            proxy_manager.PROXY_DIAGNOSTIC_ENABLED = False

        self.assertIsNone(proxies)
        self.assertEqual(name, "direct")
        self.assertEqual(calls[0], None)

    def test_get_proxies_cache_ttl(self):
        """get_proxies() 在 60s 内返回缓存结果，过期后重新探测"""
        with mock.patch.object(proxy_manager, "_check_port", return_value=True), \
             mock.patch.object(proxy_manager.time, "time", side_effect=[100.0, 130.0, 170.0]):
            # 首次调用：探测端口
            result1 = proxy_manager.get_proxies()
            self.assertIsNotNone(result1)
            self.assertEqual(result1, {"http": "http://192.168.1.249:20172",
                                       "https": "http://192.168.1.249:20172"})
            # 30s 后：仍在缓存期内
            result2 = proxy_manager.get_proxies()
            self.assertEqual(result2, result1)
            # 70s 后：缓存过期，重新探测
            result3 = proxy_manager.get_proxies()
            self.assertEqual(result3, result1)
            # 重新探测到同一代理（因为 _check_port 总是 True）


class SourceProxyCacheTests(unittest.TestCase):
    def test_openlibrary_get_best_proxies_delegates_to_get_proxies(self):
        """OL._get_best_proxies() 直接委托给 proxy_manager.get_proxies()"""
        source = source_openlibrary.OpenLibrarySource()
        with mock.patch.object(source_openlibrary.proxy_manager, "get_proxies",
                               return_value={"http": "px", "https": "px"}):
            result = source._get_best_proxies()
            self.assertEqual(result, {"http": "px", "https": "px"})

    def test_googlebooks_get_best_proxies_delegates_to_get_proxies(self):
        """GB._get_best_proxies() 直接委托给 proxy_manager.get_proxies()"""
        source = source_googlebooks.GoogleBooksSource()
        with mock.patch.object(source_googlebooks.proxy_manager, "get_proxies",
                               return_value={"http": "px", "https": "px"}):
            result = source._get_best_proxies()
            self.assertEqual(result, {"http": "px", "https": "px"})


class CoverProxyTests(unittest.TestCase):
    def setUp(self):
        self._get_params = multisource_mod._get_cover_request_params
        self._is_external = multisource_mod._is_external_cover

    def test_external_cover_detection(self):
        """_is_external_cover() 正确识别外部封面域名"""
        self.assertTrue(self._is_external(
            "https://img2.doubanio.com/view/subject/l/public/s1234567.jpg"))
        self.assertTrue(self._is_external(
            "https://books.google.com/books/content?id=abc"))
        self.assertTrue(self._is_external(
            "https://covers.openlibrary.org/b/id/12345-L.jpg"))
        self.assertFalse(self._is_external(
            "https://example.com/cover.jpg"))
        self.assertFalse(self._is_external(""))

    @mock.patch("calibre_web_multisource_testpkg.MultiSource.os.path.exists")
    @mock.patch("builtins.open", new_callable=mock.mock_open,
                 read_data="dbcl2=abc123; bid=xyz")
    def test_cover_request_params_with_cookie_file(self, mock_open, mock_exists):
        """豆瓣封面 + douban_cookie.txt 存在时，返回 Cookie 和 Referer"""
        mock_exists.return_value = True
        headers, proxies = self._get_params(
            "https://img2.doubanio.com/view/subject/l/public/s1234567.jpg")
        self.assertEqual(proxies, {"http": None, "https": None})
        self.assertEqual(headers.get("Referer"), "https://book.douban.com/")
        self.assertEqual(headers.get("Cookie"), "dbcl2=abc123; bid=xyz")

    @mock.patch("calibre_web_multisource_testpkg.MultiSource.os.path.exists")
    def test_cover_request_params_without_cookie_file(self, mock_exists):
        """豆瓣封面 + douban_cookie.txt 不存在时，只有 Referer 不含 Cookie"""
        mock_exists.return_value = False
        headers, proxies = self._get_params(
            "https://img2.doubanio.com/view/subject/l/public/s1234567.jpg")
        self.assertEqual(proxies, {"http": None, "https": None})
        self.assertEqual(headers.get("Referer"), "https://book.douban.com/")
        self.assertNotIn("Cookie", headers)

    def test_cover_request_params_non_douban_url(self):
        """非豆瓣封面 URL 返回默认 headers（含 User-Agent, Referer）+ None proxies，不含 Cookie"""
        headers, proxies = self._get_params(
            "https://books.google.com/books/content?id=abc")
        self.assertIsNone(proxies)
        self.assertNotIn("Cookie", headers)
        self.assertIn("User-Agent", headers)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.cb = multisource_mod.source_health.CircuitBreaker(
            threshold=3, cooldown=300)

    def test_skip_after_consecutive_failures(self):
        """连续失败 3 次后 should_skip() 返回 True"""
        self.assertFalse(self.cb.should_skip("test_source"))
        self.cb.record_failure("test_source")
        self.cb.record_failure("test_source")
        self.assertFalse(self.cb.should_skip("test_source"))
        self.cb.record_failure("test_source")
        self.assertTrue(self.cb.should_skip("test_source"))

    def test_success_resets_failure_count(self):
        """失败后成功 → 计数归零"""
        self.cb.record_failure("test_source")
        self.cb.record_failure("test_source")
        self.cb.record_success("test_source")
        self.assertEqual(self.cb._failures["test_source"], 0)
        self.assertFalse(self.cb.should_skip("test_source"))

    @mock.patch.object(multisource_mod.source_health.time, "time")
    def test_cooldown_expires_allows_probe(self, mock_time):
        """熔断冷却到期后 → should_skip() 返回 False（half_open）"""
        mock_time.return_value = 100.0
        for _ in range(3):
            self.cb.record_failure("test_source")
        self.assertTrue(self.cb.should_skip("test_source"))
        # 冷却到期
        mock_time.return_value = 100.0 + 300.0 + 1.0
        self.assertFalse(self.cb.should_skip("test_source"))

    @mock.patch.object(multisource_mod.source_health.time, "time")
    def test_half_open_success_closes_circuit(self, mock_time):
        """half_open 探测成功 → 状态回到 closed"""
        mock_time.return_value = 100.0
        for _ in range(3):
            self.cb.record_failure("test_source")
        mock_time.return_value = 100.0 + 300.0 + 1.0
        self.assertFalse(self.cb.should_skip("test_source"))  # half_open
        self.cb.record_success("test_source")
        self.assertFalse(self.cb.should_skip("test_source"))
        self.assertEqual(self.cb._failures["test_source"], 0)

    @mock.patch.object(multisource_mod.source_health.time, "time")
    def test_half_open_failure_reopens_circuit(self, mock_time):
        """half_open 探测失败 → 再次熔断"""
        mock_time.return_value = 100.0
        for _ in range(3):
            self.cb.record_failure("test_source")
        mock_time.return_value = 100.0 + 300.0 + 1.0
        self.assertFalse(self.cb.should_skip("test_source"))  # half_open
        opened = self.cb.record_failure("test_source")  # 再次失败
        self.assertTrue(opened)
        self.assertTrue(self.cb.should_skip("test_source"))


class SearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.provider = multisource_mod.MultiSource.__new__(
            multisource_mod.MultiSource)
        self.provider.active = True
        self.provider._cascade_sources = []
        self.provider.circuit_breaker = None
        self._budget = multisource_mod.SEARCH_BUDGET_SECONDS

    def tearDown(self):
        multisource_mod.SEARCH_BUDGET_SECONDS = self._budget

    def test_fast_source_completes_within_budget(self):
        """快源在预算内完成，慢源被跳过，总耗时不超过预算+容忍值"""
        import time as _time

        def fast_search(query, is_isbn):
            _time.sleep(0.1)
            return [BookRecord(source_id="fast", source_name="Fast",
                               title="Fast Book", isbn="9787302123456")]

        def slow_search(query, is_isbn):
            _time.sleep(10.0)
            return [BookRecord(source_id="slow", source_name="Slow",
                               title="Slow Book")]

        fast_source = types.SimpleNamespace(
            SOURCE_NAME="Fast", SOURCE_ID="fast", search=fast_search)
        slow_source = types.SimpleNamespace(
            SOURCE_NAME="Slow", SOURCE_ID="slow", search=slow_search)

        self.provider.sources = [fast_source, slow_source]
        multisource_mod.SEARCH_BUDGET_SECONDS = 2

        t0 = _time.time()
        records = self.provider._query_all_sources(
            "test", False, "abc12345",
            sources=[fast_source, slow_source])
        elapsed = _time.time() - t0

        # 快源结果正常返回
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "Fast Book")
        # 总耗时受预算约束（+1s 容忍值覆盖调度开销）
        self.assertLess(elapsed, multisource_mod.SEARCH_BUDGET_SECONDS + 1.0)

    def test_all_sources_complete_under_budget(self):
        """所有源都在预算内完成，结果完整"""
        import time as _time

        def ok_search(query, is_isbn):
            _time.sleep(0.1)
            return [BookRecord(source_id="x", source_name="X",
                               title="Book", isbn="9787302123456")]

        src1 = types.SimpleNamespace(
            SOURCE_NAME="S1", SOURCE_ID="s1", search=ok_search)
        src2 = types.SimpleNamespace(
            SOURCE_NAME="S2", SOURCE_ID="s2", search=ok_search)

        self.provider.sources = [src1, src2]
        multisource_mod.SEARCH_BUDGET_SECONDS = 5

        records = self.provider._query_all_sources(
            "test", False, "abc12345", sources=[src1, src2])
        self.assertEqual(len(records), 2)

    def test_empty_result_when_all_sources_timeout(self):
        """预算耗尽时所有源都没返回，返回空，总耗时不超过预算+容忍值"""
        import time as _time

        def slow_search(query, is_isbn):
            _time.sleep(10.0)
            return []

        src = types.SimpleNamespace(
            SOURCE_NAME="Slow", SOURCE_ID="slow", search=slow_search)

        self.provider.sources = [src]
        multisource_mod.SEARCH_BUDGET_SECONDS = 1

        t0 = _time.time()
        records = self.provider._query_all_sources(
            "test", False, "abc12345", sources=[src])
        elapsed = _time.time() - t0

        self.assertEqual(len(records), 0)
        self.assertLess(elapsed, multisource_mod.SEARCH_BUDGET_SECONDS + 1.0)


class SourceTimeoutHealthTests(unittest.TestCase):
    """验证源 timeout 常量与搜索预算的关系。
    各源内部 timeout 作为二级安全网，应 >= SEARCH_BUDGET_SECONDS，
    否则源将在预算之前先超时，造成不必要的失败。"""

    # 已知的源 timeout 常量名 → 值（手动维护，与源文件保持同步）
    KNOWN_SOURCE_TIMEOUTS = {
        "source_douban": {"DOUBAN_TIMEOUT": 8},
        "source_dangdang": {"DANGDANG_TIMEOUT": 10},
        "source_googlebooks": {"GB_TIMEOUT": 8},
        "source_openlibrary": {"OL_TIMEOUT": 10},
        "source_weread": {"WEREAD_TIMEOUT": 8},
    }

    def test_source_timeouts_not_less_than_budget(self):
        """所有已知源内部 timeout 应 >= 搜索预算"""
        budget = multisource_mod.SEARCH_BUDGET_SECONDS
        for source, constants in self.KNOWN_SOURCE_TIMEOUTS.items():
            for name, value in constants.items():
                with self.subTest(source=source, constant=name):
                    self.assertGreaterEqual(
                        value, budget,
                        f"{source}.{name}={value} < SEARCH_BUDGET={budget} — "
                        f"源内部超时不应低于预算值，否则会在预算前先触发超时"
                    )

    def test_source_timeout_no_longer_dead_code(self):
        """验证 SOURCE_TIMEOUT 死代码已清除"""
        self.assertFalse(
            hasattr(multisource_mod, "SOURCE_TIMEOUT"),
            "SOURCE_TIMEOUT 是死代码，已在 P1 中删除。"
            "如需要单源超时，应修改各源自己的 timeout 常量。"
        )


if __name__ == "__main__":
    unittest.main()
