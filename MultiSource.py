"""
MultiSource — 多源聚合书籍元数据插件

整合多个数据源（豆瓣、当当、微信读书、Open Library、Google Books，NLC 可选），通过三层防错机制
（ISBN 精确匹配 → 复合指纹匹配 → 模糊匹配打分）智能合并结果。

特性:
  - 豆瓣翻页：自动拉取多页结果，支持上下翻页
  - 字段级溯源：每个字段标注来自哪个数据源
  - 可配置：源开关、翻页条数、超时均可调整
  - 模块化：每个数据源独立文件，修改互不影响
"""
import os
import re
import time
import urllib.parse
import urllib.request
import ssl
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
from typing import List

import requests
from flask import request, Response

from cps.services.Metadata import Metadata, MetaSourceInfo, MetaRecord

# 延迟导入 helper：避免插件加载阶段因 Calibre-Web 初始化顺序问题失败
helper = None

try:
    from cps import logger
    log = logger.create()
except Exception:
    import logging
    log = logging.getLogger("MultiSource")

def _get_helper():
    global helper
    if helper is None:
        from cps import helper as _helper
        helper = _helper
    return helper

from . import proxy_manager
from .book_record import BookRecord, MergedBook, canonical_isbn, normalize_date
from .matcher import BookMatcher
from .source_douban import DoubanSource
from .source_nlc import NLCSource
from .source_openlibrary import OpenLibrarySource
from .source_dangdang import DangdangSource
from .source_health import CircuitBreaker
from . import source_health

# WeRead 可选导入
try:
    from .source_weread import WeReadSource
    _HAS_WEREAD = True
except ImportError:
    _HAS_WEREAD = False

# Google Books 可选导入（NAS 网络可能不可达，导入失败时跳过）
try:
    from .source_googlebooks import GoogleBooksSource
    _HAS_GOOGLE_BOOKS = True
except ImportError:
    _HAS_GOOGLE_BOOKS = False


def _sanitize_author(name: str) -> str:
    """Clean author name: dots -> spaces, merge spaces"""
    if not name:
        return name
    name = name.replace(".", " ")
    name = re.sub(r" +", " ", name)
    return name.strip()


# ============================================================
# 配置
# ============================================================
PROVIDER_NAME = "MultiSource"
PROVIDER_ID = "multisource"

# 源开关：设为 False 可禁用某个数据源
# 注意：NLC (国家图书馆) 和 OpenLibrary 从容器内访问经常超时/不可达，默认关闭
SOURCE_DOUBAN_ENABLED = True
SOURCE_NLC_ENABLED = False  # NLC 从 NAS 容器内不可达
SOURCE_OPENLIBRARY_ENABLED = True
SOURCE_DANGDANG_ENABLED = True
SOURCE_WEREAD_ENABLED = True     # 微信读书（需 weread_apikey.txt）

# ISBN 级联：从书名搜索结果中提取 ISBN，再精确查询其他源
CASCADE_ENABLED = True       # 是否启用 ISBN 级联
CASCADE_OPENLIBRARY = True   # 级联查询 OpenLibrary ISBN
CASCADE_GOOGLE_BOOKS = True  # 级联查询 Google Books（需网络可达）
GOOGLE_BOOKS_AS_SOURCE = True   # Google Books 是否作为常规源参与书名搜索
CASCADE_TIMEOUT = 5          # ISBN 级联超时（秒）
CASCADE_LIMIT_GOOGLE = 10  # Google Books 单次级联最多 ISBN 数
CASCADE_MAX_RECORDS = 3      # Phase 5: 只级联 top N 个 ISBN 候选

# Phase 1: 搜索预算
SEARCH_BUDGET_SECONDS = 6      # Phase1 全局等待上限（秒）
FAST_RESULT_MIN_COUNT = 5      # 结果足够时可提前进入 merge
SOURCE_TIMEOUT = 4             # 单源超时（秒）
SOURCE_RETRY_ENABLED = False   # 禁用自动重试，避免单源消耗 4+1+4=9s 超出预算

# Phase 2: 源分类与选择 — 中文搜索跳过对中文支持差/慢的外部源
ZH_SKIP_OPENLIBRARY = True     # 中文书名搜索跳过 OpenLibrary（总是 0 结果，耗时 4-31s）
ZH_SKIP_GOOGLEBOOKS = True     # 中文书名搜索跳过 Google Books（中文覆盖有限，耗时 2-9s）

# Phase 4: 源熔断器 — 连续失败自动跳过，避免每次都等到超时
SOURCE_CIRCUIT_BREAKER_ENABLED = True

# 封面代理（解决豆瓣防盗链）
PROXY_DOUBAN_COVER = True
DOUBAN_COVER_PROXY_HOST = ""  # 空 = 自动使��当前 host
DOUBAN_COVER_PROXY_PATH = "metadata/douban_cover?cover="
DOUBAN_COVER_DOMAIN = "doubanio.com"
EXTERNAL_COVER_DOMAINS = {"doubanio.com", "books.google.com", "covers.openlibrary.org"}  # 封面需要走代理的域名



DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://book.douban.com/",
}


# ============================================================
# 主插件类
# ============================================================
class MultiSource(Metadata):
    __name__ = PROVIDER_NAME
    __id__ = PROVIDER_ID

    def __init__(self):
        self.active = True
        self.matcher = BookMatcher()

        # 初始化启用的源
        self.sources = []
        if SOURCE_DOUBAN_ENABLED:
            self.sources.append(DoubanSource())
        if SOURCE_NLC_ENABLED:
            self.sources.append(NLCSource())
        if SOURCE_OPENLIBRARY_ENABLED:
            self.sources.append(OpenLibrarySource())
        if SOURCE_DANGDANG_ENABLED:
            self.sources.append(DangdangSource())
        if SOURCE_WEREAD_ENABLED and _HAS_WEREAD:
            self.sources.append(WeReadSource())
        if GOOGLE_BOOKS_AS_SOURCE and _HAS_GOOGLE_BOOKS:
            self.sources.append(GoogleBooksSource())

        # ISBN 级联源：仅在第二阶段用 ISBN 精确查询
        self._cascade_sources = []
        if CASCADE_ENABLED:
            if CASCADE_OPENLIBRARY:
                self._cascade_sources.append(OpenLibrarySource())
            if CASCADE_GOOGLE_BOOKS and _HAS_GOOGLE_BOOKS:
                self._cascade_sources.append(GoogleBooksSource())

        # 安装封面代理
        self._hack_cover_proxy()

        # Phase 4: 源熔断器
        self.circuit_breaker = CircuitBreaker() if SOURCE_CIRCUIT_BREAKER_ENABLED else None

        super().__init__()
        log.info(f"[MultiSource] 初始化完成，启用的源: "
                 f"{[s.SOURCE_NAME for s in self.sources]}")

    def search(self, query: str, generic_cover: str = "",
               locale: str = "en") -> List[MetaRecord]:
        """
        Calibre-Web 插件标准接口。
        返回 MetaRecord 列表供 UI 展示。

        两阶段搜索：
          阶段1: 书名搜索 → 所有源并行搜索
          阶段2: ISBN 级联 → 用收集到的 ISBN 并行查询级联源
        """
        if not self.active or not self.sources:
            return []

        query = query.strip()
        if not query:
            return []

        request_id = uuid.uuid4().hex[:8]
        search_start = time.time()

        try:
            # 判断是否为 ISBN 搜索
            is_isbn = self._is_isbn_query(query)
            query_type = self._classify_query(query, is_isbn)

            # ---- 代理预热：提前检测链路，缓存结果 ----
            _proxies = proxy_manager.get_proxies()
            _px_info = proxy_manager.get_current_proxy_info()

            # ---- Phase 2: 根据 query 类型选择源 ----
            phase1_sources = self.sources
            skipped_names = []
            if query_type == "zh":
                if ZH_SKIP_OPENLIBRARY:
                    phase1_sources = [s for s in phase1_sources
                                      if s.SOURCE_ID != "openlibrary"]
                    skipped_names.append("OpenLibrary")
                if ZH_SKIP_GOOGLEBOOKS:
                    phase1_sources = [s for s in phase1_sources
                                      if s.SOURCE_ID != "googlebooks"]
                    skipped_names.append("Google Books")
                if skipped_names:
                    log.info(f"[MultiSource][{request_id}] 中文搜索跳过: {', '.join(skipped_names)}")

            # ---- Phase 4: 熔断器过滤 ----
            if self.circuit_breaker:
                cb_skipped = []
                filtered = []
                for s in phase1_sources:
                    if self.circuit_breaker.should_skip(s.SOURCE_ID):
                        cb_skipped.append(s.SOURCE_NAME)
                    else:
                        filtered.append(s)
                if cb_skipped:
                    log.info(f"[MultiSource][{request_id}] 熔断器跳过: {cb_skipped}")
                phase1_sources = filtered

            log.info(f"[MultiSource][{request_id}] query='{query}'"
                     f" type={query_type} start 代理={_px_info}"
                     f" sources={[s.SOURCE_NAME for s in phase1_sources]}")

            # ---- 阶段1: 并行查询所有源 ----
            all_records = self._query_all_sources(query, is_isbn, request_id,
                                                  sources=phase1_sources)

            if not all_records:
                log.info(f"[MultiSource][{request_id}] 所有源返回空结果")
                search_total = time.time() - search_start
                log.info(f"[MultiSource][{request_id}] done total={search_total:.2f}s final=0")
                return []

            # ---- 阶段2: ISBN 级联（仅非 ISBN 搜索时执行）----
            phase2_records = []
            cascade_start = time.time()
            if not is_isbn and self._cascade_sources:
                isbns = self._extract_isbns(all_records)
                if isbns:
                    limited_isbns = isbns[:CASCADE_MAX_RECORDS]
                    log.info(f"[MultiSource][{request_id}] cascade start"
                             f" candidates={len(isbns)}"
                             f" -> limited={len(limited_isbns)}")
                    phase2_records = self._query_isbn_cascade(limited_isbns, request_id)
                    if phase2_records:
                        log.info(f"[MultiSource][{request_id}] cascade done"
                                 f" new={len(phase2_records)}")
                        all_records.extend(phase2_records)
            cascade_total = time.time() - cascade_start

            # 去重合并
            merge_start = time.time()
            try:
                merged = self.matcher.merge(all_records)
                merge_total = time.time() - merge_start
                log.info(f"[MultiSource][{request_id}] merge done"
                         f" duration={merge_total:.2f}s merged={len(merged)}")
            except Exception as merge_error:
                merge_total = time.time() - merge_start
                log.error(f"[MultiSource][{request_id}] merge failed"
                          f" duration={merge_total:.2f}s error={merge_error}",
                          exc_info=True)
                merged = []

            # 转换为 MetaRecord 列表；如果合并结果为空，至少返回原始记录的单条视图，避免整单空结果
            search_total = time.time() - search_start
            if merged:
                results = self._to_meta_records(merged)
                log.info(f"[MultiSource][{request_id}] done"
                         f" total={search_total:.2f}s final={len(results)}"
                         f" cascade={cascade_total:.2f}s merge={merge_total:.2f}s")
                return results

            log.warning(f"[MultiSource][{request_id}] 合并结果为空，回退到原始记录直出")
            results = self._to_meta_records([self._single_book(r) for r in all_records])
            log.info(f"[MultiSource][{request_id}] done"
                     f" total={search_total:.2f}s final={len(results)}"
                     f" cascade={cascade_total:.2f}s merge={merge_total:.2f}s (fallback)")
            return results

        except Exception as e:
            search_total = time.time() - search_start
            log.error(f"[MultiSource][{request_id}] search error"
                      f" total={search_total:.2f}s error={e}", exc_info=True)
            return []
    # ---- 内部方法 ----

    @staticmethod
    def _is_isbn_query(query: str) -> bool:
        """判断是否为 ISBN 搜索"""
        isbn = canonical_isbn(query)
        return len(isbn) in (10, 13)

    @staticmethod
    def _classify_query(query: str, is_isbn: bool) -> str:
        """分类查询类型：isbn / zh / en"""
        if is_isbn:
            return "isbn"
        if any('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' for c in query):
            return "zh"
        return "en"

    def _query_all_sources(self, query: str, is_isbn: bool, request_id: str,
                           sources: List = None) -> List[BookRecord]:
        """并行查询所有启用的数据源。SEARCH_BUDGET_SECONDS 内完成的源正常合并，超时源跳过。"""
        if sources is None:
            sources = self.sources
        all_records = []
        phase1_start = time.time()

        pool = ThreadPoolExecutor(max_workers=len(sources))
        futures = {
            pool.submit(self._query_source, source, query, is_isbn, request_id): source
            for source in sources
        }

        try:
            # Phase 1: 用 wait() 替代 as_completed()，全局超时硬限制
            done, not_done = wait(futures.keys(), timeout=SEARCH_BUDGET_SECONDS,
                                  return_when=ALL_COMPLETED)

            for future in done:
                source = futures[future]
                try:
                    records, elapsed, retries, status = future.result(timeout=0)
                    source_count = sum(
                        1 for r in records
                        if "__page_marker__" not in r.tags
                    )
                    log.info(f"[MultiSource][{request_id}] source={source.SOURCE_NAME}"
                             f" status={status} duration={elapsed:.2f}s"
                             f" count={source_count} retry={retries}")
                    if records:
                        all_records.extend(records)
                    # Phase 4: 记录成功/失败到熔断器
                    if self.circuit_breaker:
                        if status == "ok":
                            self.circuit_breaker.record_success(source.SOURCE_ID)
                        else:
                            opened = self.circuit_breaker.record_failure(source.SOURCE_ID)
                            if opened:
                                log.warning(f"[MultiSource] {source.SOURCE_NAME}"
                                            f" 熔断器打开 — 连续失败 {source_health.SOURCE_FAILURE_THRESHOLD} 次"
                                            f"，跳过 {source_health.SOURCE_COOLDOWN_SECONDS}s")
                except Exception as e:
                    log.error(f"[MultiSource][{request_id}] source={source.SOURCE_NAME}"
                              f" status=error error={e}")
                    # Phase 4: 异常也计入失败
                    if self.circuit_breaker:
                        opened = self.circuit_breaker.record_failure(source.SOURCE_ID)
                        if opened:
                            log.warning(f"[MultiSource] {source.SOURCE_NAME}"
                                        f" 熔断器打开 — 连续失败 {source_health.SOURCE_FAILURE_THRESHOLD} 次")

            # 日志记录因预算耗尽被跳过的源
            unused = []
            for future in not_done:
                source = futures[future]
                unused.append(futures[future].SOURCE_NAME)
            if unused:
                log.warning(f"[MultiSource][{request_id}] 搜索预算耗尽"
                            f"({SEARCH_BUDGET_SECONDS}s)，跳过: {unused}")
        finally:
            # shutdown(wait=False): 不等待 pending futures，避免搜索预算被绕过
            pool.shutdown(wait=False)

        phase1_total = time.time() - phase1_start
        early = len(all_records) >= FAST_RESULT_MIN_COUNT
        log.info(f"[MultiSource][{request_id}] phase1 done"
                 f" duration={phase1_total:.2f}s total_records={len(all_records)}"
                 f" early_merge={early}")
        return all_records

    @staticmethod
    def _query_source(source, query: str, is_isbn: bool, request_id: str):
        """查询单个源。
        SOURCE_RETRY_ENABLED=True 时超时/连接类错误自动重试一次。
        返回 (records, elapsed, retry_count, status)
        status: 'ok' | 'timeout' | 'error'
        """
        start = time.time()
        max_attempts = 2 if SOURCE_RETRY_ENABLED else 1
        for attempt in range(max_attempts):
            try:
                records = source.search(query, is_isbn)
                return records, time.time() - start, attempt, "ok"
            except Exception as e:
                err = str(e).lower()
                is_timeout = "timeout" in err or "timed out" in err
                transient = is_timeout or any(kw in err for kw in (
                    "connection", "ssl", "reset", "refused", "eof",
                ))
                status = "timeout" if is_timeout else "error"
                if attempt < max_attempts - 1 and transient:
                    log.warning(
                        f"[MultiSource][{request_id}] {source.SOURCE_NAME}"
                        f" 失败({e})，重试..."
                    )
                    time.sleep(1)
                    continue
                return [], time.time() - start, attempt, status
        return [], time.time() - start, max_attempts - 1, "error"

    @staticmethod
    def _extract_isbns(records: List[BookRecord]) -> List[str]:
        """从阶段1结果中提取去重的有效 ISBN-13"""
        seen = set()
        isbns = []
        for r in records:
            isbn = canonical_isbn(r.isbn) if r.isbn else ""
            if isbn and isbn not in seen:
                seen.add(isbn)
                isbns.append(isbn)
        return isbns

    def _query_isbn_cascade(self, isbns: List[str], request_id: str) -> List[BookRecord]:
        """用 ISBN 查询级联源：OpenLibrary 批量、Google Books 限流"""
        all_records = []
        started_at = {}

        pool = ThreadPoolExecutor(max_workers=len(self._cascade_sources) + 5)
        futures = {}
        for source in self._cascade_sources:
            if hasattr(source, 'search_by_isbns'):
                future = pool.submit(source.search_by_isbns, isbns)
                futures[future] = source.SOURCE_NAME
                started_at[future] = time.time()
            else:
                limited = isbns[:CASCADE_LIMIT_GOOGLE]
                for isbn in limited:
                    future = pool.submit(source.search, isbn, True)
                    futures[future] = f"{source.SOURCE_NAME}({isbn})"
                    started_at[future] = time.time()

        try:
            done, not_done = wait(futures.keys(), timeout=CASCADE_TIMEOUT,
                                   return_when=ALL_COMPLETED)
            for future in done:
                label = futures[future]
                try:
                    records = future.result(timeout=0)
                    elapsed = time.time() - started_at.get(future, time.time())
                    log.info(f"[MultiSource][{request_id}] {label}: {len(records)} 条，耗时 {elapsed:.2f}s")
                    if records:
                        all_records.extend(records)
                except Exception as e:
                    log.error(f"[MultiSource][{request_id}] {label} 级联失败: {e}")
            for future in not_done:
                label = futures[future]
                log.warning(f"[MultiSource][{request_id}] {label} 级联超时({CASCADE_TIMEOUT}s)，跳过")
        finally:
            pool.shutdown(wait=False)

        return all_records

    @staticmethod
    def _single_book(record: BookRecord) -> MergedBook:
        """将单个源记录包装成 MergedBook，用于合并失败或合并为空时的直出兜底。"""
        isbn = record.get_normalized_isbn()
        identifiers = dict(record.identifiers)
        if isbn:
            identifiers["isbn"] = isbn
        return MergedBook(
            title=record.title,
            subtitle=record.subtitle,
            authors=record.authors,
            translators=record.translators,
            publisher=record.publisher,
            published_date=record.published_date,
            isbn=isbn,
            description=record.description,
            cover_url=record.cover_url,
            rating=record.rating,
            tags=record.tags,
            series=record.series,
            language=record.language,
            pages=record.pages,
            clc_code=record.clc_code,
            url=record.url,
            identifiers=identifiers,
            series_index=record.series_index,
            sources=[record.source_name],
            confidence="high",
            field_sources={
                "title": record.source_name,
                "authors": record.source_name,
                "publisher": record.source_name,
                "description": record.source_name,
                "cover_url": record.source_name,
                "isbn": record.source_name,
            },
            source_records=[record],
        )

    def _to_meta_records(self, merged_books: List[MergedBook]) -> List[MetaRecord]:
        """将 MergedBook 列表转换为 Calibre-Web 的 MetaRecord 列表"""
        results = []

        for book in merged_books:
            try:
                # 构建来源信息
                source_names = " + ".join(book.sources)
                if book.confidence == "medium":
                    source_names += " [低置信]"
                elif book.confidence == "low":
                    source_names += " [不匹配]"

                source_info = MetaSourceInfo(
                    id=PROVIDER_ID,
                    description=f"{PROVIDER_NAME} ({source_names})",
                    link=book.url or "https://book.douban.com/",
                )

                # 构建标识符
                identifiers = dict(book.identifiers)
                if book.isbn:
                    identifiers["isbn"] = book.isbn
                if book.series:
                    identifiers["series"] = book.series
                if book.series_index:
                    try:
                        identifiers["series_index"] = int(float(book.series_index))
                    except (ValueError, TypeError):
                        pass

                # 合并 note 到标题
                title = _sanitize_author(book.title)
                if book.subtitle:
                    title = f"{title}: {book.subtitle}"
                # 低置信标记：仅在多源合并且置信度低时加提示
                if book.confidence == "medium" and book.merge_note and len(book.sources) > 1:
                    title = f"[?] {title}"

                # 构建标签
                tags = list(book.tags)
                if book.publisher:
                    tags.append(book.publisher)
                if book.language:
                    tags.append(book.language)
                if book.series:
                    tags.append(book.series)

                # 去重
                tags = self._dedup_tags(tags)

                # 处理封面
                cover = book.cover_url
                if cover and PROXY_DOUBAN_COVER and _is_external_cover(cover):
                    pass  # cover 代理在 save 时处理

                record_id = book.isbn or str(hash(book.title))[:16]

                # 创建 MetaRecord
                meta = MultiSourceMetaRecord(
                    id=record_id,
                    title=title,
                    authors=[_sanitize_author(a) for a in book.authors],
                    publisher=book.publisher,
                    description=book.description,
                    url=book.url,
                    source=source_info,
                    identifiers=identifiers,
                    tags=tags,
                    cover=cover,
                    rating=int(round(book.rating)) if book.rating else 0,
                    publishedDate=normalize_date(book.published_date),
                    series=book.series,
                )

                results.append(meta)

            except Exception as e:
                log.error(f"[MultiSource] 记录转换失败 ({getattr(book, 'title', '?')}): {e}")
                continue
        return results

    @staticmethod
    def _dedup_tags(tags: List[str]) -> List[str]:
        """标签去重（大小写不敏感）"""
        seen = set()
        result = []
        for t in tags:
            if not t or not t.strip():
                continue
            lower = t.strip().casefold()
            if lower not in seen:
                seen.add(lower)
                result.append(t.strip())
        return result

    # ---- 封面代理 ----
    def _hack_cover_proxy(self):
        """安装封面代理以解决豆瓣防盗链。豆瓣封面直连 + 带 Cookie/Referer。"""
        try:
            h = _get_helper()
            save_cover = h.save_cover_from_url

            def new_save_cover(url, book_path):
                nonlocal save_cover
                if _is_external_cover(url):
                    if PROXY_DOUBAN_COVER:
                        parsed = urllib.parse.urlparse(url)
                        qs = urllib.parse.parse_qs(parsed.query)
                        cover_url = urllib.parse.unquote(qs.get("cover", [url])[0])
                    else:
                        cover_url = url
                    headers, proxies = _get_cover_request_params(cover_url)
                    resp = requests.get(cover_url, headers=headers,
                                       timeout=15, proxies=proxies)
                    return h.save_cover(resp, book_path)
                return save_cover(url, book_path)

            h.save_cover_from_url = new_save_cover
        except Exception as e:
            log.error(f"[MultiSource] 封面代理安装失败（可忽略）: {e}")


def _is_external_cover(url):
    return any(d in url for d in EXTERNAL_COVER_DOMAINS)


def _get_cover_request_params(cover_url: str):
    """获取封面下载的 headers 和 proxies。
    豆瓣封面: 直连(不走全局代理) + Cookie + Referer（参考 fugary/calibre-douban）。
    其他外部封面: 使用容器默认代理。
    """
    headers = dict(DEFAULT_HEADERS)
    proxies = None  # None = 使用容器默认代理

    if "doubanio.com" in cover_url:
        proxies = {"http": None, "https": None}  # 直连，不被豆瓣检测到代理 IP
        headers["Referer"] = "https://book.douban.com/"
        cookie_path = os.path.join(os.path.dirname(__file__), "douban_cookie.txt")
        if os.path.exists(cookie_path):
            try:
                with open(cookie_path, "r", encoding="utf-8") as f:
                    cookie = f.read().strip()
                    if cookie:
                        headers["Cookie"] = cookie
            except Exception:
                pass

    return headers, proxies


class MultiSourceMetaRecord(MetaRecord):
    """扩展 MetaRecord，支持封面代理"""

    def __getattribute__(self, item):
        if item == "cover" and PROXY_DOUBAN_COVER:
            cover_url = super().__getattribute__(item)
            if cover_url and _is_external_cover(cover_url):
                try:
                    host = DOUBAN_COVER_PROXY_HOST
                    if not host:
                        try:
                            host = request.host_url
                        except Exception:
                            pass
                    if host and host not in cover_url:
                        encoded = urllib.parse.quote(cover_url)
                        self.cover = host + DOUBAN_COVER_PROXY_PATH + encoded
                except Exception:
                    pass
        return super().__getattribute__(item)


# ---- 封面代理路由 ----
try:
    from cps.search_metadata import meta

    @meta.route("/metadata/douban_cover", methods=["GET"])
    def proxy_douban_cover():
        """代理外部封面。豆瓣封面直连(不走全局代理) + Cookie + Referer。"""
        cover_url = urllib.parse.unquote(request.args.get("cover", ""))
        if not cover_url:
            return Response("", status=400)
        headers, proxies = _get_cover_request_params(cover_url)
        resp = requests.get(cover_url, headers=headers,
                           timeout=15, proxies=proxies)
        return Response(resp.content, mimetype=resp.headers.get("Content-Type", "image/jpeg"))

except ImportError:
    pass  # meta 路由在非 Web 环境下不可用
