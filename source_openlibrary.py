"""
MultiSource - Open Library 数据源
通过 Open Library REST API (openlibrary.org) 查询书籍元数据。
"""
import json
import os
import time
from typing import List, Optional

import requests

from .book_record import BookRecord, canonical_isbn


# ============================================================
# 配置
# ============================================================
OL_BOOKS_API = "https://openlibrary.org/api/books"
OL_SEARCH_API = "https://openlibrary.org/search.json"
OL_TIMEOUT = 15

OL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# 代理：从环境变量读取，NAS 环境无法直连 openlibrary.org
_OL_PROXIES = None
if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
    _OL_PROXIES = {
        "http": os.environ.get("HTTP_PROXY", ""),
        "https": os.environ.get("HTTPS_PROXY", os.environ.get("HTTP_PROXY", "")),
    }


# ============================================================
# 源模块
# ============================================================
class OpenLibrarySource:
    SOURCE_ID = "openlibrary"
    SOURCE_NAME = "Open Library"

    def search(self, query: str, is_isbn: bool = False) -> List[BookRecord]:
        if is_isbn:
            return self._search_by_isbn(query)
        return self._search_by_title(query)

    def _search_by_isbn(self, isbn_str: str) -> List[BookRecord]:
        """通过 ISBN 精确查询"""
        clean = canonical_isbn(isbn_str)
        if not clean:
            clean = isbn_str.strip().replace("-", "")

        bibkey = f"ISBN:{clean}"
        params = {
            "bibkeys": bibkey,
            "format": "json",
            "jscmd": "data",
        }

        try:
            data = self._fetch_json(OL_BOOKS_API, params)
            if not data or bibkey not in data:
                return []

            book_data = data[bibkey]
            record = self._parse_book(book_data, isbn=clean)
            if record and record.title:
                return [record]

        except Exception as e:
            print(f"[OpenLibrary] ISBN 查询失败: {e}")

        return []

    def _search_by_title(self, title: str) -> List[BookRecord]:
        """通过标题搜索"""
        params = {
            "q": title,
            "limit": 10,
            "language": "chi",
        }

        try:
            time.sleep(0.3)
            data = self._fetch_json(OL_SEARCH_API, params)
            if not data or "docs" not in data:
                return []

            records = []
            for doc in data["docs"][:10]:
                record = self._parse_search_doc(doc)
                if record and record.title:
                    records.append(record)

            # 如果中文结果少，再搜一次不限语言
            if len(records) < 3:
                params2 = {"q": title, "limit": 10}
                data2 = self._fetch_json(OL_SEARCH_API, params2)
                if data2 and "docs" in data2:
                    for doc in data2["docs"][:10]:
                        record = self._parse_search_doc(doc)
                        if record and record.title:
                            if not any(r.title == record.title for r in records):
                                records.append(record)

            return records[:10]

        except Exception as e:
            print(f"[OpenLibrary] 标题搜索失败: {e}")

        return []

    def _fetch_json(self, url: str, params: dict) -> Optional[dict]:
        """获取 JSON 数据（优先走代理，因为 NAS 无法直连 openlibrary.org）"""
        try:
            resp = requests.get(url, params=params, headers=OL_HEADERS,
                                timeout=OL_TIMEOUT, proxies=_OL_PROXIES)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code != 200:
                print(f"[OpenLibrary] HTTP {resp.status_code}: {url}")
        except Exception as e:
            print(f"[OpenLibrary] JSON 请求失败 {url}: {e}")
        return None

    def _parse_book(self, book_data: dict, isbn: str = "") -> Optional[BookRecord]:
        """解析 /api/books 返回的单本书数据"""
        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
        )

        record.title = book_data.get("title", "").strip()

        # 副标题
        subtitle = book_data.get("subtitle", "")
        if subtitle:
            record.subtitle = subtitle.strip()

        # 作者
        authors = book_data.get("authors", [])
        record.authors = [a.get("name", "").strip() for a in authors if a.get("name")]

        # 译者（contributors 中 role 为 translator 的）
        for contributor in book_data.get("contributors", []) or []:
            role = contributor.get("role", "").lower()
            name = contributor.get("name", "").strip()
            if "translat" in role and name:
                record.translators.append(name)

        # 出版社
        publishers = book_data.get("publishers", [])
        record.publisher = (publishers[0].get("name", "").strip() if publishers else "")

        # 出版日期
        pub_date = book_data.get("publish_date", "")
        if pub_date:
            record.published_date = pub_date.strip()

        # ISBN
        identifiers = book_data.get("identifiers", {})
        if "isbn_13" in identifiers:
            record.isbn = canonical_isbn("".join(identifiers["isbn_13"]))
        elif "isbn_10" in identifiers:
            record.isbn = canonical_isbn("".join(identifiers["isbn_10"]))
        elif isbn:
            record.isbn = canonical_isbn(isbn)

        # 简介
        description = book_data.get("description", "")
        if isinstance(description, dict):
            record.description = description.get("value", "")
        elif isinstance(description, str):
            record.description = description

        # 封面
        covers = book_data.get("cover", {})
        if covers:
            for size in ("large", "medium", "small"):
                if size in covers:
                    record.cover_url = covers[size]
                    break

        # 页数
        pages = book_data.get("number_of_pages", 0)
        try:
            record.pages = int(pages)
        except (TypeError, ValueError):
            pass

        # 系列
        series_list = book_data.get("series", [])
        if series_list:
            record.series = series_list[0].get("name", "").strip() if isinstance(series_list[0], dict) else str(series_list[0])

        # 标签/主题
        subjects = book_data.get("subjects", [])
        record.tags = [
            s.get("name", "").strip() if isinstance(s, dict) else str(s).strip()
            for s in subjects[:20] if s
        ]

        # 语言
        languages = book_data.get("languages", [])
        if languages:
            lang = languages[0]
            record.language = lang.get("key", "").replace("/languages/", "") if isinstance(lang, dict) else str(lang)

        # URL
        url = book_data.get("url", "")
        record.url = url.strip() if url else f"https://openlibrary.org/books/{book_data.get('key', '').replace('/books/', '')}"

        # ID
        record.raw_id = book_data.get("key", "").replace("/books/", "")

        return record

    def _parse_search_doc(self, doc: dict) -> Optional[BookRecord]:
        """解析 /search.json 返回的搜索结果项"""
        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
        )

        record.title = doc.get("title", "").strip()
        if not record.title:
            return None

        # 副标题
        subtitle = doc.get("subtitle", "")
        if subtitle:
            record.subtitle = subtitle.strip()

        # 作者
        authors = doc.get("author_name", [])
        record.authors = [a.strip() for a in authors if a]

        # 译者
        translators = doc.get("contributor", [])
        record.translators = [t.strip() for t in translators if t]

        # 出版社
        publishers = doc.get("publisher", [])
        record.publisher = publishers[0].strip() if publishers else ""

        # 出版日期
        pub_years = doc.get("publish_year", [])
        record.published_date = str(pub_years[0]) if pub_years else ""

        # ISBN
        isbns = doc.get("isbn", [])
        if isbns:
            record.isbn = canonical_isbn(isbns[0])

        # 语言
        languages = doc.get("language", [])
        if languages:
            record.language = languages[0] if isinstance(languages[0], str) else str(languages[0])

        # 页数
        pages = doc.get("number_of_pages_median", 0)
        try:
            record.pages = int(pages) if pages else 0
        except (TypeError, ValueError):
            pass

        # 主题
        subjects = doc.get("subject", [])
        record.tags = [s.strip() for s in subjects[:20] if s]

        # 封面
        cover_id = doc.get("cover_i")
        if cover_id:
            record.cover_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"

        # URL
        ol_key = doc.get("key", "")
        record.url = f"https://openlibrary.org{ol_key}" if ol_key else ""
        record.raw_id = ol_key.replace("/works/", "").replace("/books/", "")

        return record
