"""
MultiSource - Google Books API 数据源
支持书名搜索和 ISBN 精确查询
API: https://developers.google.com/books/docs/v1/using
"""
import os
import time
import requests
from typing import List, Optional

from . import proxy_manager
from .proxy_manager import probe_best_proxy
from requests.adapters import HTTPAdapter
from .book_record import BookRecord, canonical_isbn, normalize_date, normalize_date

# ============================================================
# 配置
# ============================================================
GB_TIMEOUT = 8   # 查询超时（秒）
GB_MAX_RESULTS = 10  # 最多返回条数

# Google Books API 免费额度：无需 key 也能查询，但有限速
# 有 key 的话每日 1000 次
GB_API_KEY = "AIzaSyDaMsuCFGtm6zDc2U70NCv9kEF4tZDQWis"  # 备用：硬编码 Key（优先读 googlebooks_apikey.txt）
GB_SEARCH_API = "https://www.googleapis.com/books/v1/volumes"

GB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# 代理：由 proxy_manager 动态获取
def _get_proxies():
    return proxy_manager.get_proxies()


class GoogleBooksSource:
    """Google Books API 数据源"""

    SOURCE_ID = "googlebooks"
    SOURCE_NAME = "Google Books"

    _proxies_cache = None
    _proxies_cache_name = ""

    def __init__(self, api_key: str = ""):
        self._api_key = api_key or GB_API_KEY
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=0))
        self._session.mount("http://", HTTPAdapter(max_retries=0))

    def _read_apikey_file(self) -> str:
        """从插件目录的 googlebooks_apikey.txt 读取 API Key（无需重启容器即可更新）"""
        try:
            key_path = os.path.join(os.path.dirname(__file__), "googlebooks_apikey.txt")
            if os.path.exists(key_path):
                with open(key_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        return content
        except Exception:
            pass
        return ""

    def _get_api_key(self) -> str:
        """获取 API Key：优先从文件读取，fallback 到硬编码"""
        return self._read_apikey_file() or self._api_key

    def search(self, query: str, is_isbn: bool = False) -> List[BookRecord]:
        """主搜索接口"""
        if is_isbn:
            return self._search_by_isbn(query)
        return self._search_by_title(query)

    def _search_by_isbn(self, isbn_str: str) -> List[BookRecord]:
        """通过 ISBN 精确查询"""
        clean = canonical_isbn(isbn_str)
        if not clean:
            clean = isbn_str.strip().replace("-", "")

        params = {"q": f"isbn:{clean}"}
        _api_key = self._get_api_key()
        if _api_key:
            params["key"] = _api_key

        try:
            data = self._fetch_json(GB_SEARCH_API, params)
            if not data or "items" not in data:
                return []

            records = []
            for item in data["items"][:3]:
                record = self._parse_volume(item, isbn=clean)
                if record and record.title:
                    records.append(record)

            return records

        except Exception as e:
            print(f"[GoogleBooks] ISBN 查询失败: {e}")
            return []

    def _search_by_title(self, title: str) -> List[BookRecord]:
        """通过标题搜索（优先中文）"""
        params = {
            "q": f'intitle:"{title}"',
            "langRestrict": "zh-CN",
            "maxResults": min(GB_MAX_RESULTS, 20),
            "printType": "books",
        }
        _api_key = self._get_api_key()
        if _api_key:
            params["key"] = _api_key

        records = []

        try:
            time.sleep(0.3)  # 友好限速
            data = self._fetch_json(GB_SEARCH_API, params)
            if data and "items" in data:
                for item in data["items"][:20]:
                    record = self._parse_volume(item)
                    if record and record.title:
                        records.append(record)

            # 如果中文结果少，再搜一次不限语言
            if len(records) < 3:
                params["langRestrict"] = ""
                data2 = self._fetch_json(GB_SEARCH_API, params)
                if data2 and "items" in data2:
                    for item in data2["items"][:20]:
                        record = self._parse_volume(item)
                        if record and record.title:
                            if not any(r.title == record.title for r in records):
                                records.append(record)

            return records[:20]

        except Exception as e:
            print(f"[GoogleBooks] 标题搜索失败: {e}")

        return []

    def _fetch_json(self, url: str, params: dict) -> Optional[dict]:
        """获取 JSON 数据"""
        try:
            resp = self._session.get(url, params=params, headers=GB_HEADERS,
                                timeout=GB_TIMEOUT, proxies=self._get_best_proxies(), verify=False)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                print(f"[GoogleBooks] 被限速 (429)，等待 1 秒后重试...")
                time.sleep(1)
                resp = self._session.get(url, params=params, headers=GB_HEADERS,
                                    timeout=GB_TIMEOUT, proxies=self._get_best_proxies(), verify=False)
                if resp.status_code == 200:
                    return resp.json()
            elif resp.status_code != 200:
                print(f"[GoogleBooks] HTTP {resp.status_code}: {url}")
        except Exception as e:
            print(f"[GoogleBooks] JSON 请求失败 {url}: {e}")
        return None

    def _get_best_proxies(self):
        if self._proxies_cache is None:
            px, name = probe_best_proxy()
            self._proxies_cache = px
            self._proxies_cache_name = name
        return self._proxies_cache

    def _parse_volume(self, item: dict, isbn: str = "") -> Optional[BookRecord]:
        """解析 Google Books API 返回的 volume"""
        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
        )

        info = item.get("volumeInfo") or {}

        # 标题
        record.title = info.get("title", "").strip()
        if not record.title:
            return None

        # 副标题
        subtitle = info.get("subtitle", "")
        if subtitle:
            record.subtitle = subtitle.strip()

        # 作者
        authors = info.get("authors", [])
        record.authors = [a.strip() for a in authors if a]

        # 出版社
        record.publisher = info.get("publisher", "").strip()

        # 出版日期
        pub_date = info.get("publishedDate", "")
        record.published_date = normalize_date(pub_date) if pub_date else ""

        # ISBN
        identifiers = info.get("industryIdentifiers", [])
        isbn_13 = ""
        isbn_10 = ""
        for ident in identifiers:
            t = ident.get("type", "")
            v = ident.get("identifier", "")
            if t == "ISBN_13":
                isbn_13 = v
            elif t == "ISBN_10":
                isbn_10 = v
        if isbn_13:
            record.isbn = canonical_isbn(isbn_13)
        elif isbn_10:
            record.isbn = canonical_isbn(isbn_10)
        elif isbn:
            record.isbn = canonical_isbn(isbn)

        # 简介
        description = info.get("description", "")
        record.description = description if description else ""

        # 封面
        image_links = info.get("imageLinks", {})
        # 优先大图
        for size in ("extraLarge", "large", "medium", "small", "thumbnail"):
            if size in image_links:
                url = image_links[size]
                # 去掉 curl 参数获取无边框原图
                url = url.replace("&edge=curl", "")
                record.cover_url = url
                break

        # 页数
        pages = info.get("pageCount", 0)
        try:
            record.pages = int(pages)
        except (TypeError, ValueError):
            pass

        # 语言
        lang = info.get("language", "")
        record.language = lang if lang else ""

        # 分类/标签
        categories = info.get("categories", [])
        record.tags = [c.strip() for c in categories[:20] if c]

        # URL
        record.url = info.get("infoLink", "") or info.get("canonicalVolumeLink", "")

        # ID
        record.raw_id = item.get("id", "")

        # 平均评分
        avg_rating = info.get("averageRating", 0)
        try:
            record.rating = float(avg_rating)
        except (TypeError, ValueError):
            pass

        return record
