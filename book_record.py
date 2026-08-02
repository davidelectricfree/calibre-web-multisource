"""
MultiSource - 标准化书籍数据结构
定义插件内部使用的统一数据格式和各源合并后的结果格式
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional


def normalize_text(text: str) -> str:
    """文本归一化：去标点、去空格、全角转半角、英文小写"""
    if not text:
        return ""
    text = text.strip()
    # 全角转半角
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(" ")
        else:
            result.append(ch)
    text = "".join(result)
    # 去标点和多余空格
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text.lower()


def canonical_isbn(isbn: str) -> str:
    """标准化 ISBN，统一转 ISBN-13"""
    if not isbn:
        return ""
    isbn = re.sub(r'[^0-9X]', '', isbn.upper())
    if len(isbn) == 10:
        # ISBN-10 → ISBN-13
        try:
            digits = [int(c) for c in isbn[:9]]
            prefix = [9, 7, 8]
            all_digits = prefix + digits
            total = sum((10 - i) * all_digits[i] for i in range(12))
            check = (10 - (total % 10)) % 10
            isbn = "".join(str(d) for d in all_digits) + str(check)
        except (ValueError, IndexError):
            return ""
    if len(isbn) != 13:
        return ""
    return isbn


@dataclass
class BookRecord:
    """来自单个数据源的标准化书籍元数据"""
    source_id: str
    source_name: str
    title: str = ""
    subtitle: str = ""
    authors: List[str] = field(default_factory=list)
    translators: List[str] = field(default_factory=list)
    publisher: str = ""
    published_date: str = ""   # YYYY-MM-DD 或 YYYY
    isbn: str = ""
    description: str = ""
    cover_url: str = ""
    rating: float = 0.0
    tags: List[str] = field(default_factory=list)
    series: str = ""
    language: str = ""
    pages: int = 0
    clc_code: str = ""         # NLC 特有：中图分类号
    url: str = ""
    raw_id: str = ""           # 源站内部 ID（如豆瓣 subject id）
    identifiers: Dict[str, str] = field(default_factory=dict)
    confidence: float = 1.0    # 该条记录的可信度 (0-1)

    def get_normalized_isbn(self) -> str:
        return canonical_isbn(self.isbn)

    def compute_fingerprint(self) -> str:
        """计算书籍指纹：SHA256(归一化标题 + 归一化作者 + 归一化出版社) 前16字符"""
        title = normalize_text(self.title)
        author = normalize_text(" ".join(sorted(self.authors)))
        publisher = normalize_text(self.publisher)
        fp = f"{title}|{author}|{publisher}"
        return hashlib.sha256(fp.encode("utf-8")).hexdigest()[:16]


@dataclass
class MergedBook:
    """多源合并后的书籍记录"""
    title: str = ""
    subtitle: str = ""
    authors: List[str] = field(default_factory=list)
    translators: List[str] = field(default_factory=list)
    publisher: str = ""
    published_date: str = ""
    isbn: str = ""
    description: str = ""
    cover_url: str = ""
    rating: float = 0.0
    tags: List[str] = field(default_factory=list)
    series: str = ""
    language: str = ""
    pages: int = 0
    clc_code: str = ""
    url: str = ""
    identifiers: Dict[str, str] = field(default_factory=dict)

    # 合并元信息
    sources: List[str] = field(default_factory=list)
    confidence: str = "high"    # high / medium / low
    merge_note: str = ""        # 低置信合并时给出说明
    field_sources: Dict[str, str] = field(default_factory=dict)
    source_records: List[BookRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "authors": self.authors,
            "translators": self.translators,
            "publisher": self.publisher,
            "published_date": self.published_date,
            "isbn": self.isbn,
            "description": self.description,
            "cover_url": self.cover_url,
            "rating": self.rating,
            "tags": self.tags,
            "series": self.series,
            "language": self.language,
            "pages": self.pages,
            "clc_code": self.clc_code,
            "url": self.url,
            "identifiers": self.identifiers,
            "sources": self.sources,
            "confidence": self.confidence,
            "merge_note": self.merge_note,
            "field_sources": self.field_sources,
        }
