"""
MultiSource - 当当网数据源
从 search.dangdang.com 爬取书籍元数据。
当当使用阿里云 WAF 保护详情页，仅能从搜索结果页获取标题+封面+价格。
"""
import re
from typing import List, Optional

import requests

from .book_record import BookRecord, canonical_isbn

# ============================================================
# 配置
# ============================================================
DANGDANG_SEARCH_URL = "https://search.dangdang.com/"
DANGDANG_PAGE_SIZE = 5
DANGDANG_TIMEOUT = 10

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

NO_PROXY = {"http": None, "https": None}


# ============================================================
# 源模块
# ============================================================
class DangdangSource:
    SOURCE_ID = "dangdang"
    SOURCE_NAME = "当当"

    def search(self, query: str, is_isbn: bool = False) -> List[BookRecord]:
        if is_isbn:
            clean = canonical_isbn(query)
            if clean:
                return self._search_products(clean)
            return []
        return self._search_products(query)

    def _search_products(self, query: str) -> List[BookRecord]:
        """搜索结果页（page_index=1 确保有产品 HTML）"""
        try:
            resp = requests.get(
                DANGDANG_SEARCH_URL,
                params={"key": query, "act": "input", "page_index": 1},
                headers=DEFAULT_HEADERS,
                timeout=DANGDANG_TIMEOUT,
                proxies=NO_PROXY,
            )
            if resp.status_code != 200:
                return []

            html = resp.content.decode("gbk", errors="replace")
            return self._parse_search_results(html)
        except Exception:
            return []

    def _parse_search_results(self, html: str) -> List[BookRecord]:
        """解析搜索结果 li.line1 容器"""
        products = re.findall(
            r'<li[^>]*class="line1"[^>]*id="p(\d+)"[^>]*>(.*?)</li>',
            html, re.DOTALL
        )
        if not products:
            return []

        results = []
        seen = set()
        for pid, item_html in products[:DANGDANG_PAGE_SIZE]:
            if pid in seen:
                continue
            seen.add(pid)
            record = self._parse_item(pid, item_html)
            if record:
                results.append(record)
        return results

    def _parse_item(self, pid: str, item_html: str) -> Optional[BookRecord]:
        """解析单个产品"""
        # Title: from pic link title attribute
        title = ""
        url = ""
        m = re.search(r'<a[^>]*class="pic"[^>]*title="([^"]*)"', item_html)
        if m:
            title = m.group(1).strip()
            url_m = re.search(r'href="([^"]*)"', m.group(0))
            if url_m:
                url = url_m.group(1)
                if url.startswith("//"):
                    url = "https:" + url

        if not title:
            m = re.search(r'<p[^>]*class="name"[^>]*>.*?<a[^>]*title="([^"]*)"', item_html, re.DOTALL)
            if m:
                title = m.group(1).strip()

        if not title:
            return None

        # Clean title: truncate marketing text after Chinese colon
        if "：" in title:
            parts = title.split("：", 1)
            ads_kw = ["获奖", "代表作", "推荐", "畅销", "经典", "科幻基石", "雨果奖", "银河奖"]
            if any(kw in parts[1][:20] for kw in ads_kw):
                title = parts[0].strip()

        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
            title=title,
            url=url,
            raw_id=pid,
        )
        record.identifiers["dangdang_id"] = pid

        # Cover
        m = re.search(r'<img[^>]*src=[\'\"]([^\'\"]+)[\'\"]', item_html)
        if m:
            cover = m.group(1)
            if cover.startswith("//"):
                cover = "https:" + cover
            record.cover_url = cover

        # Price
        m = re.search(r'<span[^>]*class="search_now_price"[^>]*>(.*?)</span>', item_html)
        if m:
            try:
                price = m.group(1).strip().replace("\u00a5", "").replace("\uffe5", "")
                record.identifiers["dd_price"] = str(float(price))
            except (ValueError, TypeError):
                pass

        # Language
        if re.search(r'[\u4e00-\u9fff]', record.title):
            record.language = "\u4e2d\u6587"

        return record
