"""
MultiSource - 上海图书馆（Shanghai Library）数据源
通过 VuFind API (https://vufind.library.sh.cn) 查询中文书目元数据。

两阶段：
  1. API 搜索（/api/v1/search）获取记录 ID + 基本字段
  2. HTML 详情页（/Record/{id}）提取所有元数据（表格字段优于 COinS）
"""
import re
import json
import time
from typing import List, Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape

import requests
from urllib.parse import unquote

from .book_record import BookRecord, canonical_isbn, normalize_date

# ============================================================
# 配置
# ============================================================
SHL_API_BASE = "https://vufind.library.sh.cn/api/v1"
SHL_RECORD_BASE = "https://vufind.library.sh.cn/Record"
SHL_TIMEOUT = 10
SHL_MAX_RESULTS = 10
SHL_DETAIL_WORKERS = 3

SHL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}


# ============================================================
# 源模块
# ============================================================
class ShanghaiLibrarySource:
    SOURCE_ID = "shlibrary"
    SOURCE_NAME = "上海图书馆"

    def search(self, query: str, is_isbn: bool = False) -> List[BookRecord]:
        if is_isbn:
            return self._search_by_isbn(query)
        return self._search_by_title(query)

    # ---- API 搜索 ----

    def _search_by_title(self, title: str) -> List[BookRecord]:
        results = self._search_by_api("Title", title, SHL_MAX_RESULTS)
        if not results:
            return []
        return self._fetch_details_concurrent(results)

    def _search_by_isbn(self, isbn: str) -> List[BookRecord]:
        results = self._search_by_api("ISN", isbn, 3)
        if not results:
            return []
        return self._fetch_details_concurrent(results)

    def _search_by_api(self, search_type: str, query: str,
                       limit: int) -> List[dict]:
        """调用 VuFind API 搜索，返回 [{id, title, authors, subjects, languages}, ...]"""
        params = {
            "lookfor": query,
            "type": search_type,
            "limit": limit,
        }
        data = self._fetch_json(f"{SHL_API_BASE}/search", params)
        if not data:
            return []

        results = []
        for r in data.get("records", []) or []:
            if r.get("id"):
                # Extract author names from API response
                authors = []
                primary = r.get("authors", {}).get("primary", {})
                for name, info in primary.items():
                    # Strip birth year suffix like " 1963-"
                    clean_name = re.sub(r"\s+\d{4}-$", "", name).strip()
                    authors.append(clean_name)

                # Extract subjects (flat list)
                api_subjects = []
                for group in r.get("subjects", []) or []:
                    api_subjects.extend(group)

                results.append({
                    "id": r["id"],
                    "title": r.get("title", ""),
                    "authors": authors,
                    "api_subjects": api_subjects,
                    "languages": r.get("languages", []),
                })
        return results

    # ---- 详情获取 ----

    def _fetch_details_concurrent(self, api_results: List[dict]) -> List[BookRecord]:
        """并发获取多个记录详情，传递 API 基本字段"""
        results = []
        with ThreadPoolExecutor(max_workers=SHL_DETAIL_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_record_detail, r): r
                for r in api_results
            }
            for future in as_completed(futures):
                try:
                    record = future.result(timeout=SHL_TIMEOUT)
                    if record:
                        results.append(record)
                except Exception:
                    continue
        return results

    def _fetch_record_detail(self, api_result: dict) -> Optional[BookRecord]:
        """获取单个记录详情页并解析"""
        record_id = api_result["id"]
        url = f"{SHL_RECORD_BASE}/{record_id}"
        html = self._fetch_html(url)
        if not html:
            return None
        return self._parse_html_detail(html, record_id, api_result)

    # ---- HTTP 请求 ----

    def _fetch_json(self, url: str, params: dict) -> Optional[dict]:
        try:
            resp = requests.get(url, params=params, headers=SHL_HEADERS,
                                timeout=SHL_TIMEOUT, verify=False)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def _fetch_html(self, url: str) -> Optional[str]:
        try:
            resp = requests.get(url, headers=SHL_HEADERS,
                                timeout=SHL_TIMEOUT, verify=False)
            resp.encoding = "utf-8"
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
        return None

    # ---- HTML 解析 ----

    def _parse_html_detail(self, html: str, record_id: str,
                           api_result: dict) -> Optional[BookRecord]:
        """从 HTML 表格提取所有元数据，API 字段作为 fallback"""
        table = self._parse_all_table_fields(html)

        # Title: prefer table, fallback to API
        title = table.get("title", "") or api_result.get("title", "")
        # Split subtitle
        subtitle = ""
        if ":" in title or "：" in title:
            parts = re.split(r"[:：]", title, maxsplit=1)
            title, subtitle = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")

        # Authors: prefer table 著者, fallback to API
        table_authors = table.get("authors", [])
        if table_authors:
            authors = table_authors
        else:
            authors = api_result.get("authors", [])

        # ISBN
        isbn_raw = table.get("isbn", "")
        isbn = canonical_isbn(isbn_raw) if isbn_raw else ""

        # Cover
        cover_url = self._extract_cover_url(html, record_id)
        if cover_url:
            cover_url = self._validate_cover_url(cover_url)

        # Subjects: merge table subjects + API subjects
        tags = table.get("subjects", [])
        api_subs = api_result.get("api_subjects", [])
        for s in api_subs:
            if s not in tags:
                tags.append(s)

        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
            title=title,
            subtitle=subtitle,
            authors=authors,
            publisher=table.get("publisher", ""),
            published_date=normalize_date(table.get("pub_date", "")) if table.get("pub_date") else "",
            isbn=isbn,
            description=table.get("description", ""),
            cover_url=cover_url,
            tags=tags,
            language=table.get("language", ""),
            pages=table.get("pages", ""),
            clc_code=table.get("clc", ""),
            url=f"{SHL_RECORD_BASE}/{record_id}",
            raw_id=isbn or record_id,
            identifiers={"shl_id": record_id},
        )
        return record

    def _parse_all_table_fields(self, html: str) -> dict:
        """解析 HTML 表格所有 th/td 对，兼容中英文标签。"""
        result = {
            "title": "",
            "authors": [],
            "publisher": "",
            "pub_date": "",
            "isbn": "",
            "clc": "",
            "description": "",
            "pages": "",
            "subjects": [],
            "language": "",
        }

        # Extract all <th>/<td> pairs
        pairs = re.findall(r"<th>(.*?)</th>\s*<td>(.*?)</td>", html, re.DOTALL)
        raw_fields = {}
        for th, td in pairs:
            key = th.strip().rstrip(":")
            value = re.sub(r"<[^>]+>", " ", td).strip()
            value = re.sub(r"\s+", " ", value)
            raw_fields[key] = value

        def get_field(*keys):
            for k in keys:
                if k in raw_fields:
                    return raw_fields[k]
            return ""

        # Author — 著者 (CN) / Authors (EN)
        author_text = get_field("著者", "Authors", "著者:")
        if author_text:
            result["authors"] = self._parse_authors(author_text)

        # Publisher
        result["publisher"] = get_field("出版社", "Publisher", "Publisher Address")

        # Publication date
        result["pub_date"] = get_field("出版时间", "Publication Dates", "Published")

        # ISBN
        result["isbn"] = get_field("ISBN", "ISBN:")

        # CLC
        result["clc"] = get_field("中图法", "CLC", "CLC:")

        # Description — 附注 (CN) / Contents (EN)
        desc = get_field("附注", "Contents", "附注:")
        if len(desc) > 10:
            result["description"] = unescape(desc)

        # Pages — 载体形态 (CN) / Carrier Form (EN)
        cf = get_field("载体形态", "Carrier Form", "载体形态:")
        if cf:
            m = re.search(r"(\d+)\s*页", cf)
            if m:
                result["pages"] = m.group(1)

        # Subjects — 主题 (CN) / Subjects (EN)
        subj_text = get_field("主题", "Subjects", "主题:")
        if subj_text:
            # Split on > (hierarchical separator, may appear as &gt;)
            parts = re.split(r"\s*(?:&gt;|>|--)\s*", unescape(subj_text))
            result["subjects"] = [p.strip() for p in parts if p.strip() and len(p.strip()) > 1]

        # Language — 语言 (CN) / Language (EN)
        lang = get_field("语言", "Language", "语言:")
        if lang and lang.lower() in ("chi", "chinese", "中文"):
            result["language"] = "中文"
        elif lang:
            result["language"] = lang

        # Title — 题名 (CN) / Title (EN) — from API primarily, but try table too
        title = get_field("题名", "Title")
        if title:
            result["title"] = title

        return result

    @staticmethod
    def _parse_authors(text: str) -> List[str]:
        """解析著者字段：'胡良剑 (编); 丁晓东 (编); 孙晓君 (编)' → ['胡良剑', '丁晓东', '孙晓君']"""
        # Split by semicolons (Chinese or English)
        authors = []
        for part in re.split(r"[;；]", text):
            part = part.strip()
            if not part:
                continue
            # Remove role suffixes in parentheses: (编), (著), (译), etc.
            part = re.sub(r"\s*[（(][^)）]*[)）]", "", part).strip()
            if part:
                authors.append(part)
        return authors

    @staticmethod
    def _extract_cover_url(html: str, record_id: str) -> str:
        m = re.search(
            r'<img[^>]*src="(/Cover/Show\?instanceId=[^"]*)"', html)
        if m:
            return f"https://vufind.library.sh.cn{m.group(1)}"
        return ""

    def _validate_cover_url(self, cover_url: str) -> str:
        try:
            resp = requests.head(cover_url, headers=SHL_HEADERS,
                                 timeout=SHL_TIMEOUT, verify=False)
            if resp.status_code != 200:
                return ""
            if not resp.headers.get("Content-Type", "").startswith("image/"):
                return ""
            if int(resp.headers.get("Content-Length", "0")) < 1024:
                return ""
            return cover_url
        except Exception:
            return ""
