"""
MultiSource - 书籍去重与匹配合并引擎

三层防线：
  第一层：ISBN 精确匹配（最高优先级，ISBN-10/13 自动归一化）
  第二层：复合指纹匹配（SHA256 归一化标题+作者+出版社）
  第三层：模糊匹配打分（Levenshtein 相似度加权评分）
"""
import re
from typing import List, Tuple, Dict, Optional
from .book_record import BookRecord, MergedBook, normalize_text, canonical_isbn


# ============================================================
# 作者名称清洗
# ============================================================

AUTHOR_ROLE_PATTERN = re.compile(r'[，,。.\s\u3000\u00a0]*(著|编|译|主编|校|校译|编著|编译|编撰|等|绘|摄影)\s*$')


def clean_author_name(name: str) -> str:
    """去掉作者名称末尾的角色标注（著/编/译 等）"""
    if not name:
        return name
    return AUTHOR_ROLE_PATTERN.sub('', name).strip()


# ============================================================
# 纯 Python Levenshtein 距离实现（避免外部依赖）
# ============================================================

def levenshtein_ratio(s1: str, s2: str) -> float:
    """计算两个字符串的 Levenshtein 相似度 (0.0 ~ 1.0)"""
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    len1, len2 = len(s1), len(s2)
    if len1 > len2:
        s1, s2 = s2, s1
        len1, len2 = len2, len1

    prev = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        curr = [i] + [0] * len2
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(
                curr[j - 1] + 1,       # 插入
                prev[j] + 1,           # 删除
                prev[j - 1] + cost,    # 替换
            )
        prev = curr

    distance = prev[len2]
    max_len = max(len1, len2)
    return 1.0 - (distance / max_len)


# ============================================================
# 匹配引擎
# ============================================================

class BookMatcher:
    """多源书籍匹配与合并引擎"""

    # 模糊匹配阈值
    HIGH_CONFIDENCE_THRESHOLD = 0.85   # 高置信合并
    MEDIUM_CONFIDENCE_THRESHOLD = 0.70  # 低置信合并（打 ⚠️ 标记）
    # 低于此值 → 不合并，各自独立展示

    # 各字段在模糊匹配中的权重
    WEIGHT_TITLE = 0.5
    WEIGHT_AUTHOR = 0.3
    WEIGHT_PUBLISHER = 0.2

    def merge(self, records: List[BookRecord]) -> List[MergedBook]:
        """
        对多个数据源返回的记录进行去重合并
        返回 MergedBook 列表，按置信度排序（高 → 中 → 低）
        """
        if not records:
            return []

        # 第 1 步：ISBN 分组
        isbn_groups, unmatched = self._group_by_isbn(records)

        # 第 2 步：对无 ISBN 的记录进行指纹分组
        fp_groups, still_unmatched = self._group_by_fingerprint(unmatched)

        # 第 3 步：对仍未匹配的记录进行模糊匹配
        fuzzy_groups, orphans = self._fuzzy_group(still_unmatched)

        # 合并所有分组
        merged = []
        merged.extend(self._merge_group(group, method="isbn") for group in isbn_groups)
        merged.extend(self._merge_group(group, method="fingerprint") for group in fp_groups)
        merged.extend(self._merge_group(group, method="fuzzy") for group in fuzzy_groups)

        # 孤儿记录各自独立
        for r in orphans:
            merged.append(self._single_book(r))

        # 按置信度排序
        order = {"high": 0, "medium": 1, "low": 2}
        merged.sort(key=lambda b: (order.get(b.confidence, 3), -b.rating))

        return merged

    def _group_by_isbn(self, records: List[BookRecord]) -> Tuple[List[List[BookRecord]], List[BookRecord]]:
        """按 ISBN 分组。有 ISBN 且相同的放一组，无 ISBN 的进入 unmatched"""
        isbn_map: Dict[str, List[BookRecord]] = {}
        unmatched = []

        for r in records:
            n_isbn = r.get_normalized_isbn()
            if n_isbn:
                isbn_map.setdefault(n_isbn, []).append(r)
            else:
                unmatched.append(r)

        # 每组至少 2 条才算"匹配"（单条有 ISBN 但无其他源匹配的也进入 fingerprint 阶段）
        # 不过单条有 ISBN 的，我们把它的 ISBN 保留，fingerprint 阶段可能和另一条无 ISBN 的合并
        groups = []
        for isbn, group in isbn_map.items():
            if len(group) >= 2:
                groups.append(group)
            else:
                unmatched.extend(group)

        return groups, unmatched

    def _group_by_fingerprint(self, records: List[BookRecord]) -> Tuple[List[List[BookRecord]], List[BookRecord]]:
        """按指纹分组。相同指纹的放一组，其余进入模糊匹配"""
        fp_map: Dict[str, List[BookRecord]] = {}
        for r in records:
            fp = r.compute_fingerprint()
            fp_map.setdefault(fp, []).append(r)

        groups = []
        unmatched = []
        for fp, group in fp_map.items():
            if len(group) >= 2:
                groups.append(group)
            else:
                unmatched.extend(group)

        return groups, unmatched

    def _fuzzy_group(self, records: List[BookRecord]) -> Tuple[List[List[Tuple[BookRecord, BookRecord, float]]], List[BookRecord]]:
        """
        对剩余记录做两两模糊匹配，将相似度 >= MEDIUM_CONFIDENCE 的配对分组
        返回: (匹配分组列表, 孤儿记录列表)
        每个匹配分组是 [(record1, record2, score), ...] 列表
        """
        if len(records) < 2:
            return [], records

        pairs = []
        matched_indices = set()

        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                score = self._compute_similarity(records[i], records[j])
                if score >= self.MEDIUM_CONFIDENCE_THRESHOLD:
                    pairs.append((records[i], records[j], score))
                    matched_indices.add(i)
                    matched_indices.add(j)

        orphans = [records[i] for i in range(len(records)) if i not in matched_indices]

        # 将配对分组（相互传递分组）
        groups = self._cluster_pairs(pairs)

        return groups, orphans

    def _compute_similarity(self, a: BookRecord, b: BookRecord) -> float:
        """计算两本书的加权相似度"""
        title_sim = levenshtein_ratio(
            normalize_text(a.title),
            normalize_text(b.title),
        )

        author_a = normalize_text(" ".join(sorted(a.authors)))
        author_b = normalize_text(" ".join(sorted(b.authors)))
        author_sim = levenshtein_ratio(author_a, author_b) if author_a and author_b else 0.0

        pub_sim = levenshtein_ratio(
            normalize_text(a.publisher),
            normalize_text(b.publisher),
        )

        return (
            title_sim * self.WEIGHT_TITLE
            + author_sim * self.WEIGHT_AUTHOR
            + pub_sim * self.WEIGHT_PUBLISHER
        )

    def _cluster_pairs(self, pairs: List[Tuple[BookRecord, BookRecord, float]]) -> List[List[BookRecord]]:
        """将两两配对聚类成组（传递闭包）"""
        # Union-Find，用 id() 追踪对象身份（BookRecord 不可哈希）
        all_records = []
        idx_map = {}  # id(record) -> index
        for a, b, _ in pairs:
            if id(a) not in idx_map:
                idx_map[id(a)] = len(all_records)
                all_records.append(a)
            if id(b) not in idx_map:
                idx_map[id(b)] = len(all_records)
                all_records.append(b)

        n = len(all_records)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for a, b, _ in pairs:
            union(idx_map[id(a)], idx_map[id(b)])

        # 收集分组
        groups: Dict[int, List[BookRecord]] = {}
        for i, r in enumerate(all_records):
            root = find(i)
            groups.setdefault(root, []).append(r)

        return list(groups.values())

    def _merge_group(self, group: List[BookRecord], method: str = "isbn") -> MergedBook:
        """
        将一个分组的记录合并为一条 MergedBook
        优先级规则：
          - 标题：取最常见的（最长、去重后唯一的）
          - 作者：合并所有源去重
          - 出版社：优先豆瓣 > shlibrary > OpenLibrary
          - 封面：豆瓣 > 当当 > 微信读书 > Google Books > OpenLibrary > shlibrary
          - 评分：优先豆瓣
          - 简介：优先豆瓣，其次选非空最长的
          - ISBN：取所有源中最完整的
        """
        # 源优先级：中文商业/社区源优先，其次权威书目和国际源
        source_priority = {
            "douban": 0,
            "dangdang": 1,
            "weread": 2,
            "shlibrary": 3,
            "openlibrary": 4,
            "googlebooks": 5,
            "jd": 6,
        }

        # 计算置信度
        if method == "isbn":
            confidence = "high"
            note = ""
        elif method == "fingerprint":
            # 指纹匹配：需要检查是否来自不同源（同源可能是缓存重复）
            source_ids = set(r.source_id for r in group)
            if len(source_ids) >= 2:
                confidence = "high"
                note = ""
            else:
                confidence = "medium"
                note = "同一数据源的不同记录（可能是不同版本）"
        else:
            # 模糊匹配
            max_score = 0.0
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    score = self._compute_similarity(group[i], group[j])
                    max_score = max(max_score, score)
            if max_score >= self.HIGH_CONFIDENCE_THRESHOLD:
                confidence = "high"
                note = ""
            else:
                confidence = "medium"
                note = "标题/作者近似匹配，可能是不同版本或翻译版，建议人工确认"

        # 按源优先级排序
        sorted_records = sorted(group, key=lambda r: source_priority.get(r.source_id, 99))

        # 挑选各字段
        title = self._pick_title(sorted_records)
        subtitle = self._pick_subtitle(sorted_records)
        authors = self._merge_authors(sorted_records)
        translators = self._merge_translators(sorted_records)
        publisher = self._pick_by_priority(sorted_records, "publisher")
        published_date = self._pick_by_priority(sorted_records, "published_date")
        description = self._pick_description(sorted_records)
        cover_url = self._pick_cover(sorted_records)
        rating = self._pick_rating(sorted_records)
        tags = self._merge_tags(sorted_records)
        series = self._pick_by_priority(sorted_records, "series")
        language = self._pick_by_priority(sorted_records, "language")
        pages = self._pick_pages(sorted_records)
        clc_code = self._pick_clc(sorted_records)
        url = self._pick_by_priority(sorted_records, "url")
        isbn = self._pick_isbn(sorted_records)
        series_index = self._pick_by_priority(sorted_records, "series_index")

        # 合并 identifiers
        identifiers = {}
        for r in sorted_records:
            identifiers.update(r.identifiers)
        if isbn:
            identifiers["isbn"] = isbn

        # 字段来源追踪
        field_sources = self._trace_field_sources(sorted_records)

        return MergedBook(
            title=title,
            subtitle=subtitle,
            authors=authors,
            translators=translators,
            publisher=publisher,
            published_date=published_date,
            isbn=isbn,
            description=description,
            cover_url=cover_url,
            rating=rating,
            tags=tags,
            series=series,
            language=language,
            pages=pages,
            clc_code=clc_code,
            url=url,
            identifiers=identifiers,
            series_index=series_index,
            sources=self._ordered_source_names(sorted_records),
            confidence=confidence,
            merge_note=note,
            field_sources=field_sources,
            source_records=list(sorted_records),
        )

    def _single_book(self, record: BookRecord) -> MergedBook:
        """单条记录不合并"""
        return MergedBook(
            title=record.title,
            subtitle=record.subtitle,
            authors=record.authors,
            translators=record.translators,
            publisher=record.publisher,
            published_date=record.published_date,
            isbn=record.get_normalized_isbn(),
            description=record.description,
            cover_url=record.cover_url,
            rating=record.rating,
            tags=record.tags,
            series=record.series,
            language=record.language,
            pages=record.pages,
            clc_code=record.clc_code,
            url=record.url,
            identifiers=record.identifiers,
            series_index=record.series_index,
            sources=[record.source_name],
            confidence="high" if record.isbn else "medium",
            merge_note="" if record.isbn else "仅一个数据源返回，无 ISBN 校验",
            field_sources={},
            source_records=[record],
        )

    # ---- 字段挑选方法 ----

    def _ordered_source_names(self, records: List[BookRecord]) -> List[str]:
        """按排序后的记录顺序返回去重来源名，保证展示顺序稳定。"""
        seen = set()
        result = []
        for r in records:
            if r.source_name and r.source_name not in seen:
                seen.add(r.source_name)
                result.append(r.source_name)
        return result

    def _pick_title(self, records: List[BookRecord]) -> str:
        """选最常见的标题"""
        titles = [r.title.strip() for r in records if r.title and r.title.strip()]
        if not titles:
            return ""
        # 去重后选最长的
        unique = list(set(titles))
        return max(unique, key=len)

    def _pick_subtitle(self, records: List[BookRecord]) -> str:
        for r in records:
            if r.subtitle:
                return r.subtitle
        return ""

    def _merge_authors(self, records: List[BookRecord]) -> List[str]:
        """合并所有源作者去重，并清洗角色标注"""
        seen = set()
        result = []
        for r in records:
            for a in r.authors:
                cleaned = clean_author_name(a)
                na = normalize_text(cleaned)
                if na and na not in seen:
                    seen.add(na)
                    result.append(cleaned)
        return result if result else ["未知作者"]

    def _merge_translators(self, records: List[BookRecord]) -> List[str]:
        seen = set()
        result = []
        for r in records:
            for t in r.translators:
                nt = normalize_text(t)
                if nt and nt not in seen:
                    seen.add(nt)
                    result.append(t)
        return result

    def _pick_by_priority(self, records: List[BookRecord], field: str) -> str:
        """按源优先级取第一个非空值"""
        for r in records:
            val = getattr(r, field, "").strip() if isinstance(getattr(r, field, ""), str) else str(getattr(r, field, ""))
            if val:
                return val
        return ""

    def _pick_description(self, records: List[BookRecord]) -> str:
        """优先豆瓣，其次选最长的非空简介"""
        for r in records:
            if r.source_id == "douban" and r.description and r.description.strip():
                return r.description.strip()
        descs = [r.description.strip() for r in records if r.description and r.description.strip()]
        return max(descs, key=len) if descs else ""

    def _pick_cover(self, records: List[BookRecord]) -> str:
        """封面优先级：豆瓣 > 当当 > 微信读书 > Google Books > OpenLibrary > shlibrary"""
        priority = ["douban", "dangdang", "weread", "googlebooks", "openlibrary", "shlibrary"]
        for src in priority:
            for r in records:
                if r.source_id == src and r.cover_url:
                    return r.cover_url
        # 任何源的封面都可以
        for r in records:
            if r.cover_url:
                return r.cover_url
        return ""

    def _pick_rating(self, records: List[BookRecord]) -> float:
        for r in records:
            if r.source_id == "douban" and r.rating > 0:
                return r.rating
        for r in records:
            if r.rating > 0:
                return r.rating
        return 0.0

    def _merge_tags(self, records: List[BookRecord]) -> List[str]:
        """合并标签去重"""
        seen = set()
        result = []
        for r in records:
            for t in r.tags:
                nt = normalize_text(t)
                if nt and nt not in seen:
                    seen.add(nt)
                    result.append(t)
        return result

    def _pick_pages(self, records: List[BookRecord]) -> int:
        for r in records:
            if r.pages > 0:
                return r.pages
        return 0

    def _pick_clc(self, records: List[BookRecord]) -> str:
        """中图分类号只从 shlibrary 取"""
        for r in records:
            if r.source_id == "shlibrary" and r.clc_code:
                return r.clc_code
        return ""

    def _pick_isbn(self, records: List[BookRecord]) -> str:
        """从所有源中取标准化后的 ISBN"""
        for r in records:
            isbn = r.get_normalized_isbn()
            if isbn:
                return isbn
        return ""

    def _trace_field_sources(self, records: List[BookRecord]) -> Dict[str, str]:
        """追踪每个字段来自哪个源"""
        result = {}
        fields = ["title", "subtitle", "authors", "publisher", "published_date",
                   "isbn", "description", "cover_url", "rating", "tags", "series",
                   "language", "pages", "clc_code", "series_index"]
        for field in fields:
            for r in records:
                val = getattr(r, field, None)
                if val:
                    if isinstance(val, list) and len(val) > 0:
                        result[field] = r.source_name
                        break
                    elif isinstance(val, str) and val.strip():
                        result[field] = r.source_name
                        break
                    elif isinstance(val, (int, float)) and val > 0:
                        result[field] = r.source_name
                        break
        return result
