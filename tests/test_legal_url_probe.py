import gzip
import io
import os
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import legal_url_probe as probe
from legal_url_probe import ProbeResponse, probe_legal_url


URL = "https://legal.example.org/privacy"
PUBLIC_IP = "93.184.216.34"


class LegalURLTests(unittest.TestCase):
    def setUp(self):
        # Any unmocked network access is a test failure, including DNS.
        self.socket_guard = patch("socket.socket", side_effect=AssertionError("real network forbidden"))
        self.dns_guard = patch("socket.getaddrinfo", side_effect=AssertionError("real DNS forbidden"))
        self.socket_guard.start()
        self.dns_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.addCleanup(self.dns_guard.stop)
        self.calls = []
        self.resolutions = []

    def resolver(self, host, port):
        self.resolutions.append((host, port))
        return [PUBLIC_IP]

    def check(self, body=b"<html><body><h1>Privacy Policy</h1><p>We protect your data.</p></body></html>",
              status=200, headers=None, truncated=False, url=URL, resolver=None):
        response = ProbeResponse(status, headers or {"Content-Type": "text/html; charset=utf-8"}, body, truncated)

        def transport(url, **kwargs):
            self.calls.append((url, kwargs))
            return response

        return probe_legal_url(url, transport=transport, resolver=resolver or self.resolver)

    def test_get_contract_and_success_evidence(self):
        result = self.check()
        self.assertEqual("PASS", result["status"])
        self.assertEqual(URL, result["original_url"])
        self.assertEqual(URL, result["final_url"])
        self.assertEqual(200, result["http_status"])
        self.assertTrue(result["checked_at"].endswith("+00:00"))
        self.assertEqual("", result["manual_check"])
        self.assertEqual({"timeout": 10, "max_bytes": 1048576, "addresses": (PUBLIC_IP,)}, self.calls[0][1])

    def test_html_plain_xhtml_unicode_and_content_after_comments(self):
        for content, content_type in [
            (b"Terms apply.", "text/plain"),
            ("<p>隐私协议</p>".encode(), "application/xhtml+xml"),
            (b"<!-- ignore --><article>Terms</article>", "text/html"),
            (b"\xef\xbb\xbf<p>Terms</p>", "text/html"),
        ]:
            with self.subTest(content=content):
                self.assertEqual("PASS", self.check(content, headers={"Content-Type": content_type})["status"])

    def test_status_classification(self):
        for status, expected in [(201, "PASS"), (204, "FAIL"), (404, "FAIL"), (410, "FAIL"),
                                 (401, "NOT_VERIFIABLE"), (403, "NOT_VERIFIABLE"),
                                 (429, "NOT_VERIFIABLE"), (500, "NOT_VERIFIABLE"), (503, "NOT_VERIFIABLE"),
                                 (304, "NOT_VERIFIABLE")]:
            with self.subTest(status=status):
                body = b"" if status == 204 else b"<p>Terms</p>"
                self.assertEqual(expected, self.check(body, status=status)["status"])

    def test_blank_html_excludes_title_style_and_hidden_text(self):
        for body in [b"", b" \r\n ", b"<html><head><title>Privacy</title><style>body{}</style></head><body></body></html>",
                     b"<title>Privacy Policy</title>", b"<!-- text -->", b"<p hidden>hidden</p>", b'<p style="display: none">hidden</p>',
                     b"<noscript>Enable JS</noscript>", b"<template>Not rendered</template>"]:
            with self.subTest(body=body):
                self.assertEqual("FAIL", self.check(body)["status"])

    def test_js_shell_including_head_script(self):
        for body in [b'<div id="app"></div><script src="app.js"></script>',
                     b'<html><head><script src="app.js"></script></head><body></body></html>',
                     b'<script>document.write("Terms")</script><noscript>Enable JavaScript</noscript>',
                     b'<body><div id="root"></div><script src="main.js"></script><noscript>Enable JS</noscript>Loading...</body>']:
            with self.subTest(body=body):
                self.assertEqual("NOT_VERIFIABLE", self.check(body)["status"])
        self.assertEqual("PASS", self.check(b'<article>Privacy Policy.</article><script src="app.js"></script>')["status"])

    def test_login_captcha_and_browser_challenge_templates(self):
        for body in [b'<form><h1>Sign in</h1><input type="password"></form>',
                     b'<h1>Verify human</h1><div class="g-recaptcha"></div>',
                     b'<h1>Just a moment...</h1>',
                     b'<body>Just a moment... <p>Enable JavaScript and cookies to continue</p></body>',
                     b'<script src="/cdn-cgi/challenge-platform/h/b.js"></script>',
                     b'<form><input type="password"></form>' + b'<p>Sign in required.</p>' * 100,
                     b'<p>Enable JavaScript to continue.</p>']:
            with self.subTest(body=body):
                self.assertEqual("NOT_VERIFIABLE", self.check(body)["status"])

    def test_legal_prose_mentions_login_captcha_without_false_positive(self):
        for body in [b'<h1>Privacy Policy</h1><p>We store login times and may use captcha to prevent abuse.</p>',
                     b'<p>Our login and password policy explains how to verify you are human.</p>',
                     b'<p>We use Cloudflare security services.</p>']:
            with self.subTest(body=body):
                self.assertEqual("PASS", self.check(body)["status"])

    def test_binary_unknown_encoding_and_bad_compression_need_review(self):
        for body, headers in [(b"PDFdata", {"Content-Type": "application/pdf"}),
                              (b"<p>Terms</p>", {}),
                              (b"\xff", {"Content-Type": "text/html; charset=utf-8"}),
                              (b"text", {"Content-Type": "text/html; charset=unknown-encoding"}),
                              (b"\x00text", {"Content-Type": "text/plain"}),
                              (b"compressed", {"Content-Type": "text/html", "Content-Encoding": "br"}),
                              (b"bad", {"Content-Type": "text/html", "Content-Encoding": "gzip"})]:
            with self.subTest(headers=headers):
                # An absent content type is deliberately explicit.
                response = ProbeResponse(200, headers, body)
                self.assertEqual("NOT_VERIFIABLE", probe_legal_url(URL, transport=lambda *a, **kw: response, resolver=self.resolver)["status"])

    def test_size_limit_and_bounded_gzip_deflate(self):
        self.assertEqual("NOT_VERIFIABLE", self.check(b"<p>Terms</p>", truncated=True)["status"])
        self.assertEqual("NOT_VERIFIABLE", self.check(b"a" * (probe.MAX_BYTES + 1))["status"])
        for encoding, compress in [("gzip", gzip.compress), ("deflate", zlib.compress)]:
            with self.subTest(encoding=encoding):
                headers = {"Content-Type": "text/html", "Content-Encoding": encoding}
                self.assertEqual("PASS", self.check(compress(b"<p>Terms</p>"), headers=headers)["status"])
                self.assertEqual("NOT_VERIFIABLE", self.check(compress(b"a" * (probe.MAX_BYTES + 1)), headers=headers)["status"])
                self.assertEqual("NOT_VERIFIABLE", self.check(compress(b"<p>Terms</p>")[:-2], headers=headers)["status"])

    def test_bad_url_never_connects(self):
        for url in ["", " ", "file:///tmp/privacy", "javascript:alert(1)", "mailto:x@y.com",
                    "http:///policy", "https://user:password@example.org/privacy", "https://user@example.org",
                    "http://example.org:0", "http://example.org:65536", "http://example.org:notaport",
                    "https://example.org\\@localhost/x", "https://example.org/\nsecret", "http://[::1",
                    "http://bad_host.example/policy", "http://[fe80::1%25en0]/"]:
            with self.subTest(url=url):
                result = self.check(url=url)
                self.assertEqual("FAIL", result["status"])
        self.assertEqual([], self.calls)

    def test_nonpublic_ipv4_ipv6_and_local_names_never_connect(self):
        for host in ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                     "0.0.0.0", "224.0.0.1", "192.0.2.1", "[::1]", "[::]", "[fc00::1]", "[fe80::1]",
                     "[::ffff:127.0.0.1]", "[2002:7f00:0001::]", "[2001:db8::1]", "localhost", "LOCALHOST.",
                     "other.localhost", "router.local", "internal.home"]:
            with self.subTest(host=host):
                self.assertEqual("FAIL", self.check(url=f"https://{host}/privacy")["status"])
        self.assertEqual([], self.calls)

    def test_private_dns_answer_and_mixed_public_private_answers_rejected(self):
        for addresses in [["127.0.0.1"], [PUBLIC_IP, "10.0.0.1"], ["::ffff:192.168.1.1"], ["224.0.0.1"], ["bad"]]:
            with self.subTest(addresses=addresses):
                self.assertEqual("FAIL", self.check(resolver=lambda *a: addresses)["status"])
        self.assertEqual([], self.calls)

    def test_public_ipv4_ipv6_and_idna_hosts(self):
        for url in [f"https://{PUBLIC_IP}/privacy", "https://[2606:4700:4700::1111]/privacy",
                    "https://例子.com/隐私", "https://legal.example.org.:443/privacy"]:
            with self.subTest(url=url):
                self.assertEqual("PASS", self.check(url=url)["status"])
        self.assertTrue(any("xn--" in call[0] for call in self.calls))

    def test_relative_and_cross_host_redirects_are_checked_every_hop(self):
        responses = [ProbeResponse(301, {"Location": "/redirect"}, b""),
                     ProbeResponse(302, {"Location": "https://other.example.org/terms"}, b""),
                     ProbeResponse(200, {"Content-Type": "text/html"}, b"<p>Terms</p>")]

        def transport(url, **kwargs):
            self.calls.append((url, kwargs))
            return responses[len(self.calls) - 1]

        result = probe_legal_url(URL, transport=transport, resolver=self.resolver)
        self.assertEqual("PASS", result["status"])
        self.assertEqual("https://other.example.org/terms", result["final_url"])
        self.assertEqual(2, len(result["redirects"]))
        self.assertEqual(3, len(self.resolutions))

    def test_redirect_ssrf_credentials_and_local_alias_are_not_requested(self):
        for location in ["http://127.0.0.1/secrets", "http://[::ffff:127.0.0.1]/secrets",
                         "http://metadata.example.org/secrets", "file:///etc/passwd",
                         "https://user:secret@example.org", "http://localhost/p", " /invalid"]:
            with self.subTest(location=location):
                self.calls.clear()

                def transport(url, **kwargs):
                    self.calls.append(url)
                    return ProbeResponse(302, {"Location": location}, b"")

                def resolver(host, port):
                    return ["169.254.169.254"] if host == "metadata.example.org" else [PUBLIC_IP]

                result = probe_legal_url(URL, transport=transport, resolver=resolver)
                self.assertEqual("FAIL", result["status"])
                self.assertEqual([URL], self.calls)

    def test_redirect_loop_missing_location_and_limit(self):
        self.assertEqual("FAIL", self.check(status=302)["status"])
        self.assertEqual("FAIL", self.check(status=302, headers={"Location": URL})["status"])

        def transport(url, **kwargs):
            self.calls.append(url)
            return ProbeResponse(307, {"Location": f"/step{len(self.calls)}"}, b"")

        self.calls.clear()
        result = probe_legal_url(URL, transport=transport, resolver=self.resolver)
        self.assertEqual("NOT_VERIFIABLE", result["status"])
        self.assertEqual(6, len(self.calls))

    def test_exactly_five_redirects_can_pass(self):
        def transport(url, **kwargs):
            self.calls.append(url)
            if len(self.calls) <= 5:
                return ProbeResponse(308, {"Location": f"/step{len(self.calls)}"}, b"")
            return ProbeResponse(200, {"Content-Type": "text/plain"}, b"Terms")

        self.assertEqual("PASS", probe_legal_url(URL, transport=transport, resolver=self.resolver)["status"])
        self.assertEqual(6, len(self.calls))

    def test_network_dns_tls_and_incomplete_response_errors_need_review(self):
        for error in [TimeoutError(), socket.gaierror(), ssl.SSLCertVerificationError(),
                      ConnectionRefusedError(), URLError("certificate failed"),
                      probe.http.client.IncompleteRead(b"text")]:
            with self.subTest(error=error):
                def transport(*a, **kw):
                    raise error

                result = probe_legal_url(URL, transport=transport, resolver=self.resolver)
                self.assertEqual("NOT_VERIFIABLE", result["status"])
                self.assertTrue(result["manual_check"])
        self.assertEqual("NOT_VERIFIABLE", self.check(resolver=lambda *a: [])["status"])

    def test_meta_refresh_needs_review(self):
        self.assertEqual("NOT_VERIFIABLE", self.check(b'<meta http-equiv="refresh" content="0;url=/login"><p>Moving</p>')["status"])

    def test_default_resolver_timeout_is_bounded(self):
        with patch.object(probe.threading, "Thread") as thread, patch.object(probe.queue, "Queue") as queue:
            queue.return_value.get.side_effect = probe.queue.Empty
            with self.assertRaises(TimeoutError):
                probe._resolve("example.org", 443)
            queue.return_value.get.assert_called_once_with(timeout=10)
            self.assertTrue(thread.call_args.kwargs["daemon"])


class TransportTests(unittest.TestCase):
    def test_numeric_connect_does_not_resolve_again(self):
        sock = MagicMock()
        with patch.object(probe.socket, "socket", return_value=sock) as factory, patch.object(probe.socket, "getaddrinfo", side_effect=AssertionError("DNS rebinding")):
            result = probe._connect_pinned((PUBLIC_IP,), 443, 10)
        self.assertIs(sock, result)
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect.assert_called_once_with((PUBLIC_IP, 443))

    def test_https_keeps_original_tls_hostname_and_verification(self):
        sock = MagicMock()
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        with patch.object(probe, "_connect_pinned", return_value=sock), patch.object(context, "wrap_socket", return_value=sock) as wrap:
            conn = probe._PinnedHTTPSConnection("legal.example.org", addresses=(PUBLIC_IP,), timeout=10, context=context)
            conn.connect()
        wrap.assert_called_once_with(sock, server_hostname="legal.example.org", do_handshake_on_connect=False)
        sock.do_handshake.assert_called_once()

    def test_deadline_aborts_registered_socket_and_blocks_late_connection(self):
        with patch.object(probe.threading, "Timer") as timer:
            with probe._RequestDeadline(10) as deadline:
                sock = MagicMock()
                deadline.register(sock)
                deadline.abort()
                sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
                late_sock = MagicMock()
                with self.assertRaises(TimeoutError):
                    deadline.register(late_sock)
                late_sock.close.assert_called_once()
            timer.return_value.start.assert_called_once()
            timer.return_value.cancel.assert_called_once()

    def test_production_opener_has_no_proxy_auth_cookie_or_auto_redirect(self):
        class Response:
            code = 200
            headers = {"Content-Type": "text/html", "Set-Cookie": "session=secret"}
            fp = None

            def __init__(self):
                self.stream = io.BytesIO(b"<p>Terms</p>")
                self.read_sizes = []

            def read(self, size):
                self.read_sizes.append(size)
                return self.stream.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        response = Response()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://user:secret@127.0.0.1:8888"}), patch.object(probe, "build_opener", return_value=opener) as build:
            result = probe._transport(URL, timeout=10, max_bytes=probe.MAX_BYTES, addresses=(PUBLIC_IP,))
        handlers = build.call_args.args
        self.assertEqual({}, handlers[0].proxies)
        self.assertIsInstance(handlers[1], probe._NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual("GET", request.get_method())
        for key in ["Authorization", "Proxy-authorization", "Cookie"]:
            self.assertIsNone(request.get_header(key))
        self.assertEqual("identity", request.get_header("Accept-encoding"))
        self.assertEqual(10, opener.open.call_args.kwargs["timeout"])
        self.assertEqual(b"<p>Terms</p>", result.body)
        self.assertTrue(all(size <= 65536 for size in response.read_sizes))

    def test_bounded_transport_read_and_response_close(self):
        response = MagicMock()
        response.code = 200
        response.headers = {"Content-Type": "text/plain"}
        response.__enter__.return_value = response
        response.read1.side_effect = [b"a" * 8, b"b"]
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(probe, "build_opener", return_value=opener):
            result = probe._transport(URL, timeout=10, max_bytes=8, addresses=(PUBLIC_IP,))
        self.assertTrue(result.truncated)
        self.assertEqual(b"a" * 8, result.body)
        self.assertEqual([9, 1], [call.args[0] for call in response.read1.call_args_list])
        response.__exit__.assert_called_once()

    def test_http_errors_are_returned_and_redirect_body_not_read(self):
        body = io.BytesIO(b"huge body")
        error = HTTPError(URL, 302, "Found", {"Location": "/terms"}, body)
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.object(probe, "build_opener", return_value=opener):
            result = probe._transport(URL, timeout=10, max_bytes=8, addresses=(PUBLIC_IP,))
        self.assertEqual(302, result.status)
        self.assertEqual(b"", result.body)
        self.assertTrue(body.closed)

    def test_premature_eof_is_incomplete_not_success(self):
        response = MagicMock()
        response.code = 200
        response.headers = {"Content-Type": "text/plain", "Content-Length": "100"}
        response.__enter__.return_value = response
        response.read1.side_effect = [b"Terms", b""]
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(probe, "build_opener", return_value=opener):
            result = probe._transport(URL, timeout=10, max_bytes=probe.MAX_BYTES, addresses=(PUBLIC_IP,))
        self.assertTrue(result.incomplete)
        self.assertFalse(result.truncated)
        status, actual = probe._classify_content(result)
        self.assertEqual("NOT_VERIFIABLE", status)
        self.assertIn("提前中断", actual)
        self.assertNotIn("1 MiB", actual)

    def test_deadline_eof_is_timeout_not_empty_page(self):
        def bounded(*args, deadline, **kwargs):
            deadline.expired = True
            return ProbeResponse(200, {"Content-Type": "text/html"}, b"")

        with patch.object(probe, "_transport_bounded", side_effect=bounded):
            with self.assertRaises(TimeoutError):
                probe._transport(URL, timeout=10, max_bytes=probe.MAX_BYTES, addresses=(PUBLIC_IP,))


if __name__ == "__main__":
    unittest.main()
