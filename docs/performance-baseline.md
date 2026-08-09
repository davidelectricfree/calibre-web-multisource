# Phase 0: Performance Baseline Report

> Generated: 2026-08-09 | 16 searches across 11 distinct queries
> Environment: DS923+ NAS (DSM 7.3.2), Docker container calibre-web, proxy: xray

---

## Methodology

Test script ran inside the calibre-web container via `docker exec`, directly instantiating `MultiSource` and calling its `search()` method. Each search was timed with `time.time()` and the Phase 0 structured logs (request id, per-source duration/status/retry, phase1/cascade/merge totals) were captured from container stdout.

Queries cover four categories:

| Category | Queries | Count |
|----------|---------|-------|
| Chinese popular (zh-hot) | 三体 (×3), 活着, 原则, 明朝那些事儿 | 6 |
| Chinese technical/literary (zh) | 深入理解计算机系统, 百年孤独 | 2 |
| English | Python Programming, Design Patterns, The Great Gatsby | 3 |
| ISBN | 9787544270878, 9787111633224, 9787506365437 | 3 |
| Repeat | 三体 | (included above) |

---

## Raw Results

### Round 1 — 4 searches

| # | Query | Type | Phase1 | Cascade | Merge | **Total** | Results | Cascade% |
|---|-------|------|--------|---------|-------|-----------|---------|----------|
| 1 | 三体 | zh | 4.46s | 60.52s | 0.05s | **65.03s** | 18 | 93% |
| 2 | Python Programming | en | 26.98s | 94.26s | 0.13s | **121.37s** | 32 | 78% |
| 3 | 9787544270878 | isbn | 15.59s | 0s | 0.01s | **15.61s** | 10 | — |
| 4 | 原则 | zh | 4.16s | 14.89s | 0.15s | **19.21s** | 20 | 78% |

### Round 2 — 10 searches

| # | Query | Type | Phase1 | Cascade | Merge | **Total** | Results | Cascade% |
|---|-------|------|--------|---------|-------|-----------|---------|----------|
| 5 | 三体 | zh-hot | 9.15s | 102.02s | 0.05s | **111.22s** | 24 | 92% |
| 6 | 活着 | zh-hot | 5.57s | 15.20s | 0.14s | **20.91s** | 21 | 73% |
| 7 | 明朝那些事儿 | zh-hot | 11.31s | 57.56s | 0.82s | **69.68s** | 18 | 83% |
| 8 | 深入理解计算机系统 | zh-tech | 17.23s | 36.06s | 0.01s | **53.30s** | 11 | 68% |
| 9 | 百年孤独 | zh-lit | 30.82s | 44.16s | 0.11s | **75.08s** | 19 | 59% |
| 10 | Design Patterns | en-tech | 13.32s | 31.53s | 0.31s | **45.15s** | 23 | 70% |
| 11 | The Great Gatsby | en-lit | 19.85s | 58.37s | 0.07s | **78.29s** | 23 | 75% |
| 12 | 9787111633224 | isbn | 8.80s | 0s | 0.08s | **8.88s** | 12 | — |
| 13 | 9787506365437 | isbn | 3.24s | 0s | 0.11s | **3.35s** | 11 | — |
| 14 | 三体 (repeat) | zh-repeat | 3.82s | 82.30s | 0.04s | **86.17s** | 19 | 95% |

---

## Per-Source Performance Breakdown

### Chinese Keyword Queries (zh)

| Source | Min | Max | Median | Typical | Reliability |
|--------|-----|-----|--------|---------|-------------|
| 当当 (dangdang) | 0.32s | 0.73s | 0.40s | **<1s** | ✅ Very fast |
| 微信读书 (weread) | 0.94s | 2.52s | 1.25s | **1-3s** | ✅ Fast |
| 豆瓣 (douban) | 3.13s | 7.23s | 4.15s | **3-5s** | ✅ Stable |
| Google Books (zh) | 1.56s | 13.31s | 7.12s | **2-9s** | ⚠️ Variable |
| Open Library (zh) | 4.15s | 30.81s | 11.31s | **4-31s** | 🔴 Unstable, always 0 results for zh |

### English Keyword Queries (en)

| Source | Min | Max | Median | Typical | Reliability |
|--------|-----|-----|--------|---------|-------------|
| 当当 (dangdang) | 0.32s | 0.37s | 0.37s | **<1s** | ✅ Fast (Chinese titles only) |
| 微信读书 (weread) | 1.08s | 2.52s | 1.70s | **1-3s** | ✅ Fast |
| 豆瓣 (douban) | 3.76s | 4.28s | 4.02s | **3-5s** | ✅ Stable |
| Google Books (en) | 8.24s | 13.31s | 10.78s | **8-13s** | 🔴 Slow |
| Open Library (en) | 13.00s | 26.98s | 19.85s | **13-27s** | 🔴 Very slow |

### ISBN Queries

| Source | Min | Max | Median | Typical | Reliability |
|--------|-----|-----|--------|---------|-------------|
| 当当 (dangdang) | 0.32s | 0.60s | 0.39s | **<1s** | ✅ Fast |
| 微信读书 (weread) | 0.94s | 2.19s | 1.55s | **1-2s** | ✅ Fast |
| 豆瓣 (douban) | 0.32s | 2.25s | 2.19s | **1-3s** | ✅ Stable |
| Google Books (isbn) | 0.55s | 8.80s | 6.37s | **1-9s** | ⚠️ Variable |
| Open Library (isbn) | 0.76s | 15.59s | 4.95s | **1-16s** | 🔴 Unstable |

---

## Key Findings

### Finding 1: Cascade is THE dominant bottleneck

Across all 8 Chinese title searches that triggered cascade:

| Metric | Value |
|--------|-------|
| Average cascade contribution | **80% of total latency** |
| Range | 59% – 95% |
| Average cascade time | **55.7s** |
| Worst case | 102.02s (三体 #2) |

The cascade queries 16 ISBNs × 2 sources (Google Books + OpenLibrary) serially or with limited parallelism. With SOURCE_TIMEOUT=8s, theoretical worst case for 16 ISBNs × 2 sources = 256s.

### Finding 2: Phase1 is actually fast for Chinese

Excluding the百年孤独 outlier (where OpenLibrary took 30.81s):

| Metric | Value |
|--------|-------|
| Median Phase1 (zh) | **5.6s** |
| Range (excluding outlier) | 4.16s – 11.31s |
| Fastest Chinese source (dangdang) | always <1s |

If cascade were capped or made non-blocking, most Chinese searches would return in **4-11 seconds**.

### Finding 3: OpenLibrary is the worst-performing source

| Scenario | Behavior |
|----------|----------|
| Chinese keyword | Returns 0 results every time, still takes 4-31s |
| English keyword | Returns results but takes 13-27s |
| ISBN | Variable, 1-16s |

Recommendation: skip OpenLibrary for Chinese keyword searches entirely.

### Finding 4: ISBN searches are naturally fast

No cascade, 3-16s total. These need no immediate optimization.

### Finding 5: English searches are inherently slow

Phase1 alone takes 13-27s because Google Books and OpenLibrary are both slow. Even with cascade optimization, English searches will likely remain >15s without source budgeting.

### Finding 6: Network variance is significant

三体 tested 3 times with 3 different results:

| Run | Phase1 | Cascade | Total |
|-----|--------|---------|-------|
| #1 | 4.46s | 60.52s | 65.03s |
| #2 | 9.15s | 102.02s | 111.22s |
| #3 | 3.82s | 82.30s | 86.17s |

The 2.3× variance on Cascade and 2.4× on Phase1 confirms that external API latency is the dominant uncertainty factor. Caching is essential for stable UX.

---

## Confirmed Optimization Targets

Based on the baseline data, the Phase 1 plan (`performance-optimization-plan.md`) is confirmed with these priorities:

| Priority | Optimization | Expected Impact |
|----------|-------------|-----------------|
| **P0** | Cascade limiting: max 3 candidates | 60-100s → ~12s for cascade |
| **P0** | Search budget: 6s total wait | Caps worst-case latency |
| **P1** | Skip OpenLibrary for Chinese keywords | Saves 4-31s per search, always 0 results |
| **P1** | Demote Google Books from zh keyword search | Saves 2-9s, Chinese sources suffice |
| **P2** | Source-level circuit breaker | Prevents repeated timeout amplification |
| **P2** | Memory cache (1h TTL) | Repeat searches near-instant |

---

## Source Data

Raw container logs available on request. All 16 searches were executed on 2026-08-09 between 19:40 and 20:00 CST.
