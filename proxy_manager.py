"""
proxy_manager — 代理健康检测与自动切换

每次搜索前自动检测主代理链路是否通畅，不通则切换备代理。
检测覆盖 TCP 连通性 + DNS 解析 + HTTPS 握手全链路。

用法:
    from .proxy_manager import get_proxies, get_current_proxy_info

    proxies = get_proxies()  # 返回 {"http": url, "https": url} 或 None
    resp = requests.get(url, proxies=proxies, verify=False)
"""
import time
import os

# ============================================================
# 代理列表（主 → 备）
# ============================================================
PROXY_LIST = [
    ("xray",   "http://192.168.1.249:20172"),   # 主代理：NAS 主机 xray 进程
    ("clash",  "http://192.168.1.249:17890"),   # 备代理：Docker mihomo 容器
]

# ============================================================
# 健康检测配置
# ============================================================
HEALTH_CHECK_URL = "https://www.google.com"     # 全链路验证目标
HEALTH_CHECK_TIMEOUT = 3                        # 单个代理检测超时（秒）
PROXY_CACHE_SECONDS = 60                        # 检测结果缓存时间

# 运行时状态
_active_proxy = None
_proxy_last_check = 0


# ---- 公开 API ----

def get_proxies():
    """
    获取当前可用代理配置。自动检测主备链路。

    返回:
        {"http": url, "https": url} — 选中代理
        None                         — 所有代理不可用，直连
    """
    global _active_proxy, _proxy_last_check

    now = time.time()
    if _active_proxy is not None and (now - _proxy_last_check) < PROXY_CACHE_SECONDS:
        return {"http": _active_proxy, "https": _active_proxy}

    # 按顺序检测主备
    for name, url in PROXY_LIST:
        if _check_proxy(name, url):
            _active_proxy = url
            _proxy_last_check = now
            os.environ["HTTP_PROXY"] = url
            os.environ["HTTPS_PROXY"] = url
            return {"http": url, "https": url}

    # 都不可用
    _active_proxy = None
    _proxy_last_check = now
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    return None


def get_current_proxy_info():
    """返回当前选中代理的描述（用于日志）"""
    if _active_proxy is None:
        return "直连（无代理）"
    for name, url in PROXY_LIST:
        if url == _active_proxy:
            return f"{name} ({url})"
    return f"未知 ({_active_proxy})"


# ---- 内部 ----

def _check_proxy(name, url):
    """
    代理全链路健康检测: TCP → DNS → TLS → HTTP

    步骤:
      1) TCP 端口探测 (≤1s) — 代理进程活着？
      2) HTTP GET 请求 (≤3s) — 代理能正常转发 DNS+TLS+HTTP？
    """
    try:
        # Step 1: TCP 端口探测
        import socket
        host, port = url.replace("http://", "").split(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, int(port)))
        sock.close()
        if result != 0:
            return False

        # Step 2: HTTP 全链路验证（DNS + TLS + HTTP）
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        proxies = {"http": url, "https": url}
        resp = requests.get(
            HEALTH_CHECK_URL,
            proxies=proxies,
            timeout=HEALTH_CHECK_TIMEOUT,
            verify=False,
        )
        return resp.status_code < 500

    except Exception:
        return False
