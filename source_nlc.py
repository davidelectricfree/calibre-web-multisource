"""
MultiSource - 国家图书馆（NLC）数据源
通过 NLC OPAC (http://opac.nlc.cn/F) 查询中文书目元数据。
修复了 Session URL 生命周期、详情页跳转、表格解析等核心问题。
"""
import re
import time
import random
import urllib.parse
import urllib.request
import ssl
import hashlib
from typing import List, Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

from lxml import etree, html as lhtml

from .book_record import BookRecord, canonical_isbn

# 尝试导入 clc_parser
try:
    from .clc_parser import Parser as CLCParser
except ImportError:
    CLCParser = None
    print("[NLC] 无法导入 clc_parser，中图分类号解析将跳过")


# ============================================================
# 配置
# ============================================================
NLC_BASE_URL = "http://opac.nlc.cn/F"
NLC_MAX_RESULTS = 10       # 标题搜索最多返回 N 条
NLC_CONCURRENCY = 2        # 并发获取详情

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Host": "opac.nlc.cn",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# NLC 搜索 URL 模板
SEARCH_ISBN = (
    NLC_BASE_URL
    + "?func=find-b&find_code=ISB&request={isbn}&local_base=NLC01"
    + "&filter_code_1=WLN&filter_request_1=&filter_code_2=WYR&filter_request_2="
    + "&filter_code_3=WYR&filter_request_3=&filter_code_4=WFM&filter_request_4=&filter_code_5=WSL&filter_request_5="
)
SEARCH_TITLE = (
    NLC_BASE_URL
    + "?func=find-b&find_code=WTP&request={title}&local_base=NLC01"
    + "&filter_code_1=WLN&filter_request_1=&filter_code_2=WYR&filter_request_2="
    + "&filter_code_3=WYR&filter_request_3=&filter_code_4=WFM&filter_request_4=&filter_code_5=WSL&filter_request_5="
)


# ============================================================
# 工具函数
# ============================================================
def _sleep():
    time.sleep(random.randint(300, 800) / 1000)


def _get_ssl_context():
    """获取 SSL 上下文（兼容 Python 3.13+，避免 _create_unverified_context FutureWarning）"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch(url: str) -> Optional[str]:
    """通用 HTTP GET，带重试"""
    for attempt in range(3):
        try:
            _sleep()
            ctx = _get_ssl_context()
            req = urllib.request.Request(url, headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=15, context=ctx)
            content = resp.read()
            # 尝试多种编码
            for enc in ("utf-8", "gbk", "gb2312"):
                try:
                    return content.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return content.decode("utf-8", errors="ignore")
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
                continue
            print(f"[NLC] 请求失败 {url}: {e}")
            return None


def _get_session_url() -> str:
    """
    获取 NLC OPAC 的动态 Session URL。
    NLC 在首次访问时返回一个动态路径（如 /F/XXX-XXXXX），
    后续所有请求必须基于此路径。
    """
    try:
        content = _fetch(NLC_BASE_URL)
        if not content:
            return NLC_BASE_URL

        patterns = [
            r"http://opac\.nlc\.cn:80/F/[^\s\"'>]*",
            r"http://opac\.nlc\.cn/F/[^\s\"'>]*",
            r"//opac\.nlc\.cn/F/[^\s\"'>]*",
        ]
        for pat in patterns:
            m = re.search(pat, content)
            if m:
                url = m.group(0)
                if url.startswith("//"):
                    url = "http:" + url
                print(f"[NLC] Session URL: {url}")
                return url

        return NLC_BASE_URL
    except Exception as e:
        print(f"[NLC] 获取 Session URL 失败: {e}")
        return NLC_BASE_URL


# ============================================================
# 源模块
# ============================================================
class NLCSource:
    SOURCE_ID = "nlc"
    SOURCE_NAME = "国家图书馆"

    def __init__(self):
        self.session_url = NLC_BASE_URL  # 惰性初始化
        self._parser = NLCHtmlParser()
        self._session_initialized = False

    def _ensure_session(self):
        """确保 Session URL 已初始化"""
        if not self._session_initialized:
            self.session_url = _get_session_url()
            self._session_initialized = True

    def search(self, query: str, is_isbn: bool = False) -> List[BookRecord]:
        self._ensure_session()

        if is_isbn:
            return self._search_by_isbn(query)

        # 标题搜索：先查结果列表，再逐条获取详情
        return self._search_by_title(query)

    def _search_by_isbn(self, isbn: str) -> List[BookRecord]:
        """ISBN 精确搜索"""
        clean = canonical_isbn(isbn)
        url = SEARCH_ISBN.format(isbn=clean if clean else urllib.parse.quote(isbn))
        print(f"[NLC] ISBN 搜索: {url}")

        content = _fetch(url)
        if not content:
            return []

        record = self._parser.parse_book_detail(content, isbn=clean, source_url=url)
        return [record] if record else []

    def _search_by_title(self, title: str) -> List[BookRecord]:
        """标题搜索"""
        url = SEARCH_TITLE.format(title=urllib.parse.quote(title))
        print(f"[NLC] 标题搜索: {url}")

        content = _fetch(url)
        if not content:
            return []

        # 解析搜索结果列表
        items = self._parse_search_results(content)
        if not items:
            print("[NLC] 搜索结果为空")
            return []

        # 限制数量
        items = items[:NLC_MAX_RESULTS]
        print(f"[NLC] 找到 {len(items)} 个搜索结果")

        # 并发获取详情
        records = []
        with ThreadPoolExecutor(max_workers=NLC_CONCURRENCY) as pool:
            futures = {
                pool.submit(self._fetch_detail, url, title): (url, title)
                for _, url in items
            }
            for future in as_completed(futures):
                try:
                    record = future.result(timeout=30)
                    if record:
                        records.append(record)
                except Exception as e:
                    print(f"[NLC] 详情获取异常: {e}")

        return records

    def _parse_search_results(self, html_content: str) -> List[tuple]:
        """解析搜索结果列表，返回 [(title, url), ...]"""
        items = []
        try:
            tree = lhtml.fromstring(html_content)

            # 方法 1: class="itemtitle" 的 div
            item_divs = tree.xpath('//div[contains(@class, "itemtitle")]')
            for div in item_divs:
                links = div.xpath(".//a")
                if links:
                    title = links[0].text_content().strip()
                    href = links[0].get("href", "")
                    if title and href:
                        full_url = self._normalize_url(href)
                        if full_url:
                            items.append((title, full_url))

            # 方法 2: 表格行中的链接
            if not items:
                table_links = tree.xpath('//table//a[contains(@href, "/F/")]')
                for link in table_links:
                    title = link.text_content().strip()
                    href = link.get("href", "")
                    if title and href and "func=find-b" in href:
                        full_url = self._normalize_url(href)
                        if full_url:
                            items.append((title, full_url))

        except Exception as e:
            print(f"[NLC] 解析搜索结果失败: {e}")

        return items

    def _normalize_url(self, href: str) -> Optional[str]:
        """标准化 URL"""
        if not href:
            return None
        href = href.strip()
        if href.startswith("http://"):
            return href
        elif href.startswith("//"):
            return "http:" + href
        elif href.startswith("/"):
            return "http://opac.nlc.cn" + href
        elif href.startswith("F/"):
            return "http://opac.nlc.cn/" + href
        elif href.startswith("?"):
            return "http://opac.nlc.cn/F" + href
        return None

    def _fetch_detail(self, url: str, fallback_title: str = "") -> Optional[BookRecord]:
        """获取单本书详情"""
        try:
            content = _fetch(url)
            if not content:
                return None

            record = self._parser.parse_book_detail(content, source_url=url)
            if record:
                if not record.title or record.title == "未知标题":
                    record.title = fallback_title
                return record
            return None

        except Exception as e:
            print(f"[NLC] 详情获取失败 {url}: {e}")
            return None


# ============================================================
# HTML 解析器
# ============================================================
class NLCHtmlParser:
    """NLC OPAC HTML 解析器 — 专为 td1 表格结构优化"""

    # 字段映射：NLC 标签 → 内部字段
    FIELD_MAP = {
        "题名与责任":         "title_and_author",
        "题名与责任者":       "title_and_author",
        "题名":               "title_only",
        "书名":               "title_only",
        "著者":               "author",
        "作者":               "author",
        "出版项":             "pub_info",
        "出版发行项":         "pub_info",
        "出版发行":           "pub_info",
        "出版":               "pub_info",
        "载体形态项":         "physical",
        "载体形态":           "physical",
        "丛编项":             "series",
        "丛编":               "series",
        "丛书":               "series",
        "一般附注":           "note",
        "内容提要":           "abstract",
        "摘要":               "abstract",
        "主题":               "subject",
        "主题词":             "subject",
        "中图分类号":         "clc",
        "分类号":             "clc",
        "ISBN":               "isbn",
        "国际标准书号":       "isbn",
        "语种":               "language",
        "语言":               "language",
    }

    def parse_book_detail(self, html_content: str, isbn: str = "",
                          source_url: str = "") -> Optional[BookRecord]:
        """
        解析书籍详情页。
        NLC 的详情页使用 id="td" 的表格，通过 class="td1" 的 td 做 key-value 布局。
        """
        try:
            # 检测：如果不是详情页（是搜索结果页或跳转页），尝试提取详情链接
            tree = lhtml.fromstring(html_content)

            # 检查是否需要跳转
            if "书目检索" in html_content[:500] and "func=direct" not in (source_url or ""):
                detail_url = self._extract_detail_redirect(tree, source_url)
                if detail_url and detail_url != source_url:
                    print(f"[NLC] 重定向到详情页: {detail_url}")
                    content2 = _fetch(detail_url)
                    if content2:
                        return self.parse_book_detail(content2, isbn=isbn, source_url=detail_url)

            # 提取表格数据
            data = self._extract_table_data(tree)

            if not data:
                print("[NLC] 未提取到表格数据")
                return None

            return self._build_record(data, isbn, source_url)

        except Exception as e:
            print(f"[NLC] 解析异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _extract_detail_redirect(self, tree, source_url: str) -> Optional[str]:
        """从搜索结果页提取详情页跳转 URL"""
        # func=direct 链接
        direct_links = tree.xpath('//a[contains(@href, "func=direct")]')
        if direct_links:
            href = direct_links[0].get("href", "")
            return self._normalize_detail_url(href, source_url)

        # 查找"书目检索"文本链接
        link_texts = tree.xpath('//a[contains(text(), "书目记录")]')
        if link_texts:
            href = link_texts[0].get("href", "")
            return self._normalize_detail_url(href, source_url)

        return None

    def _normalize_detail_url(self, href: str, source_url: str = "") -> Optional[str]:
        if not href:
            return None
        href = href.strip()
        if href.startswith("http://"):
            return href
        elif href.startswith("/"):
            return "http://opac.nlc.cn" + href
        elif href.startswith("?"):
            return "http://opac.nlc.cn/F" + href
        return None

    def _extract_table_data(self, tree) -> Dict[str, str]:
        """从 id='td' 表格 + class='td1' 的 td 中提取键值对"""
        data: Dict[str, str] = {}

        # 主表格
        table = tree.xpath('//table[@id="td"]')
        if table:
            table = table[0]
            rows = table.xpath(".//tr")

            current_key = ""
            for row in rows:
                cols = row.xpath('.//td[@class="td1"]')
                if len(cols) >= 2:
                    key_text = "".join(cols[0].xpath(".//text()")).strip()
                    value_text = "".join(cols[1].xpath(".//text()")).strip()

                    # 匹配已知字段名
                    matched_field = None
                    for nlc_label, field_name in self.FIELD_MAP.items():
                        if key_text.startswith(nlc_label):
                            matched_field = field_name
                            break

                    if matched_field:
                        if matched_field in data:
                            data[matched_field] += "\n" + value_text
                        else:
                            data[matched_field] = value_text
                        current_key = matched_field
                    elif key_text:
                        data[key_text] = value_text
                        current_key = key_text
                    elif current_key and value_text:
                        # 续行
                        data[current_key] += "\n" + value_text

        # 备选：直接找 class='td1' 的元素
        if not data:
            td_elems = tree.xpath('//td[@class="td1"]')
            for i in range(0, len(td_elems) - 1, 2):
                key_text = "".join(td_elems[i].xpath(".//text()")).strip()
                value_text = "".join(td_elems[i + 1].xpath(".//text()")).strip() if i + 1 < len(td_elems) else ""

                matched_field = None
                for nlc_label, field_name in self.FIELD_MAP.items():
                    if key_text.startswith(nlc_label):
                        matched_field = field_name
                        break
                if matched_field and matched_field not in data:
                    data[matched_field] = value_text

        print(f"[NLC] 提取到 {len(data)} 个字段")
        return data

    def _build_record(self, data: Dict[str, str], isbn: str, source_url: str) -> BookRecord:
        """从提取的字段构建 BookRecord"""
        record = BookRecord(
            source_id=NLCSource.SOURCE_ID,
            source_name=NLCSource.SOURCE_NAME,
            url=source_url or "",
        )

        # 标题
        title_text = data.get("title_and_author", "") or data.get("title_only", "")
        if title_text:
            # 解析 "题名与责任者: 标题 / 作者" 格式
            parts = re.split(r"\s*[/／]\s*", title_text, 1)
            record.title = parts[0].strip()
            # 移除方括号内容（如 [专著]）
            record.title = re.sub(r'\s*[\[【].*?[\]】]\s*', ' ', record.title).strip()
            # 若有冒号分隔的副标题
            title_parts = re.split(r"[:：]", record.title, 1)
            if len(title_parts) > 1:
                record.title = title_parts[0].strip()
                record.subtitle = title_parts[1].strip()
        else:
            record.title = "未知标题"

        # 作者
        author_text = data.get("author", "")
        if author_text:
            # 解析 "著者: 作者1; 作者2; 作者3" 格式
            author_text = re.sub(r"^著者[：:]?\s*", "", author_text)
            for sep in (" ; ", ";", "；", " / ", "/", "  "):
                if sep in author_text:
                    record.authors = [a.strip() for a in author_text.split(sep) if a.strip()]
                    break
            if not record.authors:
                record.authors = [author_text.strip()]

        # 清理作者名中的后缀
        record.authors = [
            re.sub(r'\s*(?:著|编|等|编写|主编|编著|编译|译|绘|注|校|撰|合著)\s*$', '', a).strip()
            for a in record.authors if a.strip()
        ]
        if not record.authors:
            record.authors = ["未知作者"]

        # 如果 title_and_author 中有作者信息而 author 字段为空
        if not author_text:
            ta = data.get("title_and_author", "")
            if ta:
                parts = re.split(r"\s*[/／]\s*", ta, 1)
                if len(parts) > 1:
                    author_part = parts[1].strip().rstrip("著编译绘")
                    record.authors = [author_part.strip()]

        # 出版信息
        pub_info = data.get("pub_info", "")
        if pub_info:
            # "出版地 : 出版社, 出版年" 格式
            pub_match = re.search(r"[:：]\s*(.+?)(?:[,，]|$)", pub_info)
            if pub_match:
                record.publisher = pub_match.group(1).strip()
            else:
                record.publisher = pub_info.split(",")[0].split("，")[0].strip()

            # 年份
            year_match = re.search(r"(\d{4})", pub_info)
            if year_match:
                record.published_date = year_match.group(1)

        # ISBN
        isbn_text = data.get("isbn", "")
        if isbn_text:
            record.isbn = canonical_isbn(isbn_text)
        elif isbn:
            record.isbn = canonical_isbn(isbn)

        # 简介
        record.description = data.get("abstract", "")

        # 丛书
        record.series = data.get("series", "")

        # 语言
        lang = data.get("language", "")
        if lang and ("chi" in lang.lower() or "中文" in lang):
            record.language = "中文"
        elif lang:
            record.language = lang.strip()

        # 载体形态（页数等）
        physical = data.get("physical", "")
        if physical:
            pages_match = re.search(r"(\d+)\s*页", physical)
            if pages_match:
                try:
                    record.pages = int(pages_match.group(1))
                except ValueError:
                    pass

        # 主题标签
        subject = data.get("subject", "")
        if subject:
            for sep in (" ; ", ";", "；", " -- ", " / "):
                if sep in subject:
                    record.tags = [s.strip() for s in subject.split(sep) if s.strip()]
                    break
            if not record.tags:
                record.tags = [subject.strip()]

        # 中图分类号
        clc = data.get("clc", "")
        if clc and CLCParser:
            try:
                # 清洗分类号
                parsed = CLCParser.parse(clc)
                if parsed:
                    for code, path in parsed.items():
                        if path:
                            record.clc_code = path[-1] if path else code
                            if len(path) >= 3:
                                record.tags.append(path[1])  # 二级分类作为标签
                            if len(path) >= 2:
                                record.tags.append(path[0])  # 一级分类作为标签
                            break
            except Exception as e:
                print(f"[NLC] 中图分类号解析失败: {e}")
                record.clc_code = clc.strip()

        # 唯一 ID
        if record.isbn:
            record.raw_id = record.isbn
        else:
            record.raw_id = hashlib.md5(
                (record.title + source_url).encode("utf-8")
            ).hexdigest()[:16]

        return record
