"""
MultiSource - 上海图书馆（Shanghai Library）数据源
通过 VuFind API (https://vufind.library.sh.cn) 查询中文书目元数据。

两阶段：
  1. API 搜索（/api/v1/search）获取记录 ID 列表
  2. HTML 详情页（/Record/{id}）获取丰富元数据（出版社、ISBN、页数、CLC、内容提要）
"""
import re
import json
import time
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .book_record import BookRecord, canonical_isbn, normalize_date

# ============================================================
# 配置
# ============================================================
SHL_API_BASE = "https://vufind.library.sh.cn/api/v1"
SHL_RECORD_BASE = "https://vufind.library.sh.cn/Record"
SHL_TIMEOUT = 10            # 请求超时（秒）
SHL_MAX_RESULTS = 10         # 搜索结果最多处理条数
SHL_DETAIL_WORKERS = 3       # 详情并发数

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
        """书名搜索：API → 详情"""
        record_ids = self._search_by_api("Title", title, SHL_MAX_RESULTS)
        if not record_ids:
            return []
        return self._fetch_details_concurrent(record_ids)

    def _search_by_isbn(self, isbn: str) -> List[BookRecord]:
        """ISBN 搜索：API → 详情"""
        record_ids = self._search_by_api("ISN", isbn, 3)
        if not record_ids:
            return []
        return self._fetch_details_concurrent(record_ids)

    def _search_by_api(self, search_type: str, query: str,
                       limit: int) -> List[str]:
        """调用 VuFind API 搜索，返回记录 ID 列表"""
        params = {
            "lookfor": query,
            "type": search_type,
            "limit": limit,
        }
        data = self._fetch_json(f"{SHL_API_BASE}/search", params)
        if not data:
            return []

        records = data.get("records", [])
        return [r["id"] for r in records if r.get("id")]

    # ---- 详情获取 ----

    def _fetch_details_concurrent(self, record_ids: List[str]) -> List[BookRecord]:
        """并发获取多个记录详情"""
        results = []
        with ThreadPoolExecutor(max_workers=SHL_DETAIL_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_record_detail, rid): rid
                for rid in record_ids
            }
            for future in as_completed(futures):
                try:
                    record = future.result(timeout=SHL_TIMEOUT)
                    if record:
                        results.append(record)
                except Exception:
                    continue
        return results

    def _fetch_record_detail(self, record_id: str) -> Optional[BookRecord]:
        """获取单个记录详情页并解析"""
        url = f"{SHL_RECORD_BASE}/{record_id}"
        html = self._fetch_html(url)
        if not html:
            return None
        return self._parse_html_detail(html, record_id)

    # ---- HTTP 请求 ----

    def _fetch_json(self, url: str, params: dict) -> Optional[dict]:
        """HTTP GET → JSON（直连，国内网站）"""
        try:
            resp = requests.get(url, params=params, headers=SHL_HEADERS,
                                timeout=SHL_TIMEOUT, verify=False)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def _fetch_html(self, url: str) -> Optional[str]:
        """HTTP GET → HTML（直连，国内网站）"""
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

    def _parse_html_detail(self, html: str, record_id: str) -> Optional[BookRecord]:
        """解析详情页 HTML，提取元数据"""
        # COinS 元数据（Publisher, Date, ISBN）
        publisher, pub_date, coin_isbn = self._parse_coin_metadata(html)

        # Table 字段（Pages, CLC, Description）
        table_data = self._parse_table_fields(html)

        # 从 API 搜索结果中提取的基本字段需要从 search 层传入
        # 这里只解析详情页特有的字段
        isbn = canonical_isbn(coin_isbn) if coin_isbn else ""

        # 提取标题和作者（从 HTML 结构）
        title = self._extract_tag(html, "property", "name", "babel_title") or ""
        # 提取副标题
        subtitle = ""
        if ":" in title or "：" in title:
            parts = re.split(r"[:：]", title, maxsplit=1)
            title, subtitle = parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""

        author = self._extract_tag(html, "property", "author") or ""

        # 提取封面 URL（内部图片服务：/Cover/Show?instanceId=UUID）
        cover_url = self._extract_cover_url(html, record_id)
        if cover_url:
            cover_url = self._validate_cover_url(cover_url)

        # 构建记录
        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
            title=title,
            subtitle=subtitle,
            authors=[a.strip() for a in author.split(";") if a.strip()] if author else [],
            publisher=publisher,
            published_date=normalize_date(pub_date) if pub_date else "",
            isbn=isbn,
            description=table_data.get("description", ""),
            cover_url=cover_url,
            tags=table_data.get("subjects", []),
            language=table_data.get("language", ""),
            pages=table_data.get("pages", ""),
            clc_code="",
            url=f"{SHL_RECORD_BASE}/{record_id}",
            raw_id=isbn or record_id,
            identifiers={"shl_id": record_id},
        )
        return record

    def _parse_coin_metadata(self, html: str) -> tuple:
        """解析 COinS 元数据 → (publisher, pub_date, isbn)"""
        publisher = ""
        pub_date = ""
        isbn = ""

        match = re.search(r'<span\s+class="Z3988"[^>]*title="([^"]*)"', html)
        if not match:
            return publisher, pub_date, isbn

        coin = match.group(1)
        # URL 解码
        from html import unescape
        coin = unescape(unescape(coin))

        params = {}
        for part in coin.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                from urllib.parse import unquote
                params[k] = unquote(v)

        publisher = params.get("rft.pub", "")
        pub_date = params.get("rft.date", "")
        isbn = params.get("rft.isbn", "")

        return publisher, pub_date, isbn

    def _parse_table_fields(self, html: str) -> dict:
        """解析 HTML table 中的字段（Pages, CLC, Description）"""
        result = {
            "description": "",
            "pages": "",
            "subjects": [],
            "language": "",
        }

        # Contents / 内容提要
        m = re.search(r"<th>Contents:</th>\s*<td>\s*(.*?)\s*</td>", html, re.DOTALL)
        if m:
            desc = m.group(1)
            # 去除 HTML 标签
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            if len(desc) > 10:
                result["description"] = desc

        # Carrier Form / 载体形态（含页数）
        m = re.search(r"<th>Carrier Form:</th>\s*<td>\s*(.*?)\s*</td>", html, re.DOTALL)
        if m:
            cf = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
            page_match = re.search(r"(\d+)\s*页", cf)
            if page_match:
                result["pages"] = page_match.group(1)

        # Subjects
        m = re.search(r"<th>Subjects:</th>\s*<td>\s*(.*?)\s*</td>", html, re.DOTALL)
        if m:
            subj_html = m.group(1)
            subjects = re.findall(r">\s*([^<]+)\s*<", subj_html)
            result["subjects"] = [s.strip() for s in subjects if s.strip()]

        # Language
        m = re.search(r"<th>Language:</th>\s*<td>\s*(.*?)\s*</td>", html, re.DOTALL)
        if m:
            lang = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if lang.lower() in ("chi", "chinese", "中文"):
                result["language"] = "中文"
            elif lang:
                result["language"] = lang

        return result

    @staticmethod
    def _extract_cover_url(html: str, record_id: str) -> str:
        """提取书籍封面图片 URL。
        VuFind 内部图片服务格式: /Cover/Show?instanceId=UUID"""
        m = re.search(
            r'<img[^>]*src="(/Cover/Show\?instanceId=[^"]*)"',
            html
        )
        if m:
            return f"https://vufind.library.sh.cn{m.group(1)}"
        return ""

    def _validate_cover_url(self, cover_url: str) -> str:
        """校验封面 URL：HEAD 请求验证状态码 + Content-Type + 大小 > 1KB。
        不合法则返回空字符串，避免存无效/占位封面。"""
        try:
            resp = requests.head(cover_url, headers=SHL_HEADERS,
                                 timeout=SHL_TIMEOUT, verify=False)
            if resp.status_code != 200:
                return ""
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                return ""
            content_length = resp.headers.get("Content-Length", "0")
            if int(content_length) < 1024:
                return ""
            return cover_url
        except Exception:
            return ""

    @staticmethod
    def _extract_tag(html: str, attr: str, value: str,
                     extra_attr: str = "") -> str:
        """提取 HTML meta 或 span 标签的属性值"""
        if extra_attr:
            pattern = rf'<[^>]+\b{attr}="{value}"[^>]*\b{extra_attr}="([^"]*)"'
        else:
            pattern = rf'<[^>]+\b{attr}="{value}"[^>]*content="([^"]*)"'
        m = re.search(pattern, html)
        return m.group(1) if m else ""
