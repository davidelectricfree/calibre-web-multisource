"""
MultiSource - 当当网数据源
从 search.dangdang.com 爬取书籍元数据。
当当网搜索结果页面直接包含完整 HTML，无需 JS 渲染。
"""
import re
import time
from typing import List, Optional

import requests
from lxml import etree

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

# 当当是国内站点，直连不走代理
NO_PROXY = {"http": None, "https": None}

# 作者名称角色标注清洗（含全角空格\u3000、不间断空格\u00a0）
AUTHOR_CLEAN_PATTERN = re.compile(r'[，,。.\s\u3000\u00a0]*(著|编|译|主编|校|校译|编著|编译|编撰|等|绘|摄影|插图|绘制)\s*$')

# ISBN 提取
ISBN_PATTERN = re.compile(r'ISBN[：:]?\s*(\d[\d\-Xx]{9,16}\d)')


# ============================================================
# 源模块
# ============================================================
class DangdangSource:
    SOURCE_ID = "dangdang"
    SOURCE_NAME = "当当"

    def search(self, query: str, is_isbn: bool = False) -> List[BookRecord]:
        """搜索书籍"""
        if is_isbn:
            return self._search_by_isbn(query)
        return self._search_by_title(query)

    def _search_by_isbn(self, isbn_str: str) -> List[BookRecord]:
        """通过 ISBN 搜索"""
        clean = canonical_isbn(isbn_str)
        if clean:
            # 当当的 ISBN 搜索需要精确格式
            return self._fetch_search_results(clean)
        return []

    def _search_by_title(self, title: str) -> List[BookRecord]:
        """通过标题搜索"""
        return self._fetch_search_results(title)

    def _fetch_search_results(self, query: str) -> List[BookRecord]:
        """获取搜索结果并解析"""
        try:
            resp = requests.get(
                DANGDANG_SEARCH_URL,
                params={"key": query, "act": "input"},
                headers=DEFAULT_HEADERS,
                timeout=DANGDANG_TIMEOUT,
                proxies=NO_PROXY,
            )
            if resp.status_code != 200:
                return []

            # 当当使用 GBK 编码，解码后直接传给 etree
            html_str = resp.content.decode("gbk", errors="replace")
            return self._parse_search_results(html_str)

        except Exception as e:
            print(f"[Dangdang] 搜索失败: {e}")
            return []

    def _parse_search_results(self, html_str: str) -> List[BookRecord]:
        """解析搜索结果页"""
        try:
            tree = etree.HTML(html_str)
        except Exception:
            return []

        results = []
        seen_ids = set()

        # 当当搜索结果容器：li[contains(@class,"line")]
        items = tree.xpath('//li[contains(@class,"line")]')
        for item in items[:DANGDANG_PAGE_SIZE]:
            record = self._parse_item(item, seen_ids)
            if record:
                results.append(record)

        return results

    def _parse_item(self, item, seen_ids: set) -> Optional[BookRecord]:
        """解析单个搜索结果项"""
        # 获取产品 ID
        item_id = item.attrib.get("id", "").replace("p", "")
        if item_id in seen_ids:
            return None

        # 名称和链接
        pic_link = item.xpath('.//a[@class="pic"]')
        name_link = item.xpath('.//p[@class="name"]//a')

        title = ""
        url = ""
        if pic_link:
            title = pic_link[0].attrib.get("title", "").strip()
            url = pic_link[0].attrib.get("href", "")
        if not title and name_link:
            title = name_link[0].attrib.get("title", "").strip()
            if not title:
                title = (name_link[0].text or "").strip()
        if not url and name_link:
            url = name_link[0].attrib.get("href", "")

        if not title:
            return None

        # 补全 URL
        if url and url.startswith("//"):
            url = "https:" + url
        elif url and not url.startswith("http"):
            url = "https:" + url

        if item_id:
            seen_ids.add(item_id)

        record = BookRecord(
            source_id=self.SOURCE_ID,
            source_name=self.SOURCE_NAME,
            title=title,
            url=url,
        )

        # 封面
        cover_imgs = item.xpath('.//a[@class="pic"]//img')
        if cover_imgs:
            cover = cover_imgs[0].attrib.get("data-original", "")
            if not cover:
                cover = cover_imgs[0].attrib.get("src", "")
            if cover and cover.startswith("//"):
                cover = "https:" + cover
            record.cover_url = cover

        # 详情：作者/出版社/出版日期
        detail_elem = item.xpath('.//p[@class="detail"]')
        if detail_elem:
            detail_text = "".join(detail_elem[0].itertext()).strip()
            parts = [p.strip() for p in detail_text.split("/")]
            if len(parts) >= 1:
                record.authors = [self._clean_author(parts[0])]
            if len(parts) >= 2:
                record.publisher = parts[1]
            if len(parts) >= 3:
                date_str = parts[2]
                record.published_date = self._normalize_date(date_str)

        # 价格（可选）
        price_elem = item.xpath('.//span[@class="search_now_price"]/text()')
        if price_elem:
            try:
                price_text = price_elem[0].strip().replace("¥", "").replace("￥", "")
                price_val = float(price_text)
                if price_val > 0:
                    record.identifiers["dd_price"] = str(price_val)
            except (ValueError, TypeError):
                pass

        # 评分
        star_elem = item.xpath('.//p[contains(@class,"search_star")]//span[@class="level"]')
        if star_elem:
            star_text = "".join(star_elem[0].itertext()).strip()
            try:
                # 当当评分通常是 0-5 星
                record.rating = float(star_text)
            except (ValueError, TypeError):
                pass

        # ISBN 从标题或详情中提取
        isbn_match = ISBN_PATTERN.search(title)
        if isbn_match:
            record.isbn = canonical_isbn(isbn_match.group(1))
        elif detail_elem:
            detail_text = "".join(detail_elem[0].itertext())
            isbn_match = ISBN_PATTERN.search(detail_text)
            if isbn_match:
                record.isbn = canonical_isbn(isbn_match.group(1))

        # 语言推断（中文书名 → 中文）
        if record.title and re.search(r'[\u4e00-\u9fff]', record.title):
            record.language = "中文"

        record.raw_id = item_id
        if item_id:
            record.identifiers["dangdang_id"] = item_id

        return record

    def _clean_author(self, author: str) -> str:
        """清洗作者名称，去掉角色标注"""
        return AUTHOR_CLEAN_PATTERN.sub("", author).strip()

    def _normalize_date(self, date_str: str) -> str:
        """标准化日期格式"""
        return normalize_date(date_str)
