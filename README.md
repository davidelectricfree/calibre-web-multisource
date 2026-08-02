# Calibre-Web MultiSource 元数据聚合插件

多源书籍元数据聚合插件，整合 **豆瓣** + **国家图书馆 (NLC)** + **Open Library** 三个数据源，通过三层防错机制智能合并结果。

## 特性

- **多源聚合**: 同时从豆瓣、国家图书馆、Open Library 拉取元数据
- **三层匹配合并**:
  1. ISBN 精确匹配（ISBN-10/13 自动归一化）
  2. 复合指纹匹配（SHA256 标题+作者+出版社）
  3. Levenshtein 模糊打分（≥0.85 高置信，≥0.70 低置信）
- **字段级溯源**: 每个字段标注来自哪个数据源
- **豆瓣翻页**: 支持搜索结果翻页
- **封面代理**: 解决豆瓣防盗链问题
- **可配置**: 通过顶部常量控制源开关、超时、并发数

## 安装

���整个 `douban/` 目录（所有 `.py` 文件）放入 Calibre-Web 的 `cps/metadata_provider/` 目录下：

```
cps/metadata_provider/douban/
├── MultiSource.py          # 主插件入口
├── book_record.py          # 标准化数据结构
├── matcher.py              # 三层匹配合并引擎
├── source_douban.py        # 豆瓣数据源
├── source_nlc.py           # 国家图书馆数据源
├── source_openlibrary.py   # Open Library 数据源
├── clc_parser.py           # 中图分类号解析（可选）
└── data_wrapper.py         # CLC 数据（可选）
```

重启 Calibre-Web 容器后，在元数据源中选择 **MultiSource** 即可。

## 配置

编辑 `MultiSource.py` 顶部常量：

```python
SOURCE_DOUBAN_ENABLED = True       # 豆瓣（默认启用）
SOURCE_NLC_ENABLED = False         # 国家图书馆（容器内网络不稳定，默认禁用）
SOURCE_OPENLIBRARY_ENABLED = False  # Open Library（容器内可能不可达，默认禁用）
SOURCE_TIMEOUT = 12                # 单源超时（秒）
```

各源文件的独立配置（如 `DOUBAN_TIMEOUT`、`DOUBAN_MAX_DETAIL_WORKERS`）在各自的 `source_*.py` 顶部。

## 数据源说明

| 数据源 | 优势 | 限制 |
|--------|------|------|
| 豆瓣 | 中文书籍信息丰富，评分、标签、简介完善 | 需爬虫解析 HTML |
| 国家图书馆 | 权威中文书目，中图分类号 | OPAC 响应慢，网络不稳定 |
| Open Library | 免费 REST API，国际化 | 中文书籍覆盖有限 |

## 许可证

MIT
