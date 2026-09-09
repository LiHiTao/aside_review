"""Bounded, unauthenticated checks of public legal-document URLs.

``probe_legal_url`` accepts two test seams. ``resolver(host, port)`` returns
numeric IP strings. ``transport(url, *, timeout, max_bytes, addresses)`` returns
``ProbeResponse`` and must not follow redirects. Its addresses have already
been checked; the production transport pins connections to those addresses.
No browser, JavaScript, cookies, credentials or environment proxies are used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPHandler, HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request,
    build_opener,
)
import zlib


TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 5
MAX_BYTES = 1024 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MANUAL_CHECK = "请在可正常联网的设备上打开协议 URL，确认无需登录、验证或额外操作即可查看正文。"


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool = False
    incomplete: bool = False


class InvalidURL(ValueError):
    pass


def _normalize_url(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        raise InvalidURL("URL 为空或含空白、控制字符、反斜杠")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            raise InvalidURL("协议 URL 必须使用 HTTP(S)")
        if parts.username is not None or parts.password is not None:
            raise InvalidURL("协议 URL 不得包含认证信息")
        host = parts.hostname
        if not host or "%" in host:
            raise InvalidURL("URL 缺少有效主机名")
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port if parts.port is not None else (443 if parts.scheme.lower() == "https" else 80)
        if port < 1 or port > 65535:
            raise InvalidURL("URL 端口无效")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
            raise InvalidURL("不请求本机或内网主机")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise InvalidURL("URL 主机名无效")
        authority = f"[{host}]" if ":" in host else host
        if parts.port is not None:
            authority += f":{port}"
        normalized = urlunsplit((parts.scheme.lower(), authority,
                                quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                                quote(parts.query, safe="%/?@:!$&'()*+,;=-._~"), ""))
        return normalized, host, port
    except (ValueError, UnicodeError) as error:
        if isinstance(error, InvalidURL):
            raise
        raise InvalidURL("URL 格式或端口无效") from error


def _is_public(address: str) -> bool:
    try:
        if "%" in address:
            return False
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped:
                return _is_public(str(ip.ipv4_mapped))
            if ip.sixtofour and not _is_public(str(ip.sixtofour)):
                return False
            if ip.teredo:
                return False
        return ip.is_global and not (ip.is_multicast or ip.is_unspecified or ip.is_reserved)
    except ValueError:
        return False


def _resolve(host: str, port: int) -> list[str]:
    """Bound DNS waiting without letting a stuck platform resolver stall audits."""
    answers = queue.Queue(maxsize=1)

    def resolve():
        try:
            result = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            answers.put((list(dict.fromkeys(item[4][0] for item in result)), None))
        except Exception as error:
            answers.put((None, error))

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses, error = answers.get(timeout=TIMEOUT_SECONDS)
    except queue.Empty as error:
        raise TimeoutError("DNS 查询超时") from error
    if error:
        raise error
    return addresses


def _validated_addresses(host: str, port: int, resolver) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = tuple(dict.fromkeys(resolver(host, port)))
    else:
        addresses = (str(literal),)
    if not addresses:
        raise socket.gaierror("DNS 未返回地址")
    if not all(_is_public(address) for address in addresses):
        raise InvalidURL("URL 解析到本机、内网或非公网地址，未发送请求")
    return addresses


class _RequestDeadline:
    """Abort slow-drip headers/TLS too, rather than only timing each recv call."""
    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self.sockets = []
        self.lock = threading.Lock()
        self.expired = False
        self.timer = threading.Timer(timeout, self.abort)
        self.timer.daemon = True

    def __enter__(self):
        self.timer.start()
        return self

    def register(self, sock):
        with self.lock:
            if self.expired or time.monotonic() >= self.deadline:
                sock.close()
                raise TimeoutError("请求超过总时限")
            self.sockets.append(sock)

    def abort(self):
        with self.lock:
            self.expired = True
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def __exit__(self, *args):
        self.timer.cancel()


def _connect_pinned(addresses, port, timeout, request_deadline=None):
    deadline = time.monotonic() + timeout
    last_error = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("连接超时")
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if request_deadline:
                request_deadline.register(sock)
                remaining = min(remaining, request_deadline.deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("连接超时")
            sock.settimeout(remaining)
            # Numeric connect avoids a second DNS lookup and rebinding.
            sock.connect((address, port))
            return sock
        except OSError as error:
            sock.close()
            last_error = error
    raise last_error or OSError("无可用地址")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, addresses, request_deadline=None, **kwargs):
        self._addresses = addresses
        self._request_deadline = request_deadline
        super().__init__(*args, **kwargs)

    def connect(self):
        self.sock = _connect_pinned(self._addresses, self.port, self.timeout, self._request_deadline)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, addresses, request_deadline=None, **kwargs):
        self._addresses = addresses
        self._request_deadline = request_deadline
        super().__init__(*args, **kwargs)

    def connect(self):
        sock = _connect_pinned(self._addresses, self.port, self.timeout, self._request_deadline)
        try:
            # self.host is the URL hostname, never the pinned numeric address.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            if self._request_deadline:
                self._request_deadline.register(self.sock)
            self.sock.do_handshake()
        except Exception:
            if self.sock is not None:
                self.sock.close()
            sock.close()
            raise


class _PinnedHTTPHandler(HTTPHandler):
    def __init__(self, addresses, request_deadline=None):
        super().__init__()
        self.addresses = addresses
        self.request_deadline = request_deadline

    def http_open(self, request):
        return self.do_open(lambda host, **kw: _PinnedHTTPConnection(host, addresses=self.addresses, request_deadline=self.request_deadline, **kw), request)


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, addresses, request_deadline=None):
        super().__init__(context=ssl.create_default_context())
        self.addresses = addresses
        self.request_deadline = request_deadline

    def https_open(self, request):
        return self.do_open(lambda host, **kw: _PinnedHTTPSConnection(host, addresses=self.addresses, request_deadline=self.request_deadline, **kw),
                            request, context=self._context)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(url: str, *, timeout: int, max_bytes: int, addresses: tuple[str, ...]) -> ProbeResponse:
    with _RequestDeadline(timeout) as deadline:
        result = _transport_bounded(url, timeout=timeout, max_bytes=max_bytes, addresses=addresses, deadline=deadline)
        if deadline.expired or time.monotonic() >= deadline.deadline:
            raise TimeoutError("请求超过总时限")
        return result


def _transport_bounded(url, *, timeout, max_bytes, addresses, deadline):
    opener = build_opener(ProxyHandler({}), _NoRedirect(),
                          _PinnedHTTPHandler(addresses, deadline), _PinnedHTTPSHandler(addresses, deadline))
    request = Request(url, headers={
        "User-Agent": "ios-aside-review/legal-url-check",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
        "Accept-Encoding": "identity",
    }, method="GET")
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        status = response.code
        headers = dict(response.headers.items())
        if status in REDIRECT_STATUSES or not 200 <= status < 300:
            return ProbeResponse(status, headers, b"")
        chunks, size = [], 0
        while size <= max_bytes:
            remaining = deadline.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("响应读取超时")
            raw_sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if raw_sock is not None:
                raw_sock.settimeout(remaining)
            read = getattr(response, "read1", response.read)
            chunk = read(min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        body = b"".join(chunks)
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        content_length = normalized_headers.get("content-length")
        incomplete = False
        if content_length is not None:
            if not re.fullmatch(r"[0-9]+", content_length):
                raise http.client.HTTPException("无效 Content-Length")
            incomplete = size < int(content_length)
        return ProbeResponse(status, headers, body[:max_bytes], size > max_bytes, incomplete)


class _PageText(HTMLParser):
    EXCLUDED = {"script", "style", "noscript", "template", "head", "title", "svg", "canvas"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.has_script = False
        self.password = False
        self.challenge = False
        self.refresh = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.has_script |= tag == "script"
        self.password |= tag == "input" and attrs.get("type", "").lower() == "password"
        identifiers = " ".join(attrs.get(key, "") or "" for key in ("id", "class", "src", "action"))
        if re.search(r"(?:g-recaptcha|h-captcha|cf-chl-|challenge-platform|challenges\.cloudflare\.com|recaptcha/(?:api|enterprise))", identifiers, re.I):
            self.challenge = True
        self.refresh |= tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh"
        hidden = (tag in self.EXCLUDED or "hidden" in attrs or
                  re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attrs.get("style", "") or "", re.I) is not None)
        if tag not in self.VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not any(hidden for _, hidden in self.stack):
            self.text.append(data)


def _classify_content(response: ProbeResponse) -> tuple[str, str]:
    headers = {key.lower(): value for key, value in response.headers.items()}
    if response.truncated or len(response.body) > MAX_BYTES:
        return "NOT_VERIFIABLE", "响应超过 1 MiB 读取上限，无法确认完整页面"
    if response.incomplete:
        return "NOT_VERIFIABLE", "响应提前中断，正文长度小于 Content-Length，无法确认完整页面"
    body = response.body
    encoding = headers.get("content-encoding", "identity").lower().strip()
    if encoding not in {"", "identity"}:
        if encoding not in {"gzip", "deflate"}:
            return "NOT_VERIFIABLE", "响应压缩格式不受支持"
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
            body = decoder.decompress(body, MAX_BYTES + 1)
            if len(body) > MAX_BYTES or decoder.unconsumed_tail:
                return "NOT_VERIFIABLE", "解压后的响应超过 1 MiB 上限"
            if not decoder.eof or decoder.unused_data:
                return "NOT_VERIFIABLE", "压缩响应不完整或包含无法确认的附加内容"
        except zlib.error:
            return "NOT_VERIFIABLE", "压缩响应无法解析"
    if not body.strip():
        return "FAIL", "HTTP 响应成功但页面正文为空"
    content_type = Message()
    content_type["content-type"] = headers.get("content-type", "application/octet-stream")
    media_type = content_type.get_content_type()
    if media_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        return "NOT_VERIFIABLE", "响应不是可确认的 HTML 或纯文本页面"
    charset = content_type.get_content_charset() or "utf-8"
    try:
        text = body.decode("utf-8-sig" if charset.lower() == "utf-8" else charset)
    except (UnicodeError, LookupError):
        return "NOT_VERIFIABLE", "响应字符编码无法确认"
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        return "NOT_VERIFIABLE", "响应包含二进制或不可读控制字符"
    if media_type == "text/plain":
        return ("PASS", "HTTP 响应成功，已获取非空文本正文") if text.strip() else ("FAIL", "文本正文为空")
    parser = _PageText()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError):
        return "NOT_VERIFIABLE", "HTML 内容无法解析"
    visible = re.sub(r"\s+", " ", " ".join(parser.text)).strip()
    if parser.challenge:
        return "NOT_VERIFIABLE", "页面要求人机验证或返回反爬页面"
    if parser.password:
        return "NOT_VERIFIABLE", "页面包含登录验证表单，无法确认匿名可访问正文"
    if parser.refresh:
        return "NOT_VERIFIABLE", "页面依赖 HTML 自动跳转，尚未确认最终正文"
    challenge_clause = r"(?:just a moment|checking your browser|verify (?:that )?you are human|verifying you are human|enable javascript(?: and cookies)?(?: to continue)?)"
    if re.fullmatch(rf"(?:{challenge_clause}[.!… ]*)+", visible, re.I):
        return "NOT_VERIFIABLE", "页面是验证提示或要求 JavaScript 执行"
    if parser.has_script and re.fullmatch(r"(?:loading|please wait|loading (?:content|page)|正在加载|加载中)[.!…\s]*", visible, re.I):
        return "NOT_VERIFIABLE", "页面仅含 JavaScript 加载占位内容，无法确认正文"
    if not visible:
        if parser.has_script:
            return "NOT_VERIFIABLE", "仅返回 JavaScript 页面空壳，未执行脚本，无法确认正文"
        return "FAIL", "HTTP 响应成功但 HTML 页面没有可读正文"
    if not re.search(r"[^\W_]", visible, re.UNICODE):
        return "NOT_VERIFIABLE", "页面未包含可确认的可读正文"
    return "PASS", "HTTP 响应成功，已获取非空可读网页正文"


def probe_legal_url(url: str, *, transport=None, resolver=None) -> dict:
    """Return four-state audit evidence; a PASS is network evidence, not rendering."""
    transport = transport or _transport
    resolver = resolver or _resolve
    result = {
        "status": "NOT_VERIFIABLE", "actual": "", "manual_check": MANUAL_CHECK,
        "original_url": url, "final_url": url, "http_status": None,
        "content_type": "", "checked_at": datetime.now(timezone.utc).isoformat(),
        "redirects": [],
    }

    def finish(status, actual):
        result.update(status=status, actual=actual,
                      manual_check="" if status == "PASS" else MANUAL_CHECK)
        return result

    current, seen = url, set()
    try:
        for hop in range(MAX_REDIRECTS + 1):
            current, host, port = _normalize_url(current)
            result["final_url"] = current
            if current in seen:
                return finish("FAIL", "协议 URL 存在重定向循环")
            seen.add(current)
            addresses = _validated_addresses(host, port, resolver)
            response = transport(current, timeout=TIMEOUT_SECONDS, max_bytes=MAX_BYTES, addresses=addresses)
            headers = {key.lower(): value for key, value in response.headers.items()}
            result.update(http_status=response.status, content_type=headers.get("content-type", ""))
            if response.status in REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    return finish("FAIL", "重定向响应缺少有效 Location 地址")
                if re.search(r"[\s\x00-\x1f\x7f\\]", location):
                    return finish("FAIL", "重定向地址含无效字符")
                destination = urljoin(current, location)
                result["redirects"].append({"url": current, "http_status": response.status, "location": destination})
                if hop == MAX_REDIRECTS:
                    return finish("NOT_VERIFIABLE", "重定向超过 5 次，停止请求并需复核最终地址")
                current = destination
                continue
            if response.status in {404, 410}:
                return finish("FAIL", f"协议 URL 返回 HTTP {response.status}，页面不存在或已删除")
            if not 200 <= response.status < 300:
                return finish("NOT_VERIFIABLE", f"协议 URL 返回 HTTP {response.status}，无法确认页面可正常访问")
            return finish(*_classify_content(response))
    except InvalidURL as error:
        return finish("FAIL", str(error))
    except (TimeoutError, socket.timeout):
        return finish("NOT_VERIFIABLE", "协议 URL 请求或 DNS 查询超时")
    except ssl.SSLError:
        return finish("NOT_VERIFIABLE", "HTTPS 证书或 TLS 连接无法验证")
    except socket.gaierror:
        return finish("NOT_VERIFIABLE", "协议域名 DNS 解析失败")
    except (URLError, OSError, http.client.HTTPException):
        return finish("NOT_VERIFIABLE", "协议 URL 连接失败或响应中断")
    return finish("NOT_VERIFIABLE", "协议 URL 请求未完成")
