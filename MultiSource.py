"""
MultiSource — 多源聚合书籍元数据插件

整合三个数据源（豆瓣、国家图书馆、Open Library），通过三层防错机制
（ISBN 精确匹配 → 复合指纹匹配 → 模糊匹配打分）智能合并结果。

特性:
  - 豆瓣翻页：自动拉取多页结果，支持上下翻页
  - 字段级溯源：每个字段标注来自哪个数据源
  - 可配置：源开关、翻页条数、超时均可调整
  - 模块化：每个数据源独立文件，修改互不影响
"""
import re
import time
import urllib.parse
import urllib.request
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from .book_record import BookRecord, MergedBook, canonical_isbn
from .matcher import BookMatcher
from .source_douban import DoubanSource
from .source_nlc import NLCSource
from .source_openlibrary import OpenLibrarySource


# ============================================================
# 配置
# ============================================================
PROVIDER_NAME = "MultiSource"
PROVIDER_ID = "multisource"

# 源开关：设为 False 可禁用某个数据源
# 注意：NLC (国家图书馆) 和 OpenLibrary 从容器内访问经常超时/不可达，默认关闭
SOURCE_DOUBAN_ENABLED = True
SOURCE_NLC_ENABLED = False  # NLC 从 NAS 容器内不可达，仅在大陆网络环境可用
SOURCE_OPENLIBRARY_ENABLED = True

# 最大并发源查询数
SOURCE_TIMEOUT = 12  # 单源超时（秒）

# 封面代理（解决豆瓣防盗链）
PROXY_DOUBAN_COVER = True
DOUBAN_COVER_PROXY_HOST = ""  # 空 = 自动使��当前 host
DOUBAN_COVER_PROXY_PATH = "metadata/douban_cover?cover="
DOUBAN_COVER_DOMAIN = "doubanio.com"

# 代理设置：从环境变量读取，用于访问外网资源
import os as _os
_COVER_PROXIES = None
if _os.environ.get("HTTPS_PROXY") or _os.environ.get("HTTP_PROXY"):
    _COVER_PROXIES = {
        "http": _os.environ.get("HTTP_PROXY", ""),
        "https": _os.environ.get("HTTPS_PROXY", _os.environ.get("HTTP_PROXY", "")),
    }

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

        # 安装封面代理
        self._hack_cover_proxy()

        super().__init__()
        log.info(f"[MultiSource] 初始化完成，启用的源: "
                 f"{[s.SOURCE_NAME for s in self.sources]}")

    def search(self, query: str, generic_cover: str = "",
               locale: str = "en") -> List[MetaRecord]:
        """
        Calibre-Web 插件标准接口。
        返回 MetaRecord 列表供 UI 展示。
        """
        if not self.active or not self.sources:
            return []

        query = query.strip()
        if not query:
            return []

        # 判断是否为 ISBN 搜索
        is_isbn = self._is_isbn_query(query)

        # 并行查询所有源
        log.info(f"[MultiSource] 搜索 '{query}' (is_isbn={is_isbn})")
        all_records = self._query_all_sources(query, is_isbn)

        if not all_records:
            log.info("[MultiSource] 所有源返回空结果")
            return []

        # 去重合并
        merged = self.matcher.merge(all_records)
        log.info(f"[MultiSource] 合并后: {len(merged)} 条 (原始 {len(all_records)} 条)")

        # 转换为 MetaRecord 列表
        return self._to_meta_records(merged)

    # ---- 内部方法 ----

    @staticmethod
    def _is_isbn_query(query: str) -> bool:
        """判断是否为 ISBN 搜索"""
        isbn = canonical_isbn(query)
        return len(isbn) in (10, 13)

    def _query_all_sources(self, query: str, is_isbn: bool) -> List[BookRecord]:
        """并行查询所有启用的数据源"""
        all_records = []

        with ThreadPoolExecutor(max_workers=len(self.sources)) as pool:
            futures = {
                pool.submit(self._query_source, source, query, is_isbn): source
                for source in self.sources
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    records = future.result(timeout=SOURCE_TIMEOUT)
                    if records:
                        source_count = sum(
                            1 for r in records
                            if "__page_marker__" not in r.tags
                        )
                        log.info(f"[MultiSource] {source.SOURCE_NAME}: "
                                 f"{source_count} 条结果")
                        all_records.extend(records)
                except Exception as e:
                    log.error(f"[MultiSource] {source.SOURCE_NAME} 查询失败: {e}")

        return all_records

    @staticmethod
    def _query_source(source, query: str, is_isbn: bool) -> List[BookRecord]:
        return source.search(query, is_isbn)

    def _to_meta_records(self, merged_books: List[MergedBook]) -> List[MetaRecord]:
        """将 MergedBook 列表转换为 Calibre-Web 的 MetaRecord 列表"""
        results = []

        for book in merged_books:
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
            if book.clc_code:
                identifiers["clc"] = book.clc_code

            # 合并 note 到标题
            title = book.title
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
            # 添加字段来源标注
            if book.field_sources:
                source_tags = []
                for field, src in book.field_sources.items():
                    short = {"豆瓣": "DB", "国家图书馆": "NLC", "Open Library": "OL"}.get(src, src[:3])
                    source_tags.append(f"{field}:{short}")
                # 只取前几个关键字段
                key_fields = ["title", "authors", "publisher", "isbn",
                              "cover_url", "rating", "description", "clc_code"]
                key_source_tags = []
                for f in key_fields:
                    if f in book.field_sources:
                        src = book.field_sources[f]
                        short = {"豆瓣": "DB", "国家图书馆": "NLC",
                                 "Open Library": "OL"}.get(src, src[:3])
                        key_source_tags.append(f"{f}={short}")
                tags.extend(key_source_tags[:8])

            # 去重
            tags = self._dedup_tags(tags)

            # 处理封面
            cover = book.cover_url
            if cover and PROXY_DOUBAN_COVER and DOUBAN_COVER_DOMAIN in cover:
                pass  # cover 代理在 save 时处理

            record_id = book.isbn or str(hash(book.title))[:16]

            # 创建 MetaRecord
            meta = MultiSourceMetaRecord(
                id=record_id,
                title=title,
                authors=book.authors,
                publisher=book.publisher,
                description=book.description,
                url=book.url,
                source=source_info,
                identifiers=identifiers,
                tags=tags,
                cover=cover,
                rating=book.rating,
                publishedDate=book.published_date,
                series=book.series,
            )

            results.append(meta)

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
        """安装封面代理以解决豆瓣防盗链"""
        try:
            h = _get_helper()
            save_cover = h.save_cover_from_url

            def new_save_cover(url, book_path):
                nonlocal save_cover
                if DOUBAN_COVER_DOMAIN in url:
                    if PROXY_DOUBAN_COVER:
                        parsed = urllib.parse.urlparse(url)
                        qs = urllib.parse.parse_qs(parsed.query)
                        cover_url = urllib.parse.unquote(qs.get("cover", [url])[0])
                    else:
                        cover_url = url
                    resp = requests.get(cover_url, headers=DEFAULT_HEADERS,
                                       proxies=_COVER_PROXIES, timeout=15)
                    return h.save_cover(resp, book_path)
                return save_cover(url, book_path)

            h.save_cover_from_url = new_save_cover
        except Exception as e:
            log.error(f"[MultiSource] 封面代理安装失败（可忽略）: {e}")


class MultiSourceMetaRecord(MetaRecord):
    """扩展 MetaRecord，支持封面代理"""

    def __getattribute__(self, item):
        if item == "cover" and PROXY_DOUBAN_COVER:
            cover_url = super().__getattribute__(item)
            if cover_url and DOUBAN_COVER_DOMAIN in cover_url:
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
        """代理豆瓣封面"""
        cover_url = urllib.parse.unquote(request.args.get("cover", ""))
        if not cover_url:
            return Response("", status=400)
        resp = requests.get(cover_url, headers=DEFAULT_HEADERS,
                           proxies=_COVER_PROXIES, timeout=15)
        return Response(resp.content, mimetype=resp.headers.get("Content-Type", "image/jpeg"))

except ImportError:
    pass  # meta 路由在非 Web 环境下不可用
