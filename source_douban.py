"""
MultiSource - 豆瓣数据源
从 book.douban.com 爬取书籍元数据。
"""
import random
import re
import time
import hashlib
from typing import List, Optional
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from lxml import etree

from .book_record import BookRecord


# ============================================================
# 配置
# ============================================================
DOUBAN_SEARCH_URL = "https://www.douban.com/search"
DOUBAN_BASE = "https://book.douban.com/"
DOUBAN_BOOK_CAT = "1001"
DOUBAN_BOOK_URL_PATTERN = re.compile(r".*/subject/(\d+)/?")

# 搜索结果：一次拉 1 页（15 条），并发获取详情
DOUBAN_SEARCH_PAGES = 1
DOUBAN_PAGE_SIZE = 5
DOUBAN_MAX_DETAIL_WORKERS = 8
DOUBAN_TIMEOUT = 8  # 秒

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Encoding": "gzip, deflate",
    "Referer": DOUBAN_BASE,
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 豆瓣是国内站点，必须绕过容器代理直连，否则豆瓣检测到代理 IP 会跳转登录页
NO_PROXY = {"http": None, "https": None}


# ============================================================
# 源模块
# ============================================================
class DoubanSource:
    SOURCE_ID = "douban"
    SOURCE_NAME = "豆瓣"

    def __init__(self, cookie: str = ""):
        self._parser = DoubanBookHtmlParser()
        self._cookie = cookie.strip() if cookie else ""

    def _get_headers(self) -> dict:
        """返回请求头，如果有登录 cookie 则注入"""
        headers = dict(DEFAULT_HEADERS)
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers
        """搜索书籍"""
        return self._search(query)

    def _search(self, query: str) -> List[BookRecord]:
        """执行搜索，拉取结果页并并发获取详情"""
        all_records = []

        for page_idx in range(DOUBAN_SEARCH_PAGES):
            start = page_idx * DOUBAN_PAGE_SIZE
            entries = self._fetch_search_page(query, start)

            if not entries and page_idx == 0:
                return []

            # 并发获取详情
            records = self._fetch_details_concurrent([e["url"] for e in entries])

            # 用搜索结果页的基本信息补全（当详情失败时）
            url_to_basic = {e["url"]: e for e in entries}
            for rec in records:
                basic = url_to_basic.get(rec.url, {})
                if not rec.title and basic.get("title"):
                    rec.title = basic["title"]
                if not rec.authors and basic.get("author"):
                    rec.authors = [basic["author"]]
                if not rec.publisher and basic.get("publisher"):
                    rec.publisher = basic["publisher"]
                if not rec.published_date and basic.get("year"):
                    rec.published_date = basic["year"]
                if not rec.cover_url and basic.get("cover"):
                    rec.cover_url = basic["cover"]
                if not rec.rating and basic.get("rating"):
                    rec.rating = basic["rating"]

            # 如果详情全失败，至少返回列表页解析出的基本信息
            if not records:
                for e in entries:
                    rec = BookRecord(
                        source_id=self.SOURCE_ID,
                        source_name=self.SOURCE_NAME,
                        url=e["url"],
                        title=e.get("title", ""),
                        authors=[e["author"]] if e.get("author") else [],
                        publisher=e.get("publisher", ""),
                        published_date=e.get("year", ""),
                        cover_url=e.get("cover", ""),
                        rating=e.get("rating", 0.0),
                    )
                    if rec.title:
                        records.append(rec)

            all_records.extend(records)

            if len(entries) < DOUBAN_PAGE_SIZE:
                break

            time.sleep(random.uniform(0.3, 0.8))

        return all_records

    def _fetch_search_page(self, query: str, start: int) -> List[dict]:
        """获取豆瓣搜索结果页，提取书籍 URL 和基本信息"""
        try:
            params = {"cat": DOUBAN_BOOK_CAT, "q": query}
            if start > 0:
                params["start"] = start

            resp = requests.get(
                DOUBAN_SEARCH_URL, params=params,
                headers=self._get_headers(), timeout=DOUBAN_TIMEOUT,
                proxies=NO_PROXY,
            )
            if resp.status_code not in (200, 201):
                return []

            html = etree.HTML(resp.content)
            return self._parse_search_list(html)

        except Exception as e:
            print(f"[Douban] 搜索结果页获取失败: {e}")
            return []

    def _parse_search_list(self, tree) -> List[dict]:
        """解析搜索结果列表，提取 URL、标题、作者、出版社、年份、封面、评分"""
        results = []
        seen = set()

        for item in tree.xpath('//div[@class="result"]')[:DOUBAN_PAGE_SIZE]:
            # URL
            link = item.xpath('.//a[@class="nbg"]')
            if not link:
                link = item.xpath('.//a[contains(@href,"subject")]')
            if not link:
                continue

            href = link[0].attrib.get("href", "")
            url = self._calc_url(href)
            if not url or url in seen:
                continue
            seen.add(url)

            # 标题
            title = ""
            title_elem = item.xpath('.//div[@class="title"]//a')
            if title_elem:
                title = self._text(title_elem[0])
            if not title:
                # 备用：img alt
                img = link[0].xpath(".//img")
                if img:
                    title = img[0].attrib.get("alt", "").strip()

            # 作者 / 出版社 / 年份
            author = ""
            publisher = ""
            year = ""
            info_elem = item.xpath('.//span[@class="subject-cast"]')
            if info_elem:
                text = self._text(info_elem[0])
                parts = [p.strip() for p in text.split("/")]
                if parts:
                    author = parts[0]
                if len(parts) >= 2:
                    publisher = parts[1]
                if len(parts) >= 3:
                    year = parts[2]

            # 评分
            rating = 0.0
            rating_elem = item.xpath('.//span[@class="rating_nums"]')
            if rating_elem:
                try:
                    rating = float(self._text(rating_elem[0])) / 2
                except (ValueError, TypeError):
                    pass

            # 封面
            cover = ""
            img = link[0].xpath(".//img")
            if img:
                cover = img[0].attrib.get("src", "")

            results.append({
                "url": url,
                "title": title,
                "author": author,
                "publisher": publisher,
                "year": year,
                "rating": rating,
                "cover": cover,
            })

        return results

    def _fetch_details_concurrent(self, urls: List[str]) -> List[BookRecord]:
        """并发获取书籍详情"""
        records = []
        if not urls:
            return records

        with ThreadPoolExecutor(max_workers=DOUBAN_MAX_DETAIL_WORKERS) as pool:
            futures = {pool.submit(self._fetch_book_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result(timeout=DOUBAN_TIMEOUT + 2)
                    if record:
                        records.append(record)
                except Exception as e:
                    print(f"[Douban] 详情获取异常 {url}: {e}")

        return records

    def _fetch_book_detail(self, url: str) -> Optional[BookRecord]:
        """获取单本书的详情"""
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=DOUBAN_TIMEOUT,
                                proxies=NO_PROXY)
            if resp.status_code not in (200, 201):
                return None

            return self._parser.parse(url, resp.content.decode("utf-8", errors="ignore"))

        except Exception as e:
            print(f"[Douban] 书籍详情获取失败 {url}: {e}")
            return None

    def _calc_url(self, href: str) -> Optional[str]:
        """从豆瓣搜索结果链接中提取真实 URL"""
        try:
            query_str = urlparse(href).query
            params = dict(item.split("=", 1) for item in query_str.split("&"))
            url = unquote(params.get("url", ""))
            if DOUBAN_BOOK_URL_PATTERN.match(url):
                return url
        except Exception:
            pass
        return None

    @staticmethod
    def _text(elem) -> str:
        """安全提取元素文本"""
        if elem is None:
            return ""
        text = elem.text or ""
        if not text:
            # 尝试合并所有子节点文本
            text = "".join(elem.itertext()).strip()
        return text.strip()


# ============================================================
# HTML 解析器
# ============================================================
class DoubanBookHtmlParser:
    def __init__(self):
        self.id_pattern = DOUBAN_BOOK_URL_PATTERN
        self.date_pattern = re.compile(r"(\d{4})-(\d+)")
        self.tag_pattern = re.compile(r"criteria = '(.+)'")

    def parse(self, url: str, html_content: str) -> Optional[BookRecord]:
        try:
            tree = etree.HTML(html_content)

            record = BookRecord(
                source_id=DoubanSource.SOURCE_ID,
                source_name=DoubanSource.SOURCE_NAME,
                url=url,
            )

            # ID
            id_match = self.id_pattern.match(url)
            if id_match:
                record.raw_id = id_match.group(1)

            # 标题
            title_elem = tree.xpath("//span[@property='v:itemreviewed']")
            record.title = self._get_text(title_elem)

            # 封面
            cover_elem = tree.xpath("//a[@class='nbg']")
            if cover_elem:
                cover = cover_elem[0].attrib.get("href", "")
                if cover and "update_image" not in cover:
                    record.cover_url = cover

            # 评分
            rating_elem = tree.xpath("//strong[@property='v:average']")
            rating_text = self._get_text(rating_elem, "0")
            try:
                record.rating = float(rating_text) / 2
            except (ValueError, TypeError):
                pass

            # 元数据字段
            elements = tree.xpath("//span[@class='pl']")
            for elem in elements:
                text = self._get_text(elem)

                if text.startswith("作者") or text.startswith("译者"):
                    author_links = elem.xpath("..//a")
                    for alink in author_links:
                        href = alink.attrib.get("href", "")
                        name = self._get_text([alink])
                        if name:
                            if "/author" in href or "/search" in href:
                                if text.startswith("译者"):
                                    record.translators.append(name)
                                else:
                                    record.authors.append(name)

                elif text.startswith("出版社"):
                    record.publisher = self._get_tail(elem)

                elif text.startswith("副标题"):
                    subtitle = self._get_tail(elem)
                    if subtitle:
                        record.subtitle = subtitle
                        record.title = f"{record.title}: {subtitle}" if record.title else subtitle

                elif text.startswith("出版年"):
                    date_str = self._get_tail(elem)
                    if date_str:
                        date_match = self.date_pattern.fullmatch(date_str)
                        if date_match:
                            record.published_date = f"{date_match.group(1)}-{date_match.group(2)}-01"
                        else:
                            record.published_date = date_str

                elif text.startswith("丛书"):
                    sibling = elem.getnext()
                    if sibling is not None:
                        record.series = self._get_text([sibling])

                elif text.startswith("ISBN"):
                    isbn = self._get_tail(elem)
                    if isbn:
                        record.isbn = isbn.strip()

            # 简介
            intro_elem = tree.xpath("//div[@id='link-report']//div[@class='intro']")
            if intro_elem:
                record.description = etree.tostring(
                    intro_elem[-1], encoding="utf-8"
                ).decode("utf-8", errors="ignore").strip()

            # 标签
            tag_elements = tree.xpath("//a[contains(@class, 'tag')]")
            if tag_elements:
                record.tags = [self._get_text([t]) for t in tag_elements]
            else:
                record.tags = self._parse_script_tags(html_content)

            # 去重
            record.tags = self._dedup_case_insensitive(record.tags)

            # 语言推断
            if record.title and re.search(r'[\u4e00-\u9fff]', record.title):
                record.language = "中文"

            return record

        except Exception as e:
            print(f"[Douban] 解析错误 {url}: {e}")
            return None

    def _parse_script_tags(self, html_content: str) -> list:
        """从页面脚本中解析豆瓣标签"""
        match = self.tag_pattern.findall(html_content)
        if match:
            tags = []
            for tag_str in match[0].split("|"):
                tag = tag_str.strip()
                if tag and tag.startswith("7:"):
                    tags.append(tag.replace("7:", ""))
            return tags
        return []

    def _dedup_case_insensitive(self, tags: list) -> list:
        """标签去重"""
        seen = set()
        result = []
        for t in tags:
            lower = t.casefold()
            if lower not in seen:
                seen.add(lower)
                result.append(t)
        return result

    def _get_text(self, elements, default: str = "") -> str:
        """安全获取元素文本"""
        if elements is None or len(elements) == 0:
            return default
        elem = elements[0]
        if elem.text:
            return elem.text.strip()
        return default

    def _get_tail(self, element, default: str = "") -> str:
        """获取标签的 tail 文本"""
        if element is None:
            return default
        if element.tail:
            text = element.tail.strip()
            if text:
                return text
        next_elem = element.getnext()
        if next_elem is not None:
            return self._get_text([next_elem], default)
        return default
