# Calibre-Web MultiSource 元数据聚合插件

多源书籍元数据聚合插件，整合 **豆瓣 + 当当 + 微信读书 + Open Library + Google Books** 五个数据源，通过三层防错机制智能合并结果。

## 特性

- **五源聚合**: 同时从豆瓣、当当、微信读书、Open Library、Google Books 拉取元数据
- **三层匹配合并**:
  1. ISBN 精确匹配（ISBN-10/13 自动归一化）
  2. 复合指纹匹配（SHA256 标题+作者+出版社）
  3. Levenshtein 模糊打分（≥0.85 高置信，≥0.70 低置信）
- **ISBN 级联**: 从书名搜索结果提取 ISBN，再精确查询 Open Library 和 Google Books
- **字段级溯源**: 每个字段标注来自哪个数据源
- **豆瓣翻页**: 支持搜索结果翻页
- **封面代理**: 解决豆瓣防盗链问题
- **API Key 热更新**: 豆瓣 Cookie、Google Books API Key、微信读书 API Key 通过文件读取，修改后无需重启容器；无 key 文件时对应数据源优雅降级
- **可配置**: 通过顶部常量控制源开关、超时、并发数
- **搜索预算** (Phase 1): 全局 6s 硬上限，慢源超时自动跳过
- **智能源选择** (Phase 2): 中文搜索自动跳过 OpenLibrary、Google Books（省 6-40s 无效等待）

## 安装

将整个 `douban/` 目录（所有 `.py` 文件）放入 Calibre-Web 的 `cps/metadata_provider/` 目录下：

```
cps/metadata_provider/douban/
├── MultiSource.py          # 主插件入口（聚合调度）
├── book_record.py          # 标准化数据结构 (BookRecord/MergedBook)
├── matcher.py              # 三层匹配合并引擎
├── source_douban.py        # 豆瓣数据源（爬虫 + Cookie）
├── source_dangdang.py      # 当当数据源（爬虫）
├── source_weread.py        # 微信读书数据源（API + Bearer Token）
├── source_openlibrary.py   # Open Library 数据源（REST API）
├── source_googlebooks.py   # Google Books 数据源（REST API）
├── source_nlc.py           # 国家图书馆数据源（默认禁用）
├── source_health.py        # 源熔断器（Phase 4）
├── clc_parser.py           # 中图分类号解析（可选）
├── data_wrapper.py         # CLC 数据（可选）
├── douban_cookie.txt       # 豆瓣 Cookie（自行配置，不入版本控制）
├── googlebooks_apikey.txt  # Google Books API Key（自行配置，不入版本控制）
└── weread_apikey.txt       # 微信读书 API Key（自行配置，不入版本控制）
```

重启 Calibre-Web 容器后，在元数据源中选择 **MultiSource** 即可。

## 配置

### 源开关

编辑 `MultiSource.py` 顶部常量：

```python
SOURCE_DOUBAN_ENABLED = True        # 豆瓣
SOURCE_DANGDANG_ENABLED = True      # 当当
SOURCE_WEREAD_ENABLED = True        # 微信读书（需 weread_apikey.txt）
SOURCE_OPENLIBRARY_ENABLED = True   # Open Library
SOURCE_NLC_ENABLED = False          # 国家图书馆（默认禁用，容器内不可达）

# Google Books
GOOGLE_BOOKS_AS_SOURCE = True       # 作为常规源参与书名搜索
CASCADE_GOOGLE_BOOKS = True         # ISBN 级联查询

# ISBN 级联
CASCADE_ENABLED = True
CASCADE_OPENLIBRARY = True
CASCADE_TIMEOUT = 5
CASCADE_LIMIT_GOOGLE = 10
CASCADE_MAX_RECORDS = 3          # Phase 5: 最多级联 N 个 ISBN

# 搜索预算 (Phase 1)
SEARCH_BUDGET_SECONDS = 6           # Phase1 全局等待上限
SOURCE_TIMEOUT = 4                  # 单源超时
SOURCE_RETRY_ENABLED = False        # 禁用自动重试

# 源选择 (Phase 2)
ZH_SKIP_OPENLIBRARY = True          # 中文搜索跳过 OpenLibrary
ZH_SKIP_GOOGLEBOOKS = True          # 中文搜索跳过 Google Books
```

### API Key / Cookie 配置

在插件目录下创建以下文件：

| 文件 | 内容 | 说明 |
|------|------|------|
| `douban_cookie.txt` | 浏览器 Cookie | F12 → Application → Cookies → douban.com，复制完整 Cookie 值 |
| `googlebooks_apikey.txt` | Google API Key | [Google Cloud Console](https://console.cloud.google.com/) 创建 API Key |
| `weread_apikey.txt` | 微信读书 Token | 格式 `wrk-xxxxx` |

修改后**无需重启容器**，下次搜索自动读取新值。

## 数据源说明

| 数据源 | 优势 | 限制 | 中文搜索 |
|--------|------|------|----------|
| 豆瓣 | 中文书籍信息丰富，评分、标签、简介完善 | 需爬虫解析 HTML，需 Cookie | 启用 |
| 当当 | 中文书籍封面、价格、出版社 | 无 ISBN 字段 | 启用 |
| 微信读书 | 中文书籍评分、简介、封面 | 需 API Key，/book/info 偶有超时 | 启用 |
| Open Library | 免费 REST API，国际化 | 中文书名搜索不支持，ISBN 级联可用 | 跳过（总返回 0） |
| Google Books | 全球覆盖，ISBN 查询准确 | 需 API Key，中文覆盖有限 | 跳过（覆盖率低） |
| 国家图书馆 | 权威中文书目，中图分类号 | OPAC 响应慢，默认禁用 | 可选 |

## 许可证

MIT
