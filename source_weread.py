"""
MultiSource - 微信读书 (WeRead) 数据源
使用微信读书 Agent Gateway API，支持书名搜索和书籍详情查询
API: POST https://i.weread.qq.com/api/agent/gateway
"""
import time
import os
import requests
from typing import List, Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .book_record import BookRecord, canonical_isbn

# ============================================================
# 配置
# ============================================================
WEREAD_TIMEOUT = 8   # 查询超时（秒）
WEREAD_MAX_RESULTS = 10  # 最多返回条数
WEREAD_DETAIL_WORKERS = 3  # 并行获取详情的线程数
WEREAD_DETAIL_LIMIT = 5  # 最多获取多少本书的详情

WEREAD_GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"

# 优先读 weread_apikey.txt；无文件则不传 key
WEREAD_API_KEY = ""

WEREAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
}

class WeReadSource:
    """微信读书 Agent Gateway 数据源"""

    SOURCE_ID = "weread"
    SOURCE_NAME = "微信读书"

    def __init__(self, api_key: str = ""):
        self._api_key = api_key or WEREAD_API_KEY

    # ---- API Key 热更新（和 douban_cookie.txt / googlebooks_apikey.txt 同模式）----

    def _read_apikey_file(self) -> str:
        """从插件目录的 weread_apikey.txt 读取 API Key"""
        try:
            key_path = os.path.join(os.path.dirname(__file__), "weread_apikey.txt")
            if os.path.exists(key_path):
                with open(key_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        return content
        except Exception:
            pass
        return ""

    def _get_api_key(self) -> str:
        """获取 API Key：优先从文件读取；无文件则返回空字符串"""
        return self._read_apikey_file() or self._api_key

    def _get_auth_headers(self) -> Dict[str, str]:
        """构建带鉴权的请求头"""
        headers = WEREAD_HEADERS.copy()
        headers["Authorization"] = "Bearer " + self._get_api_key()
        return headers

    # ---- 主搜索接口 ----

    def search(self, query: str, is_isbn: bool = False) -> List[BookRecord]:
        """主搜索接口"""
        if not query.strip():
            return []

        # 先用 Agent Gateway 搜索
        search_results = self._gateway_search(query)

        if not search_results:
            return []

        # 并行获取前 N 本书的详情（获取 ISBN + 简介）
        records = self._enrich_with_details(search_results)

        return records[:WEREAD_MAX_RESULTS]

    # ---- Agent Gateway 调用 ----

    def _gateway_call(self, api_name: str, params: dict) -> Optional[dict]:
        """调用 Agent Gateway 统一入口"""
        body = {"api_name": api_name, "skill_version": WEREAD_SKILL_VERSION}
        body.update(params)

        try:
            resp = requests.post(
                WEREAD_GATEWAY,
                json=body,
                headers=self._get_auth_headers(),
                timeout=WEREAD_TIMEOUT,
                verify=False,
            )
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(1)
                resp = requests.post(
                    WEREAD_GATEWAY,
                    json=body,
                    headers=self._get_auth_headers(),
                    timeout=WEREAD_TIMEOUT,
                    verify=False,
                )
                if resp.status_code == 200:
                    return resp.json()
            elif resp.status_code != 200:
                print(f"[WeRead] HTTP {resp.status_code} calling {api_name}")
        except Exception as e:
            print(f"[WeRead] Gateway 调用失败 {api_name}: {e}")
        return None

    def _gateway_search(self, query: str) -> List[dict]:
        """通过 Agent Gateway 搜索书籍"""
        data = self._gateway_call("/store/search", {"keyword": query, "count": WEREAD_MAX_RESULTS})
        if not data:
            return []

        results = []
        for section in (data.get("results") or []):
            for book in (section.get("books") or []):
                results.append(book)

        return results

    def _enrich_with_details(self, search_results: List[dict]) -> List[BookRecord]:
        """并行获取书籍详情，返回 BookRecord 列表"""
        records = []
        detail_candidates = search_results[:WEREAD_DETAIL_LIMIT]

        # 并行获取详情
        detail_map = {}
        if detail_candidates:
            with ThreadPoolExecutor(max_workers=WEREAD_DETAIL_WORKERS) as pool:
                futures = {}
                for book in detail_candidates:
                    info = book.get("bookInfo") or {}
                    bid = info.get("bookId", "")
                    if bid:
                        futures[pool.submit(self._get_book_detail, bid)] = book

                for future in as_completed(futures):
                    book = futures[future]
                    try:
                        detail = future.result(timeout=WEREAD_TIMEOUT)
                        if detail:
                            bid = (book.get("bookInfo") or {}).get("bookId", "")
                            detail_map[bid] = detail
                    except Exception as e:
                        print(f"[WeRead] 获取详情失败: {e}")

        # 构建 BookRecord
        for book in search_results:
            info = book.get("bookInfo") or {}
            bid = info.get("bookId", "")

            detail = detail_map.get(bid, {})

            record = BookRecord(
                source_id=self.SOURCE_ID,
                source_name=self.SOURCE_NAME,
            )

            # 标题：优先用详情里的（更准确）
            record.title = (detail.get("title") or info.get("title", "")).strip()
            if not record.title:
                continue

            # 作者
            author = detail.get("author") or info.get("author", "")
            record.authors = [author.strip()] if author else []

            # ISBN
            isbn = detail.get("isbn", "")
            if isbn:
                record.isbn = canonical_isbn(isbn)

            # 出版社
            record.publisher = detail.get("publisher", "").strip()

            # 出版日期
            publish_time = detail.get("publishTime", "")
            if publish_time:
                record.published_date = publish_time[:10]  # "2022-04-01 00:00:00" -> "2022-04-01"

            # 简介
            intro = detail.get("intro", "")
            record.description = intro if intro else ""

            # 封面
            cover = info.get("cover", "")
            record.cover_url = cover if cover else ""

            # 评分
            rating = info.get("newRating", 0)
            try:
                record.rating = float(rating) / 100.0 if rating else 0.0
            except (TypeError, ValueError):
                pass

            # 分类
            category = detail.get("category", "")
            if category:
                record.tags = [category]

            # 链接
            record.url = detail.get("deepLink") or info.get("deepLink", "")

            # ID
            record.raw_id = bid

            records.append(record)

        return records

    def _get_book_detail(self, book_id: str) -> Optional[dict]:
        """获取单本书的详情"""
        return self._gateway_call("/book/info", {"bookId": book_id})
