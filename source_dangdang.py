"""
MultiSource - 当当网数据源
从 search.dangdang.com 和 product.dangdang.com 爬取书籍元数据。
搜索页（page_index=1）获取列表 + 封面，详情页获取作者/出版社。
"""
import re
import time
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .book_record import BookRecord, canonical_isbn

# ============================================================
# 配置
# ============================================================
DANGDANG_SEARCH_URL = "https://search.dangdang.com/"
DANGDANG_PAGE_SIZE = 5
DANGDANG_TIMEOUT = 10
DANGDANG_DETAIL_WORKERS = 3
DANGDANG_DETAIL_BUDGET = 4    # 详情抓取总超时（秒），不得超过搜索预算

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
            return self._search_by_isbn(query)
        return self._search_by_title(query)

    def _search_by_isbn(self, isbn_str: str) -> List[BookRecord]:
        clean = canonical_isbn(isbn_str)
        if clean:
            return self._search_products(clean)
        return []

    def _search_by_title(self, title: str) -> List[BookRecord]:
        return self._search_products(title)

    def _search_products(self, query: str) -> List[BookRecord]:
        """搜索并解析搜索结果 + 并发获取详情页"""
        try:
            # 使用 page_index=1 参数确保获取产品数据
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
            return self._parse_and_enrich(html)
        except Exception:
            return []

    def _parse_and_enrich(self, html: str) -> List[BookRecord]:
        """解析搜索结果列表，并发抓取详情页补充作者/出版社/ISBN"""
        # 提取产品列表
        products = re.findall(
            r'<li[^>]*class="line1"[^>]*id="p(\d+)"[^>]*>(.*?)</li>',
            html, re.DOTALL
        )
        if not products:
            return []

        # 先解析基本字段，再并发获取详情
        basic_records = []
        for pid, item_html in products[:DANGDANG_PAGE_SIZE]:
            record = self._parse_search_item(pid, item_html)
            if record:
                basic_records.append((pid, record))

        if not basic_records:
            return []

        # 并发获取详情页，但有时间预算
        enriched = []
        with ThreadPoolExecutor(max_workers=DANGDANG_DETAIL_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_product_detail, pid, record): (pid, record)
                for pid, record in basic_records
            }
            # 只等待预算时间内的结果，超时的跳过返回 basic
            deadline = time.time() + DANGDANG_DETAIL_BUDGET
            for future in futures:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    result = future.result(timeout=remaining)
                    if result:
                        enriched.append(result)
                except Exception:
                    pass

        # 添加未获取到详情的 basic records
        enriched_pids = {r.raw_id for r in enriched}
        enriched.extend([r for pid, r in basic_records if pid not in enriched_pids])
        return enriched

    def _parse_search_item(self, pid: str, item_html: str) -> Optional[BookRecord]:
        """从搜索结果 li 中提取基本字段"""
        # Title: from pic link title attribute
        title = ""
        url = ""
        m = re.search(r'<a[^>]*class="pic"[^>]*title="([^"]*)"', item_html)
        if m:
            title = m.group(1).strip()
            # URL from same link
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

        title = self._clean_title(title)

        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
            title=title,
            url=url,
            raw_id=pid,
        )
        record.identifiers["dangdang_id"] = pid

        # Cover
        m = re.search(r'<img[^>]*src=[\'"]([^\'"]+)[\'"]', item_html)
        if m:
            cover = m.group(1)
            if cover.startswith("//"):
                cover = "https:" + cover
            record.cover_url = cover

        # Price
        m = re.search(r'<span[^>]*class="search_now_price"[^>]*>(.*?)</span>', item_html)
        if m:
            try:
                price = m.group(1).strip().replace("¥", "").replace("￥", "")
                record.identifiers["dd_price"] = str(float(price))
            except (ValueError, TypeError):
                pass

        # Language
        if re.search(r'[\u4e00-\u9fff]', record.title):
            record.language = "中文"

        return record

    def _fetch_product_detail(self, pid: str, record: BookRecord = None) -> Optional[BookRecord]:
        """从产品详情页获取作者/出版社/ISBN"""
        try:
            url = f"https://product.dangdang.com/{pid}.html"
            resp = requests.get(url, headers=DEFAULT_HEADERS,
                               timeout=DANGDANG_TIMEOUT, proxies=NO_PROXY)
            if resp.status_code != 200:
                return record

            html = resp.content.decode("gbk", errors="replace")

            if record is None:
                return None

            # Author: from <a dd_name="作者">作者:<a>NAME</a>
            m = re.search(r'dd_name="作者"[^>]*>作者[:\s]*<a[^>]*>([^<]+)</a>', html)
            if m:
                author = self._clean_author(m.group(1))
                if author:
                    record.authors = [author]

            # Publisher: from meta description, stop at punctuation
            m = re.search(r'出版社[：:]\s*([^；。，\s<]+(?:[^；。<]+出版社)?)', html)
            if m:
                pub = m.group(1).rstrip("。，,：:")
                # Clean trailing fragments
                pub = re.sub(r'[\.。]*最新.*$', '', pub).strip()
                if pub:
                    record.publisher = pub

            # Publication date: from detail page 出版时间:2010年11月
            m = re.search(r'出版时间[：:]\s*(\d{4})\s*[年/\-]?\s*(\d{1,2})?\s*月?', html)
            if m:
                year = m.group(1)
                month = m.group(2) or "01"
                record.published_date = f"{year}-{month.zfill(2)}-01"

            # ISBN: try 国际标准书号 section
            m = re.search(r'国际标准书号ISBN[：:]\s*([\d\-]{10,17})', html)
            if m:
                isbn = canonical_isbn(m.group(1))
                if isbn and len(isbn) > 9:
                    record.isbn = isbn

            # ISBN: from detailed info area
            m = re.search(r'ISBN[：:]\s*(\d[\d\-]+)', html)
            if m:
                isbn = canonical_isbn(m.group(1))
                if isbn:
                    record.isbn = isbn

            return record
        except Exception:
            return record

    @staticmethod
    def _clean_title(title: str) -> str:
        """清理当当标题：截断营销文案"""
        # If Chinese colon present, truncate after first colon if followed by ads
        if "：" in title:
            parts = title.split("：", 1)
            if len(parts) == 2:
                # Check if second part looks like marketing
                ads_kw = ["获奖", "代表作", "推荐", "畅销", "经典", "科幻基石", "雨果奖", "银河奖"]
                if any(kw in parts[1][:20] for kw in ads_kw):
                    return parts[0].strip()
        return title

    def _clean_author(self, author: str) -> str:
        """清洗作者名称"""
        return re.sub(r'[，,。.\s\u3000\u00a0]*(著|编|译|主编|校|校译|编著|编译|编撰|等|绘|摄影)\s*$', '', author).strip()
