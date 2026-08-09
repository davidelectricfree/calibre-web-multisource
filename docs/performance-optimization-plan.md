# Calibre-Web MultiSource Search Performance Optimization Plan

> **Implementation Status**: Phase 0 ✅ | Phase 1 ✅ | Phase 2 ✅ | Phase 3 ⏭️ skipped | Phase 4 ✅ | Phase 5 next

## Background

The current remaining major defect is search latency. In production, a normal metadata search can take more than 30 seconds before Calibre-Web shows results.

The plugin currently uses a synchronous aggregation model:

1. Calibre-Web sends a keyword or ISBN query.
2. `MultiSource.py` dispatches requests to multiple metadata sources.
3. Each source may perform HTTP/API/HTML scraping, proxy probing, timeout handling, and retry.
4. Returned records are merged and deduplicated by `matcher.py`.
5. Some records are enriched through ISBN cascade queries against OpenLibrary and Google Books.
6. The final merged list is returned to Calibre-Web.

The likely performance problem is not a single slow function. It is tail latency amplification: one or more slow sources, retries, detail enrichment, and cascade queries are all part of the synchronous response path.

## Goals

Primary goal:

- Reduce common search latency from 30+ seconds to 3-8 seconds.

Secondary goals:

- Preserve high-quality Chinese metadata results.
- Keep ISBN and English metadata search usable.
- Avoid making unstable external sources block the whole search.
- Add enough performance logs to support future vibe coding work.
- Keep the first implementation low-risk and easy to roll back.

Non-goals for the first pass:

- Do not rewrite the whole plugin architecture.
- Do not introduce a persistent database cache immediately.
- Do not depend on true asynchronous UI updates unless Calibre-Web's provider interface is verified to support them.

## Current Latency Risks

| Area | Typical source | Impact |
| --- | --- | --- |
| Slow source timeout | Google Books, OpenLibrary, NLC, Douban | One source can delay the whole result set. |
| Retry amplification | `SOURCE_TIMEOUT` + retry + retry delay | One failed source can cost much more than its nominal timeout. |
| Synchronous enrichment | WeRead details, ISBN cascade | Search already has records but still waits for enrichment. |
| Proxy instability | xray/clash tunnel failures, Google 503 | Failed paths often take longer than successful paths. |
| No search cache | Repeated same query | Every search repeats all external calls. |
| Query-insensitive source selection | Chinese query still hitting English-oriented APIs | OpenLibrary Chinese title search often returns 422 and should not block Chinese search. |

## Phase 0: Add Performance Baseline Logging

Before optimizing, add structured timing logs so slow paths can be measured.

### Required metrics

For each search request:

- `request_id`
- query string and query type: `isbn`, `zh`, or `en`
- total search duration
- source-level duration
- source-level result count
- source-level error or timeout reason
- retry count per source
- cascade duration
- cascade candidate count
- merge duration
- final result count

Example log shape:

```text
[MultiSource][req=ab12cd] query='三体' type=zh start
[MultiSource][req=ab12cd] source=douban status=ok duration=2.13s count=8
[MultiSource][req=ab12cd] source=googlebooks status=timeout duration=4.00s count=0 retry=0
[MultiSource][req=ab12cd] cascade duration=2.87s candidates=3 count=2
[MultiSource][req=ab12cd] done total=6.24s final=12
```

### Baseline test queries

Use several query types:

- `三体`
- `Python`
- `原则`
- `深入理解计算机系统`
- `红楼梦`
- one valid ISBN
- one English title
- one obscure title

### Acceptance criteria

- ✅ A single search prints a complete timing breakdown in container logs.
- ✅ No search behavior changes in this phase.
- ✅ Logs are readable enough to identify the top slow sources.

## Phase 1: Add a Hard Search Budget

The plugin should have a whole-request wait budget. Source-level timeouts are not enough because the total response time can still grow through retries and follow-up enrichment.

### Proposed config

```python
SEARCH_BUDGET_SECONDS = 6
FAST_RESULT_MIN_COUNT = 5
SOURCE_TIMEOUT = 4
SOURCE_RETRY_ENABLED = False
```

### Scheduling behavior

1. Start selected sources in a `ThreadPoolExecutor`.
2. Collect completed source futures as they finish.
3. Stop waiting when `SEARCH_BUDGET_SECONDS` is reached.
4. If at least `FAST_RESULT_MIN_COUNT` records are available before the budget is exhausted, proceed to merge early.
5. Slow unfinished sources are ignored for this request and logged as skipped or timed out.
6. If all fast sources return empty, allow a fallback path with a slightly wider budget.

### Important implementation note

Python cannot safely kill a running thread that is blocked inside `requests`. The practical design is to stop waiting for unfinished futures, keep the executor bounded, and ensure source-level HTTP timeouts are short enough.

### Acceptance criteria

- ✅ Common searches return within about 6-8 seconds even when one external source is slow.
- ✅ One source timing out does not make final results empty if other sources returned records.
- (Unit tests deferred)

## Phase 2: Query Classification and Source Tiers

Different query types should use different source plans.

### Query classifier

Add a small classifier:

```python
def classify_query(query, is_isbn=False):
    if is_isbn or looks_like_isbn(query):
        return "isbn"
    if contains_cjk(query):
        return "zh"
    return "en"
```

### Source tiers

| Tier | Sources | Purpose | Default behavior |
| --- | --- | --- | --- |
| fast | Douban, Dangdang, WeRead | Chinese title search | Run in the first batch. |
| medium | NLC | Chinese bibliographic supplement | Run in first batch or fallback batch. |
| slow | OpenLibrary, Google Books | English/ISBN/external metadata | Do not block Chinese search. |
| enrich | WeRead detail, OpenLibrary ISBN, Google Books ISBN | Metadata completion | Limit candidate count and timeout. |

### Suggested source plan

Chinese title query:

- First batch: Douban, Dangdang, WeRead, NLC.
- Skip OpenLibrary title search because Chinese title queries frequently return 422.
- Do not run Google Books as a blocking title source by default.
- Allow limited ISBN cascade only for top candidates.

English title query:

- First batch: Google Books, OpenLibrary.
- Optional supplement: Douban, Dangdang.
- WeRead can run with lower priority or be skipped if it adds latency without useful results.

ISBN query:

- First batch: Google Books, OpenLibrary, Douban.
- Allow a slightly larger budget, such as 8-10 seconds.
- ISBN search is more precise, so waiting a little longer is acceptable.

### Acceptance criteria

- ✅ Chinese keyword searches no longer wait on OpenLibrary title-search 422 behavior.
- ✅ English keyword searches still use Google Books and OpenLibrary.
- ✅ ISBN search is not degraded by Chinese-query shortcuts.

## Phase 3: Add In-Memory TTL Cache

> **⏭️ Skipped (2026-08-09)**: Low usage frequency + single user = near-zero cache hit rate.
> Phase 1+2 already reduced latency from 30-120s to 4-8s — sufficient for current usage.
> Revisit if multi-user or higher search volume emerges.

A large part of metadata search is repeated user interaction. A small in-process cache can remove repeated external calls.

### Proposed config

```python
SEARCH_CACHE_TTL = 3600
SEARCH_CACHE_MAX_ITEMS = 256
EMPTY_RESULT_CACHE_TTL = 60
```

### Cache key

```python
cache_key = f"{query_type}:{normalized_query}"
```

### Cache value

The simplest first pass can cache `BookRecord` objects directly in process memory.

A later persistent cache can serialize results into SQLite, but that should be a separate phase because it adds filesystem permission and schema concerns.

### Rules

- Cache successful non-empty results for 1 hour.
- Cache empty results only briefly, or do not cache them at all.
- Do not cache exceptions.
- Log cache hit/miss.

### Acceptance criteria

- Repeating the same keyword returns in less than 0.5 seconds.
- Cache expiration causes a fresh external query.
- Cache size is bounded.

## Phase 4: Source Circuit Breaker

External sources that repeatedly fail should not be retried on every user search.

### Proposed config

```python
SOURCE_FAILURE_THRESHOLD = 3
SOURCE_COOLDOWN_SECONDS = 300
SOURCE_CIRCUIT_BREAKER_ENABLED = True
```

### Behavior

- Count consecutive failures per source.
- After threshold is reached, mark source as open.
- During cooldown, skip that source and log the skip reason.
- After cooldown, allow one half-open probe.
- On success, close the circuit and reset failure count.

### Good candidates

- Google Books
- OpenLibrary
- NLC
- Douban
- Dangdang

### Acceptance criteria

- ✅ A repeatedly timing-out source is skipped for the cooldown window.
- ✅ Other sources continue to work normally.
- ✅ Logs make it clear when a source is skipped by circuit breaker.

## Phase 5: Limit Synchronous Enrichment

Search should prioritize returning useful candidates quickly. Rich metadata can be limited to the top records.

### Proposed config

```python
CASCADE_TIMEOUT = 3
CASCADE_MAX_RECORDS = 3
WEREAD_DETAIL_LIMIT = 3
WEREAD_TIMEOUT = 4
```

### Rules

- Only enrich the top N candidates.
- Only cascade records missing important fields such as ISBN, publisher, or description.
- Do not run full cascade for all returned records.
- Keep enrichment logs separate from source search logs.

### Conservative design

If Calibre-Web's provider interface only supports synchronous result lists, keep enrichment synchronous but limited.

### Aggressive design

If later testing proves Calibre-Web asks the provider again when a user selects a record, move detail enrichment to the selection/detail stage instead of the search stage.

This must be verified against Calibre-Web behavior before implementation.

## Recommended First Implementation

The first coding pass should be small and high-impact:

1. Add performance baseline logs.
2. Add `SEARCH_BUDGET_SECONDS` and stop waiting for slow sources.
3. Add query classification and Chinese-source plan.
4. Limit cascade and WeRead detail enrichment.

Do not implement persistent cache or large module extraction in the first pass.

## Proposed First-Pass Config

```python
SEARCH_BUDGET_SECONDS = 6
FAST_RESULT_MIN_COUNT = 5
SOURCE_TIMEOUT = 4
SOURCE_RETRY_ENABLED = False
CASCADE_ENABLED = True
CASCADE_TIMEOUT = 3
CASCADE_MAX_RECORDS = 3
WEREAD_DETAIL_LIMIT = 3
WEREAD_TIMEOUT = 4
GOOGLE_BOOKS_AS_SOURCE = False
OPENLIBRARY_AS_ZH_SOURCE = False
SEARCH_CACHE_TTL = 3600
SOURCE_CIRCUIT_BREAKER_ENABLED = True
```

`GOOGLE_BOOKS_AS_SOURCE` should be interpreted by query type: false for Chinese title search, true for English and ISBN search.

## Expected Results

| Scenario | Current | Target |
| --- | ---: | ---: |
| Popular Chinese title | 30+ seconds | 3-6 seconds |
| Obscure Chinese title | 30+ seconds | 6-10 seconds |
| English title | 20-30 seconds | 5-8 seconds |
| ISBN | 10-30 seconds | 4-8 seconds |
| Repeated search with cache | 30+ seconds | less than 0.5 seconds |

## Suggested Vibe Coding Tasks

### Task A: Performance logging

Files:

- `MultiSource.py`

Work:

- Add request id.
- Log source timing and result count.
- Log cascade timing.
- Log merge timing.
- Log final total timing.

Acceptance:

- A production search produces a complete timing trace.
- Existing tests still pass.

### Task B: Search budget scheduler

Files:

- `MultiSource.py`
- `tests/test_core.py`

Work:

- Add total budget config.
- Stop waiting after budget expires.
- Return completed source results.
- Add test for slow source not blocking fast source.

Acceptance:

- Simulated slow source cannot make search exceed budget.
- Fast source records are returned.

### Task C: Query classifier and source plans

Files:

- `MultiSource.py`
- optional later: `query_classifier.py`
- `tests/test_core.py`

Work:

- Classify query as `isbn`, `zh`, or `en`.
- Select source list by query type.
- Skip OpenLibrary title search for Chinese queries.
- Keep Google Books for English and ISBN queries.

Acceptance:

- Chinese query source plan excludes OpenLibrary title search.
- ISBN source plan still includes OpenLibrary and Google Books.

### Task D: Limit enrichment

Files:

- `MultiSource.py`
- `source_weread.py`
- `tests/test_core.py`

Work:

- Add `CASCADE_MAX_RECORDS`.
- Reduce WeRead detail limit.
- Only enrich missing fields.

Acceptance:

- Cascade count is bounded.
- Search quality remains acceptable for top records.

### Task E: In-memory search cache

Files:

- optional new `cache_manager.py`
- `MultiSource.py`
- `tests/test_core.py`

Work:

- Add TTL cache.
- Add cache hit/miss logs.
- Bound cache size.

Acceptance:

- Repeated same query returns from cache.
- Empty or failed results are not cached long-term.

### Task F: Circuit breaker

Files:

- optional new `source_health.py`
- `MultiSource.py`
- `tests/test_core.py`

Work:

- Track consecutive source failures.
- Skip failed source during cooldown.
- Add half-open recovery.

Acceptance:

- Repeated timeouts open the circuit.
- Cooldown skip is visible in logs.
- Successful probe closes the circuit.

## Proposed Future Architecture

The first implementation can stay inside `MultiSource.py`. After behavior is stable, split responsibilities gradually:

```text
SearchController
  -> QueryClassifier
  -> SearchCache
  -> SourceScheduler
       -> source budget
       -> source tiering
       -> circuit breaker
  -> ResultMerger
  -> EnrichmentScheduler
       -> ISBN cascade
       -> detail fill
  -> PerfLogger
```

Recommended extraction order:

| Module | Extract when |
| --- | --- |
| `query_classifier.py` | Query rules stabilize. |
| `cache_manager.py` | Cache behavior is proven useful. |
| `source_health.py` | Circuit breaker is added. |
| `scheduler.py` | Source scheduling becomes too complex for `MultiSource.py`. |

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Too aggressive timeout reduces metadata quality | Use query-type source plans and fallback paths. |
| Empty results get cached | Do not cache exceptions; cache empty results only briefly. |
| Background threads pile up | Keep source HTTP timeouts short and thread pool bounded. |
| Google Books quota is consumed too quickly | Do not run Google Books for default Chinese title search. |
| Calibre-Web does not support async detail updates | Keep first implementation synchronous but limited. |

## Recommended Next Step

Start with Task A and Task B:

1. Add timing logs.
2. Add a 6-second search budget.
3. Verify with production logs.
4. Only then tune source plans and enrichment limits.

This gives a measurable baseline and a direct latency reduction without a large rewrite.
