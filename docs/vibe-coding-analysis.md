# Calibre-Web MultiSource 项目分析与 Vibe-Coding 准备

分析日期：2026-08-09

仓库：`davidelectricfree/calibre-web-multisource`

## 1. 项目定位

这是一个 Calibre-Web 元数据 provider 插件，目标是在 Calibre-Web 编辑书籍元数据时，通过一个统一入口同时查询多个书籍数据源，再做去重、字段择优和封面处理，最后返回 Calibre-Web 可展示和保存的 `MetaRecord` 列表。

当前代码实际覆盖的数据源：

| 数据源 | 文件 | 默认角色 | 鉴权/依赖 | 主要价值 |
|---|---|---|---|---|
| 豆瓣 | `source_douban.py` | 常规书名搜索 | `douban_cookie.txt` | 中文书信息、评分、简介、标签、封面 |
| 当当 | `source_dangdang.py` | 常规书名/ISBN 搜索 | 无 | 中文书封面、出版社、出版日期、价格 |
| 微信读书 | `source_weread.py` | 常规书名搜索 | `weread_apikey.txt`，无 key 时空结果降级 | 中文书简介、评分、封面、ISBN 补充 |
| Open Library | `source_openlibrary.py` | 常规搜索 + ISBN 级联 | 公开 API，依赖代理，最佳代理 TTL 缓存 | 国际书目、ISBN 精确补充 |
| Google Books | `source_googlebooks.py` | 常规搜索 + ISBN 级联 | `googlebooks_apikey.txt` 可选，最佳代理 TTL 缓存 | 全球书目、ISBN 精确补充、封面 |
| 国家图书馆 | `source_nlc.py` | 可选源，默认关闭 | OPAC HTML | 权威中文书目、中图分类号 |

当前 README 与 `MultiSource.py` 文件头已同步为五源聚合描述。

## 2. GitHub 与 NAS 生产代码对比

本次只读复制 NAS 生产目录 `/volume1/docker/document_app/calibre/douban/` 到本地对比。没有修改 NAS 文件，没有重启容器。

结论：GitHub 和 NAS 不是完全字节一致，但大部分 Python 文件在归一化换行后内容一致。实质差异集中在以下位置：

| 文件 | 差异 | 影响判断 |
|---|---|---|
| `MultiSource.py` | GitHub 的 `EXTERNAL_COVER_DOMAINS` 包含 `doubanio.com`；NAS 生产版只包含 `books.google.com` 和 `covers.openlibrary.org` | GitHub 会把豆瓣封面也纳入外部封面代理逻辑；NAS 生产版不会对豆瓣封面走这段域名判断 |
| `proxy_manager.py` | GitHub 的 `probe_best_proxy()` 在直连胜出时返回 `(None, "direct")`；NAS 生产版返回 `({}, "direct")` | `requests` 中 `proxies=None` 与 `proxies={}` 都表示不显式使用代理，但在容器全局 `HTTP_PROXY/HTTPS_PROXY` 存在时，二者行为可能不同；NAS 版更倾向强制直连 |
| `README.md` | NAS 版是旧三源说明，并出现乱码；GitHub 版是五源说明 | README 应以 GitHub 版为准，NAS 版不适合作为当前文档来源 |
| NAS 额外文件 | `MultiSource.py.before_cascade`、`source_dangdang.py.bak_date`、`source_douban.py.bak_date` | 生产目录残留备份文件。Calibre-Web 会加载 `metadata_provider/` 下 Python 文件，备份文件当前不是 `.py` 后缀，风险较低，但建议长期清理 |
| 仓库额外文件 | `.gitignore`、`compose.yaml` | GitHub 有部署与忽略规则，NAS 插件代码目录本身没有这些文件是正常现象 |

归一化换行后内容一致的核心文件包括：`book_record.py`、`matcher.py`、`source_douban.py`、`source_dangdang.py`、`source_openlibrary.py`、`source_googlebooks.py`、`source_weread.py`、`source_nlc.py`、`clc_parser.py`、`data_wrapper.py`。

## 3. 运行与部署形态

`compose.yaml` 使用 `linuxserver/calibre-web:latest`，关键点：

```yaml
- CALIBRE_LOCALHOST=true
- HTTP_PROXY=http://192.168.1.249:20172
- HTTPS_PROXY=http://192.168.1.249:20172
- NO_PROXY=localhost,127.0.0.1,192.168.0.0/16,172.16.0.0/12,10.0.0.0/8
- ./douban:/app/calibre-web/cps/metadata_provider
- /volume1/documents/ebook:/books
```

重要含义：

1. 插件目录挂载到 Calibre-Web 的 `metadata_provider` 根目录。
2. 修改 `.py` 文件后需要重启或重建 calibre-web 容器才能加载。
3. 修改 `douban_cookie.txt`、`googlebooks_apikey.txt`、`weread_apikey.txt` 不需要重启，因为 source 每次请求前读文件。
4. `CALIBRE_LOCALHOST=true` 是绕开 Calibre-Web `cw_advocate` 代理限制的重要配置，不能轻易移除。
5. 全局代理环境变量会影响所有 `requests` 默认行为，所以代码里 `proxies=None`、`proxies={}`、`NO_PROXY={"http": None, "https": None}` 的差异需要谨慎测试。

## 4. 核心架构

### 4.1 插件入口

`MultiSource.py` 中的 `MultiSource` 继承 Calibre-Web 的 `Metadata`：

```python
class MultiSource(Metadata):
    __name__ = PROVIDER_NAME
    __id__ = PROVIDER_ID

    def search(self, query: str, generic_cover: str = "", locale: str = "en") -> List[MetaRecord]:
```

`search()` 是 Calibre-Web 调用的主接口。职责包括：

1. 校验 query。
2. 判断 query 是否 ISBN。
3. 预热代理并记录当前代理。
4. 并发查询常规数据源。
5. 从第一阶段结果提取 ISBN。
6. 对 Open Library / Google Books 做 ISBN 级联补充。
7. 调用 matcher 合并去重。
8. 转换为 Calibre-Web `MetaRecord`。

### 4.2 两阶段数据流

```text
用户输入 query
  |
  v
MultiSource.search()
  |
  +-- 判断 ISBN
  |
  +-- 阶段 1：常规源并发搜索
  |      豆瓣 / 当当 / 微信读书 / Open Library / Google Books
  |
  +-- 提取有效 ISBN-13
  |
  +-- 阶段 2：ISBN cascade
  |      Open Library 批量 ISBN 查询
  |      Google Books 限量逐 ISBN 查询
  |
  +-- BookMatcher.merge()
  |
  +-- _to_meta_records()
  |
  v
Calibre-Web UI 展示候选元数据
```

### 4.3 标准数据模型

`book_record.py` 定义两个核心 dataclass：

- `BookRecord`：单个数据源返回的标准化记录。
- `MergedBook`：多源合并后的结果。

关键字段：`title`、`subtitle`、`authors`、`translators`、`publisher`、`published_date`、`isbn`、`description`、`cover_url`、`rating`、`tags`、`series`、`language`、`pages`、`clc_code`、`series_index`、`identifiers`。

关键工具函数：

- `canonical_isbn()`：清洗 ISBN，并把 ISBN-10 转 ISBN-13。
- `normalize_date()`：将 `YYYY`、`YYYY-M`、`YYYY.MM` 等归一成 Calibre 更容易接受的日期格式。
- `normalize_text()`：用于匹配的文本归一化。

## 5. 合并与去重策略

`matcher.py` 使用三层匹配：

1. ISBN 精确匹配：最高置信。
2. 复合指纹匹配：`SHA256(normalized title + authors + publisher)`。
3. Levenshtein 模糊匹配：标题权重 0.5，作者 0.3，出版社 0.2；0.85 以上高置信，0.70 以上中置信。

字段选择逻辑集中在 `_merge_group()`：

```python
source_priority = {"douban": 0, "dangdang": 1, "weread": 2, "nlc": 3, "openlibrary": 4, "googlebooks": 5, "jd": 6}
```

当前状态：

1. `source_priority` 已显式覆盖 `weread` 和 `googlebooks`。
2. `_pick_cover()` 已加入 WeRead 和 Google Books，封面优先级为豆瓣、当当、微信读书、Google Books、Open Library、NLC。
3. 来源展示已改成按合并优先级稳定去重，不再使用 `set` 造成随机顺序。
4. `_pick_title()` 当前仍选最长标题，后续如果要细分主标题/副标题，需要单独设计字段规则。

## 6. 代理、超时和重试

### 6.1 代理管理

`proxy_manager.py` 有两套机制：

1. `get_proxies()`：主备 TCP 探测，优先 xray `20172`，备选 clash `17890`，缓存 60 秒。
2. `probe_best_proxy()`：实际请求 OpenLibrary 测直连、xray、clash 延迟，返回最快链路。

`get_proxies()` 避免 HTTP 请求，只用 socket，目的是不触发 Calibre-Web `cw_advocate` 对代理的限制。

### 6.2 代码内网络调用特点

| 模块 | 网络策略 |
|---|---|
| 豆瓣 | `NO_PROXY={"http": None, "https": None}`，强调国内直连 |
| 当当 | 同样直连，不走代理 |
| 微信读书 | 直连，`verify=False`，429 重试一次 |
| Open Library | `probe_best_proxy()`，`verify=False`，Session 禁用 adapter 重试 |
| Google Books | `probe_best_proxy()`，`verify=False`，429 等 1 秒重试一次 |
| NLC | `urllib.request`，3 次重试，默认禁用 |

主要风险：

1. `OpenLibrarySource._proxies_cache` 和 `GoogleBooksSource._proxies_cache` 已加入 60 秒 TTL，避免一次探测结果永久固定。
2. `verify=False` 分散在多个模块里，虽然解决了代理 SSL 问题，但会产生安全告警，也不利于统一治理。
3. 全局代理环境变量存在时，`proxies=None` 不一定等于强制直连。NAS 生产版把 direct 返回 `{}`，可能是为了规避这一点。
4. `MultiSource._query_all_sources()` 使用 `as_completed()`，再对已经完成的 future 调 `future.result(timeout=SOURCE_TIMEOUT)`，这个 timeout 对单源总耗时的保护有限；真正保护主要依赖各 source 内部 timeout。
5. ISBN cascade 中 Google Books 逐 ISBN 并发，虽然 `CASCADE_LIMIT_GOOGLE=10`，但大量搜索仍会消耗 API 配额。

## 7. 数据源细节

### 7.1 豆瓣

优点：中文元数据质量高，覆盖评分、标签、简介、封面。

限制：

1. 依赖 HTML 结构，页面变更会影响解析。
2. 依赖 `douban_cookie.txt`；Cookie 过期时通常表现为空结果而非显式错误。
3. 当前 `DOUBAN_SEARCH_PAGES=1`、`DOUBAN_PAGE_SIZE=5`，搜索覆盖有限。
4. 文件内 `print()` 与主入口 `log` 混用。

### 7.2 当当

优点：中文纸书信息稳定，封面和出版社常有价值。

限制：

1. 主要从搜索结果页提取，ISBN 不稳定。
2. GBK 解码逻辑合理，但 HTML 结构变化仍会影响解析。
3. 价格放入 `identifiers["dd_price"]`，这不是标准书籍标识符，后续可考虑迁移到扩展字段或 tags 之外的展示层。

### 7.3 微信读书

优点：中文书简介、评分、封面补充价值高。

限制：

1. 硬编码 fallback API key 已移除，当前依赖 `weread_apikey.txt` 或显式传入 key。
2. `search(query, is_isbn)` 忽略 `is_isbn` 参数，本质只做关键词搜索。
3. 详情并发限制合理，但 `/book/info` 超时时可能导致 ISBN 缺失。

### 7.4 Open Library

优点：公开 API、ISBN cascade 价值大。

限制：

1. 中文书名搜索不稳定，README 也说明中文覆盖有限。
2. `_get_best_proxies()` 已加入 60 秒 TTL。
3. `search_by_isbns()` 中动态构造 `__import__(chr(...))` 的 debug 日志已清理。

### 7.5 Google Books

优点：ISBN 精确查询、封面和国际书目补充能力强。

限制：

1. 硬编码 fallback API key 已移除，当前优先读取 `googlebooks_apikey.txt`，无 key 时不传 key。
2. 每日配额有限，cascade 并发查询容易加速消耗。
3. `_get_best_proxies()` 已加入 60 秒 TTL。
4. `langRestrict="zh-CN"` 是否符合 Google Books API 预期，需要用实际请求验证；常见语言参数通常是 `zh-CN` 或 `zh`，应以 API 行为为准。

### 7.6 NLC

优点：中图分类号和权威中文书目信息。

限制：

1. 默认关闭，说明容器内访问不稳定。
2. OPAC 动态 Session URL 与 HTML 解析复杂，维护成本较高。
3. `data_wrapper.py` 体积约 8MB，不适合手工编辑。

## 8. 封面处理

封面处理有两层：

1. `MultiSourceMetaRecord.__getattribute__()` 在读取 `cover` 属性时，把外部封面 URL 改写成本地 `/metadata/douban_cover?cover=...`。
2. `_hack_cover_proxy()` monkey patch `helper.save_cover_from_url`，保存封面时对外部封面直接 `requests.get()` 后 `save_cover()`。

风险：

1. Monkey patch 依赖 Calibre-Web 内部 helper API，升级 Calibre-Web 后需要回归验证。
2. 当前函数名仍叫 `douban_cover`，但实际已代理 Google Books 和 OpenLibrary 封面，命名落后于功能。
3. GitHub 与 NAS 对 `doubanio.com` 是否纳入 `EXTERNAL_COVER_DOMAINS` 不一致，需要用实际封面保存流程验证后统一。
4. `requests.get(cover_url, timeout=15)` 没有显式代理参数，实际行为会受容器环境变量影响。

## 9. 安全与敏感信息

当前 `.gitignore` 已排除：

```gitignore
douban_cookie.txt
googlebooks_apikey.txt
weread_apikey.txt
__pycache__/
*.pyc
*.bak
backup_*/
```

源码中的硬编码 fallback key 已移除：

- `source_googlebooks.py` 的 `GB_API_KEY` 为空字符串，优先读取 `googlebooks_apikey.txt`。
- `source_weread.py` 的 `WEREAD_API_KEY` 为空字符串，优先读取 `weread_apikey.txt`。

后续可选增强：

1. 支持环境变量作为第二来源，例如 `GOOGLEBOOKS_API_KEY`、`WEREAD_API_KEY`。
2. 在 source 级别输出更清晰的“key 文件缺失”诊断日志。

## 10. 测试缺口

当前仓库没有测试目录、依赖声明、CI 配置。后续 vibe-coding 前建议先补最小测试基础：

1. `canonical_isbn()`：ISBN-10 转 ISBN-13、非法 ISBN、带横线 ISBN。
2. `normalize_date()`：`YYYY`、`YYYY-M`、`YYYY.MM`、完整日期。
3. `BookMatcher.merge()`：ISBN 合并、指纹合并、模糊合并、孤儿记录。
4. 字段优先级：豆瓣/当当/OpenLibrary/Google Books/WeRead 混合时字段来源符合预期。
5. 标签去重：大小写、空白、重复出版社/语言/系列。
6. source parser fixture：用本地 HTML/JSON fixture 测豆瓣、当当、OpenLibrary、Google Books、WeRead 解析，不依赖实时网络。
7. 网络层 mock：测试 429、timeout、SSL error、空响应。

## 11. 路线状态

### P0：已完成

1. GitHub 和 NAS 的豆瓣封面域名差异已按 GitHub 版本同步到生产插件。
2. 源码硬编码 key 已移除，Google Books 与 WeRead 改为 key 文件优先、无 key 降级。
3. README 配置值已与当前代码同步。
4. `MultiSource.py` 顶部注释已更新为当前五源架构。

### P1：已完成本轮核心项

1. `book_record.py`、`matcher.py`、`proxy_manager.py`、OpenLibrary/GoogleBooks 代理缓存已补单元测试。
2. OpenLibrary 和 Google Books 的最佳代理缓存已加入 60 秒 TTL。
3. OpenLibrary 的动态 import debug 日志已清理。
4. 剩余可选项：后续可继续抽 `http_client.py`，统一 timeout、headers、proxy、verify、retry 和 logger。

### P2：已完成本轮核心项

1. `source_priority` 已加入 `weread` 和 `googlebooks`。
2. 封面优先级已加入 WeRead 和 Google Books。
3. source description 的来源顺序已稳定化，不再使用 `set`。
4. 剩余可选项：标题选择仍是“最长标题”，如要改为“主标题优先 + 副标题单独存储”，建议单独设计规则并加 fixture。

### P3：已完成本轮核心项

1. 每次搜索会生成 request id，贯穿代理、源查询、cascade、merge 日志。
2. 源查询日志输出返回条数和耗时。
3. ISBN cascade 日志输出返回条数、耗时和超时信息。
4. 剩余可选项：后续可增加只读诊断脚本，检查 cookie/key 文件、代理链路和各源可用性。

## 12. Vibe-Coding 任务拆分建议

适合后续逐步交给 AI 做的小任务：

1. “为 `book_record.py` 和 `matcher.py` 增加 pytest 单元测试，不改业务逻辑。”
2. “修正 README 与当前代码配置不一致，只改文档。”
3. “移除 Google Books 和 WeRead 的硬编码 fallback key，改为 key 文件和环境变量，保持无 key 时优雅降级。”
4. “为 `source_priority` 增加 weread/googlebooks，并写测试证明字段优先级。”
5. “把 source 模块里的 `print()` 替换成统一 logger，不改变返回逻辑。”
6. “重构代理选择缓存，让 OpenLibrary 和 Google Books 的最佳代理支持 TTL。”
7. “为豆瓣和当当解析增加 fixture 测试，避免实时网络依赖。”
8. “统一封面代理命名，把 `douban_cover` 改成通用 cover proxy，但保持旧路由兼容。”

## 13. 后续执行原则

1. 生产 NAS 与 GitHub 已有轻微漂移，任何修复都应先确认以哪个版本为基准。
2. 修改 `.py` 后必须在隔离环境或测试中先验证，再考虑同步到 NAS；生产容器重启需要单独确认。
3. 涉及代理、封面、Calibre-Web helper monkey patch 的改动要小步提交，避免一次性改动多个变量。
4. 涉及 key/cookie 的改动不能把真实凭据写入仓库。
5. 优先补测试，再做结构性重构。
