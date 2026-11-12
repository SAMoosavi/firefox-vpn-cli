"""Acceptance tests for fflingling: proxy, rotation, replay, certificates.

No network and no ffvpn are needed. The upstream connector and the country
rotator are injected, as the proxy exposes them; the CONNECT dispatch is driven
with fake sockets.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fflingling")


def load_script():
    loader = importlib.machinery.SourceFileLoader("fflingling_cli", SCRIPT)
    spec = importlib.util.spec_from_file_location(
        "fflingling_cli", SCRIPT, loader=loader
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ffl = load_script()


# --- fakes -----------------------------------------------------------------


class FakeSocket:
    def __init__(self, incoming=b""):
        self.buf = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def recv(self, size):
        if not self.buf:
            return b""
        chunk = bytes(self.buf[:size])
        del self.buf[:size]
        return chunk

    def sendall(self, data):
        self.sent += data

    def settimeout(self, timeout):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class FragmentedSocket(FakeSocket):
    """A socket that hands out at most one byte per recv, like a split packet."""

    def recv(self, size):
        return super().recv(min(size, 1))


class Scripted:
    """A connector handing out one prepared upstream per attempt."""

    def __init__(self, responses, make=FakeSocket):
        self.responses = list(responses)
        self.make = make
        self.connects = []
        self.upstreams = []

    def __call__(self, host, port, tls):
        self.connects.append((host, port, tls))
        incoming = b""
        if self.responses:
            incoming = self.responses.pop(0)
        upstream = self.make(incoming)
        self.upstreams.append(upstream)
        return upstream


def temp_log():
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "fflingling.log")
    return ffl.Log(path), path


def make_relay(connect=None, rotate=None, pool=("FI", "SE"), country="FI",
               retries=3, rotate_wait=120.0, mitm=False, server_context=None,
               connect_proxy=None):
    log, _ = temp_log()
    return ffl.Relay(
        ("127.0.0.1", 1),
        list(pool),
        country=country,
        log=log,
        mitm=mitm,
        server_context=server_context,
        connect=connect or (lambda host, port, tls: FakeSocket()),
        connect_country=rotate or (lambda code: None),
        connect_proxy=connect_proxy,
        retries=retries,
        rotate_wait=rotate_wait,
    )


def request(body=b"", headers=b"Host: api.opencode.ai\r\n", method=b"POST",
            path=b"/v1/chat"):
    lowered = headers.lower()
    if body and b"content-length" not in lowered \
            and b"transfer-encoding" not in lowered:
        headers += f"Content-Length: {len(body)}\r\n".encode()
    return (
        method + b" " + path + b" HTTP/1.1\r\n" + headers + b"\r\n" + body
    )


def response(status, body=b"", length=None):
    if length is None:
        length = len(body)
    return (
        f"HTTP/1.1 {status} Test\r\nContent-Length: {length}\r\n\r\n".encode()
        + body
    )


def log_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


# --- AC2: host matching ----------------------------------------------------


class HostMatchTest(unittest.TestCase):
    def test_opencode_hosts_are_intercepted(self):
        for host in ("opencode.ai", "api.opencode.ai",
                     "OPENCODE.AI", "opencode.ai."):
            self.assertTrue(ffl.is_mitm_host(host), host)

    def test_other_hosts_are_not(self):
        for host in ("opencode.ai.evil.test", "example.com", "notopencode.ai",
                     "opencode.ai.example.com", "a.b.opencode.ai", "", None):
            self.assertFalse(ffl.is_mitm_host(host), host)

    def test_handle_dispatches_opencode_to_the_mitm(self):
        relay = make_relay(mitm=True, server_context=object())
        for host in ("opencode.ai", "api.opencode.ai"):
            client = FakeSocket(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
            with mock.patch.object(relay, "_handle_mitm") as mitm:
                relay.handle(client)
            mitm.assert_called_once_with(client, host, 443)
            self.assertTrue(
                client.sent.startswith(b"HTTP/1.1 200 Connection Established")
            )

    def test_handle_blind_tunnels_everything_else(self):
        connect = Scripted([b"pong"])
        relay = make_relay(connect=connect)
        client = FakeSocket(b"CONNECT example.com:443 HTTP/1.1\r\n\r\nping")
        relay.handle(client)
        self.assertEqual(connect.connects, [("example.com", 443, False)])
        self.assertTrue(
            client.sent.startswith(b"HTTP/1.1 200 Connection Established")
        )
        self.assertTrue(bytes(client.sent).endswith(b"pong"))
        self.assertEqual(bytes(connect.upstreams[0].sent), b"ping")

    def test_evil_suffix_is_a_blind_tunnel(self):
        connect = Scripted([b""])
        relay = make_relay(connect=connect)
        client = FakeSocket(
            b"CONNECT opencode.ai.evil.test:443 HTTP/1.1\r\n\r\n"
        )
        with mock.patch.object(relay, "_handle_mitm") as mitm:
            relay.handle(client)
        mitm.assert_not_called()
        self.assertEqual(
            connect.connects, [("opencode.ai.evil.test", 443, False)]
        )


class ExceptionLeakTest(unittest.TestCase):
    def test_a_failure_in_the_mitm_never_escapes_handle(self):
        relay = make_relay(mitm=True, server_context=object())
        client = FakeSocket(b"CONNECT opencode.ai:443 HTTP/1.1\r\n\r\n")
        seen = []

        def record(args):
            seen.append(args)

        original = threading.excepthook
        threading.excepthook = record
        try:
            with mock.patch.object(
                relay, "_handle_mitm", side_effect=ValueError("boom")
            ):
                thread = threading.Thread(
                    target=relay.handle, args=(client,), daemon=True
                )
                thread.start()
                thread.join(5)
        finally:
            threading.excepthook = original

        self.assertFalse(thread.is_alive())
        self.assertEqual(seen, [])
        self.assertTrue(client.closed)
        self.assertIn("boom", log_text(relay.log.path))


class PlainHttpTest(unittest.TestCase):
    def test_absolute_uri_request_is_forwarded_raw(self):
        proxied = []
        upstream = FakeSocket(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"
        )

        def connect_proxy():
            proxied.append(True)
            return upstream

        tunnel = Scripted([])
        relay = make_relay(connect=tunnel, connect_proxy=connect_proxy)
        client = FakeSocket(
            b"GET http://example.com/x HTTP/1.1\r\n"
            b"Host: example.com\r\n\r\n"
        )

        relay.handle(client)

        self.assertEqual(proxied, [True])
        self.assertEqual(tunnel.connects, [])
        self.assertTrue(bytes(upstream.sent).startswith(
            b"GET http://example.com/x HTTP/1.1\r\n"
        ))
        self.assertTrue(bytes(client.sent).startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(bytes(client.sent).endswith(b"hi"))


# --- retry set / rotation choice ------------------------------------------


class RetrySetTest(unittest.TestCase):
    def test_exactly_the_four_statuses(self):
        self.assertEqual(ffl.RETRYABLE, frozenset({429, 502, 503, 504}))
        for status in (429, 502, 503, 504):
            self.assertTrue(ffl.retryable(status))
        for status in (200, 403, 404, 500, 501):
            self.assertFalse(ffl.retryable(status))


class NextCountryTest(unittest.TestCase):
    def test_picks_a_different_country_and_wraps(self):
        pool = ["FI", "SE", "DE"]
        self.assertEqual(ffl.next_country(pool, "FI"), "SE")
        self.assertEqual(ffl.next_country(pool, "SE"), "DE")
        self.assertEqual(ffl.next_country(pool, "DE"), "FI")

    def test_country_outside_the_pool_starts_at_the_front(self):
        self.assertEqual(ffl.next_country(["FI", "SE"], "US"), "FI")

    def test_a_single_code_has_nowhere_to_go(self):
        self.assertIsNone(ffl.next_country(["FI"], "FI"))


# --- AC4 / AC5: retry, replay, pass-through -------------------------------


class RetryTest(unittest.TestCase):
    def test_429_rotates_and_replays_an_identical_request(self):
        body = b'{"model":"x"}'
        connect = Scripted([response(429), response(200, b"ok")])
        rotations = []
        relay = make_relay(connect=connect, rotate=rotations.append)
        client = FakeSocket(request(body, headers=(
            b"Host: api.opencode.ai\r\nContent-Type: application/json\r\n"
        )))

        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertEqual(rotations, ["SE"])
        self.assertEqual(len(connect.upstreams), 2)
        first = bytes(connect.upstreams[0].sent)
        second = bytes(connect.upstreams[1].sent)
        self.assertEqual(first, second)
        self.assertEqual(first.split(b"\r\n\r\n", 1)[1], body)
        self.assertIn(b"Connection: close", first)
        self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))
        self.assertTrue(bytes(client.sent).endswith(b"ok"))

    def test_each_retryable_status_is_replayed(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                connect = Scripted([response(status), response(200, b"ok")])
                relay = make_relay(connect=connect, rotate=lambda code: None)
                client = FakeSocket(request(b"{}"))
                relay._serve_request(client, "api.opencode.ai", 443)
                self.assertEqual(len(connect.upstreams), 2)
                self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))

    def test_other_statuses_are_passed_through_unretried(self):
        for status in (200, 403, 404, 500):
            with self.subTest(status=status):
                connect = Scripted([response(status, b"no")])
                rotations = []
                relay = make_relay(connect=connect, rotate=rotations.append)
                client = FakeSocket(request(b"{}"))
                relay._serve_request(client, "api.opencode.ai", 443)
                self.assertEqual(rotations, [])
                self.assertEqual(len(connect.upstreams), 1)
                self.assertTrue(
                    client.sent.startswith(f"HTTP/1.1 {status}".encode())
                )
                self.assertTrue(bytes(client.sent).endswith(b"no"))

    def test_replays_are_capped_by_the_retry_count(self):
        connect = Scripted([response(429)] * 3)
        relay = make_relay(connect=connect, rotate=lambda code: None, retries=1)
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)
        self.assertEqual(len(connect.upstreams), 2)
        self.assertTrue(client.sent.startswith(b"HTTP/1.1 429"))

    def test_a_single_code_pool_hands_back_the_last_response(self):
        connect = Scripted([response(429)])
        relay = make_relay(
            connect=connect, rotate=lambda code: None,
            pool=("FI",), country="FI", retries=3,
        )
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)
        self.assertEqual(len(connect.upstreams), 1)
        self.assertTrue(client.sent.startswith(b"HTTP/1.1 429"))

    def test_a_failed_connect_tries_the_code_after_it(self):
        connect = Scripted([response(429), response(200, b"ok")])
        rotations = []

        def rotate(code):
            rotations.append(code)
            if code == "SE":
                raise ffl.FflinglingError("SE is down")

        relay = make_relay(
            connect=connect, rotate=rotate, pool=("FI", "SE", "DE"),
        )
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)
        self.assertEqual(rotations, ["SE", "DE"])
        self.assertEqual(relay.country, "DE")
        self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))


# --- AC6: one rotation for concurrent 429s --------------------------------


class ConcurrentRotationTest(unittest.TestCase):
    def test_two_429s_produce_one_rotation(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        attempts = {}

        def connect(host, port, tls):
            ident = threading.get_ident()
            with lock:
                count = attempts.get(ident, 0) + 1
                attempts[ident] = count
            if count == 1:
                barrier.wait(timeout=5)
                return FakeSocket(response(429))
            return FakeSocket(response(200, b"ok"))

        rotations = []
        relay = make_relay(
            connect=connect,
            rotate=lambda code: rotations.append(code),
            retries=3,
            rotate_wait=5,
        )
        clients = [FakeSocket(request(b"{}")), FakeSocket(request(b"{}"))]
        threads = [
            threading.Thread(
                target=relay._serve_request,
                args=(client, "api.opencode.ai", 443),
            )
            for client in clients
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(rotations, ["SE"])
        for client in clients:
            self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))


# --- AC7: rotation waits for in-flight streams ----------------------------


class RotationWaitTest(unittest.TestCase):
    def test_waits_then_gives_up_at_the_limit(self):
        relay = make_relay(
            rotate=lambda code: None,
            rotate_wait=0.1,
            pool=("FI", "SE"),
        )
        release = threading.Event()
        entered = threading.Event()

        def stream():
            relay._enter()
            entered.set()
            release.wait(5)
            relay._leave()

        result = {}

        def rotate():
            relay._enter()
            result["value"] = relay._rotate("FI", 0)
            relay._leave()

        stream_thread = threading.Thread(target=stream)
        stream_thread.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        rotate_thread = threading.Thread(target=rotate)
        rotate_thread.start()
        rotate_thread.join(timeout=5)
        elapsed = time.monotonic() - started
        release.set()
        stream_thread.join(timeout=5)

        self.assertFalse(rotate_thread.is_alive())
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertEqual(result["value"][0], True)
        self.assertEqual(result["value"][1], "SE")
        self.assertTrue(result["value"][2])
        self.assertIn("gave up", log_text(relay.log.path))


# --- AC7: idle connections must not block a rotation ----------------------


class IdleConnectionTest(unittest.TestCase):
    def test_a_client_with_no_head_does_not_delay_rotation(self):
        relay = make_relay(rotate_wait=5.0, rotate=lambda code: None)
        release = threading.Event()
        waiting = threading.Event()

        class IdleSocket(FakeSocket):
            def recv(self, size):
                waiting.set()
                release.wait(5)
                return b""

        client = IdleSocket()
        idle = threading.Thread(
            target=relay._serve_request,
            args=(client, "api.opencode.ai", 443),
            daemon=True,
        )
        idle.start()
        self.assertTrue(waiting.wait(2))

        result = {}

        def rotate():
            relay._enter()
            try:
                result["value"] = relay._rotate("FI", 0)
            finally:
                relay._leave()

        started = time.monotonic()
        rotating = threading.Thread(target=rotate)
        rotating.start()
        rotating.join(timeout=5)
        elapsed = time.monotonic() - started
        release.set()
        idle.join(timeout=5)

        self.assertFalse(rotating.is_alive())
        self.assertFalse(idle.is_alive())
        self.assertEqual(result["value"], (True, "SE", True))
        self.assertLess(elapsed, 1.0)
        self.assertNotIn("gave up", log_text(relay.log.path))


# --- AC8: chunked bodies ---------------------------------------------------


class ChunkedBodyTest(unittest.TestCase):
    def test_chunked_body_is_replayed_byte_for_byte(self):
        body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        connect = Scripted([response(429), response(200, b"ok")])
        relay = make_relay(connect=connect, rotate=lambda code: None)
        client = FakeSocket(request(
            body,
            headers=(
                b"Host: api.opencode.ai\r\n"
                b"Transfer-Encoding: chunked\r\n"
            ),
        ))

        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertEqual(len(connect.upstreams), 2)
        first = bytes(connect.upstreams[0].sent)
        second = bytes(connect.upstreams[1].sent)
        self.assertEqual(first, second)
        self.assertIn(b"Transfer-Encoding: chunked", first)
        self.assertEqual(first.split(b"\r\n\r\n", 1)[1], body)
        self.assertTrue(bytes(client.sent).endswith(b"ok"))


# --- AC3: certificates -----------------------------------------------------


@unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
class CertificateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_generated_once_and_sans_are_right(self):
        ca, leaf, key = ffl.ensure_certs(self.directory)
        self.assertTrue(os.path.exists(ca))
        self.assertTrue(os.path.exists(leaf))
        self.assertTrue(os.path.exists(key))

        with open(leaf, "rb") as handle:
            before = handle.read()
        self.assertEqual(ffl.ensure_certs(self.directory), (ca, leaf, key))
        with open(leaf, "rb") as handle:
            self.assertEqual(handle.read(), before, "leaf was regenerated")

        text = subprocess.run(
            ["openssl", "x509", "-in", leaf, "-noout", "-text"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        self.assertIn("DNS:opencode.ai", text)
        self.assertIn("DNS:*.opencode.ai", text)
        self.assertEqual(subprocess.run(
            ["openssl", "verify", "-CAfile", ca, leaf],
            capture_output=True,
        ).returncode, 0)


# --- AC1 / AC10: opencode wiring and --no-mitm ----------------------------


class StubRelay:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        StubRelay.instances.append(self)

    def start(self):
        self.started = True
        return 4321

    def stop(self):
        self.stopped = True


class OpencodeWiringTest(unittest.TestCase):
    def setUp(self):
        StubRelay.instances = []
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.env = mock.patch.dict(
            os.environ,
            {"FFLINGLING_DATA_DIR": self.directory,
             "FFLINGLING_COUNTRY": "", "FFLINGLING_RETRIES": "",
             "FFLINGLING_ROTATE_WAIT_S": ""},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_main(self, argv, *, no_mitm=False):
        captured = {}

        def run_opencode(binary, args, env):
            captured.update(binary=binary, args=args, env=env)
            return 7

        connect_patch = mock.patch.object(ffl, "connect_country")
        connect = connect_patch.start()
        self.addCleanup(connect_patch.stop)
        patches = [
            mock.patch.object(ffl, "Relay", StubRelay),
            mock.patch.object(ffl, "find_ffvpn", return_value="/bin/ffvpn"),
            mock.patch.object(ffl, "list_countries", return_value=["FI", "SE"]),
            mock.patch.object(ffl, "proxy_address",
                              return_value=("127.0.0.1", 10810)),
            mock.patch.object(ffl, "opencode_binary",
                              return_value="/bin/opencode"),
            mock.patch.object(ffl, "run_opencode", side_effect=run_opencode),
        ]
        if no_mitm:
            patches.append(mock.patch.object(
                ffl, "ensure_certs", side_effect=AssertionError("CA used")
            ))
            patches.append(mock.patch.object(
                ffl, "server_context", side_effect=AssertionError("CA used")
            ))
        else:
            patches.append(mock.patch.object(
                ffl, "ensure_certs",
                return_value=("/data/ca.pem", "/data/leaf.pem", "/data/key.pem"),
            ))
            patches.append(mock.patch.object(
                ffl, "server_context", return_value="context"
            ))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ffl.main(argv)
        return code, out.getvalue(), captured, connect

    def test_starts_vpn_proxy_and_returns_opencode_status(self):
        code, out, captured, connect = self.run_main(["run", "hello"])
        self.assertEqual(code, 7)
        connect.assert_any_call("/bin/ffvpn", "FI")
        self.assertEqual(captured["binary"], "/bin/opencode")
        self.assertEqual(captured["args"], ["run", "hello"])
        self.assertIn(self.directory, out)
        self.assertIn(f"fflingling: FI via ffvpn, log {self.directory}", out)
        relay = StubRelay.instances[0]
        self.assertTrue(relay.started)
        self.assertTrue(relay.stopped)
        # --no-mitm was not passed: the MITM is wired with a context.
        self.assertTrue(relay.kwargs["mitm"])
        self.assertEqual(relay.kwargs["server_context"], "context")

        env = captured["env"]
        proxy = "http://127.0.0.1:4321"
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            self.assertEqual(env[name], proxy)
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1")
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], "/data/ca.pem")

    def test_no_mitm_skips_the_ca_and_mitm(self):
        code, _, captured, _ = self.run_main(["--no-mitm", "run"], no_mitm=True)
        self.assertEqual(code, 7)
        relay = StubRelay.instances[0]
        self.assertFalse(relay.kwargs["mitm"])
        self.assertIsNone(relay.kwargs["server_context"])
        self.assertNotIn("NODE_EXTRA_CA_CERTS", captured["env"])

    def test_country_override_and_retry_environment(self):
        with mock.patch.dict(
            os.environ,
            {"FFLINGLING_COUNTRY": "DE", "FFLINGLING_RETRIES": "1",
             "FFLINGLING_ROTATE_WAIT_S": "0.5"},
            clear=False,
        ):
            code, out, _, connect = self.run_main(["run"])
        self.assertEqual(code, 7)
        connect.assert_any_call("/bin/ffvpn", "DE")
        self.assertIn("fflingling: DE via ffvpn", out)
        relay = StubRelay.instances[0]
        self.assertEqual(relay.kwargs["retries"], 1)
        self.assertEqual(relay.kwargs["rotate_wait"], 0.5)

    def test_help_documents_usage(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: fflingling", result.stdout)
        self.assertIn("--no-mitm", result.stdout)
        self.assertIn("opencode", result.stdout)


# --- AC9: one-line errors --------------------------------------------------


def write_executable(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o755)
    return path


class FfvpnCallTest(unittest.TestCase):
    def test_connect_uses_proxy_mode(self):
        with mock.patch.object(ffl.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ffl.connect_country("/bin/ffvpn", "FI")
        self.assertEqual(
            run.call_args.args[0],
            ["/bin/ffvpn", "connect", "--proxy", "FI"],
        )

    def test_codes_are_split_on_whitespace(self):
        with mock.patch.object(ffl.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=0, stdout="FI SE DE\n", stderr=""
            )
            self.assertEqual(ffl.list_countries("/bin/ffvpn"),
                             ["FI", "SE", "DE"])

    def test_recommended_marker_is_not_a_country(self):
        with mock.patch.object(ffl.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=0, stdout="REC AR AU AT\n", stderr=""
            )
            self.assertEqual(ffl.list_countries("/bin/ffvpn"),
                             ["AR", "AU", "AT"])

    def test_failure_uses_the_last_stderr_line(self):
        with mock.patch.object(ffl.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=1, stdout="", stderr="noise\nthe VPN is down\n"
            )
            with self.assertRaises(ffl.FflinglingError) as raised:
                ffl.connect_country("/bin/ffvpn", "FI")
        self.assertEqual(str(raised.exception), "the VPN is down")


class ErrorPathTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.data = os.path.join(self.directory, "data")
        self.bin = os.path.join(self.directory, "bin")
        os.makedirs(self.bin)

    def run_with(self, script, path):
        env = {
            "PATH": path,
            "FFLINGLING_DATA_DIR": self.data,
            "HOME": self.directory,
        }
        return subprocess.run(
            [sys.executable, script], capture_output=True, text=True,
            timeout=30, env=env,
        )

    def assert_one_line_error(self, result, text):
        self.assertEqual(result.returncode, 1, result.stdout)
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertTrue(lines[0].startswith("fflingling: "), result.stderr)
        self.assertIn(text, lines[0])
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_ffvpn(self):
        standalone = os.path.join(self.directory, "fflingling")
        shutil.copyfile(SCRIPT, standalone)
        os.chmod(standalone, 0o755)
        result = self.run_with(standalone, self.bin)
        self.assert_one_line_error(result, "ffvpn not found")

    def test_missing_openssl(self):
        write_executable(self.bin, "ffvpn", (
            "#!/bin/sh\n"
            "case \"$1\" in ls) echo 'FI SE';; esac\n"
            "exit 0\n"
        ))
        result = self.run_with(SCRIPT, self.bin)
        self.assert_one_line_error(result, "openssl not found")

    def test_vpn_down(self):
        write_executable(self.bin, "ffvpn", (
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  ls) echo 'FI SE';;\n"
            "  connect) echo 'the VPN is down' >&2; exit 1;;\n"
            "esac\n"
            "exit 0\n"
        ))
        result = self.run_with(SCRIPT, self.bin)
        self.assert_one_line_error(result, "the VPN is down")

    def test_empty_location_list(self):
        write_executable(self.bin, "ffvpn", (
            "#!/bin/sh\n"
            "case \"$1\" in ls) exit 0;; esac\n"
            "exit 0\n"
        ))
        result = self.run_with(SCRIPT, self.bin)
        self.assert_one_line_error(result, "no locations")


# --- folders ---------------------------------------------------------------


class FolderTest(unittest.TestCase):
    def test_fflingling_data_dir_respects_the_environment(self):
        with mock.patch.dict(
            os.environ, {"FFLINGLING_DATA_DIR": "/data/ff"}, clear=False
        ):
            self.assertEqual(ffl.data_dir(), "/data/ff")

    def test_fflingling_data_dir_falls_back_to_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}, clear=False):
            os.environ.pop("FFLINGLING_DATA_DIR", None)
            self.assertEqual(ffl.data_dir(), "/xdg/fflingling")

    def test_vpn_proxy_address_comes_from_the_ffvpn_folder(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        with open(os.path.join(directory, "config.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"proxy": {"listen": "0.0.0.0:10810"}}')
        with mock.patch.dict(
            os.environ, {"FFVPN_DATA_DIR": directory}, clear=False
        ):
            self.assertEqual(ffl.proxy_address(), ("127.0.0.1", 10810))

    def test_missing_vpn_config_is_one_message(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch.dict(
            os.environ, {"FFVPN_DATA_DIR": directory}, clear=False
        ):
            with self.assertRaises(ffl.FflinglingError) as raised:
                ffl.proxy_address()
        self.assertIn("Arman VPN app running", str(raised.exception))


# --- real listener ---------------------------------------------------------


class ListenerTest(unittest.TestCase):
    def test_relay_binds_loopback_and_stops(self):
        relay = make_relay()
        port = relay.start()
        try:
            self.assertGreater(port, 0)
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        finally:
            relay.stop()
        self.assertIsNone(relay._sock)


# --- one-line log ----------------------------------------------------------


class LogTest(unittest.TestCase):
    def test_log_line_names_the_rotated_country(self):
        connect = Scripted([response(429), response(200, b"ok")])
        relay = make_relay(connect=connect, rotate=lambda code: None)
        relay._serve_request(FakeSocket(request(b"{}")),
                             "api.opencode.ai", 443)
        text = log_text(relay.log.path)
        self.assertIn("429 -> rotate FI -> SE", text)
        self.assertIn("200 api.opencode.ai /v1/chat", text)


# --- partial reads across packets -----------------------------------------


class FragmentedTransportTest(unittest.TestCase):
    def test_request_body_split_across_packets_is_replayed_whole(self):
        body = b"0123456789abcdef"
        connect = Scripted(
            [response(429), response(200, b"ok")], make=FragmentedSocket
        )
        relay = make_relay(connect=connect, rotate=lambda code: None)
        relay._serve_request(FakeSocket(request(body)),
                             "api.opencode.ai", 443)

        self.assertEqual(len(connect.upstreams), 2)
        for upstream in connect.upstreams:
            sent = bytes(upstream.sent)
            self.assertEqual(sent.split(b"\r\n\r\n", 1)[1], body)

    def test_response_body_split_across_packets_reaches_the_client(self):
        payload = b"x" * 50
        connect = Scripted([response(200, payload)], make=FragmentedSocket)
        relay = make_relay(connect=connect)
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))
        self.assertTrue(bytes(client.sent).endswith(payload))

    def test_chunked_response_split_across_packets_is_streamed_whole(self):
        raw = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )
        connect = Scripted([raw], make=FragmentedSocket)
        relay = make_relay(connect=connect)
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertTrue(bytes(client.sent).endswith(
            b"5\r\nhello\r\n0\r\n\r\n"
        ))


# --- response framing ------------------------------------------------------


class ResponseFramingTest(unittest.TestCase):
    def test_a_429_whose_body_never_arrives_is_still_retried(self):
        head_only = (
            b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 999\r\n\r\n"
        )
        connect = Scripted([head_only, response(200, b"ok")])
        relay = make_relay(connect=connect, rotate=lambda code: None)
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertEqual(len(connect.upstreams), 2)
        self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))
        self.assertTrue(bytes(client.sent).endswith(b"ok"))

    def test_no_content_length_with_connection_close_streams_to_eof(self):
        raw = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nhello"
        connect = Scripted([raw])
        relay = make_relay(connect=connect)
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Connection: close", client.sent)
        self.assertTrue(bytes(client.sent).endswith(b"hello"))
        self.assertIn("5.0 B", log_text(relay.log.path))

    def test_an_empty_transfer_encoding_header_is_not_chunked(self):
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding:\r\n\r\nhi"
        connect = Scripted([raw])
        relay = make_relay(connect=connect)
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertTrue(client.sent.startswith(b"HTTP/1.1 200"))
        self.assertTrue(bytes(client.sent).endswith(b"hi"))


# --- request framing -------------------------------------------------------


class RequestFramingTest(unittest.TestCase):
    def test_a_zero_length_body_is_replayed_without_waiting(self):
        zero = (
            b"POST /v1/chat HTTP/1.1\r\n"
            b"Host: api.opencode.ai\r\nContent-Length: 0\r\n\r\n"
        )
        connect = Scripted([response(429), response(200, b"ok")])
        relay = make_relay(connect=connect, rotate=lambda code: None)
        client = FakeSocket(zero)
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertEqual(len(connect.upstreams), 2)
        first = bytes(connect.upstreams[0].sent)
        second = bytes(connect.upstreams[1].sent)
        self.assertEqual(first, second)
        self.assertEqual(first.split(b"\r\n\r\n", 1)[1], b"")
        self.assertTrue(bytes(client.sent).startswith(b"HTTP/1.1 200"))

    def test_an_empty_transfer_encoding_request_sends_no_body(self):
        empty_te = (
            b"POST /v1/chat HTTP/1.1\r\n"
            b"Host: api.opencode.ai\r\nTransfer-Encoding:\r\n\r\n"
        )
        connect = Scripted([response(200, b"ok")])
        relay = make_relay(connect=connect)
        client = FakeSocket(empty_te)
        relay._serve_request(client, "api.opencode.ai", 443)

        sent = bytes(connect.upstreams[0].sent)
        self.assertEqual(sent.split(b"\r\n\r\n", 1)[1], b"")
        self.assertTrue(bytes(client.sent).startswith(b"HTTP/1.1 200"))

    def test_a_client_closing_mid_body_returns_a_response_not_a_hang(self):
        truncated = (
            b"POST /v1/chat HTTP/1.1\r\n"
            b"Host: api.opencode.ai\r\nContent-Length: 10\r\n\r\n1234"
        )
        connect = Scripted([response(200, b"ok")])
        relay = make_relay(connect=connect)
        client = FakeSocket(truncated)

        # The fake returns EOF after 4 of the 10 promised bytes, like a client
        # that vanished. The call must return promptly and answer something.
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertTrue(bytes(client.sent).startswith(b"HTTP/1.1 "))


# --- retry boundaries ------------------------------------------------------


class RetryBoundaryTest(unittest.TestCase):
    def test_zero_retries_sends_the_first_response_straight_back(self):
        connect = Scripted([response(429), response(200, b"ok")])
        rotations = []
        relay = make_relay(
            connect=connect, rotate=rotations.append, retries=0
        )
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertEqual(rotations, [])
        self.assertEqual(len(connect.upstreams), 1)
        self.assertTrue(client.sent.startswith(b"HTTP/1.1 429"))

    def test_rotation_where_every_country_fails_keeps_the_last_response(self):
        connect = Scripted([response(429)])
        attempts = []

        def rotate(code):
            attempts.append(code)
            raise ffl.FflinglingError(f"{code} is down")

        relay = make_relay(
            connect=connect, rotate=rotate, pool=("FI", "SE", "DE")
        )
        client = FakeSocket(request(b"{}"))
        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertEqual(attempts, ["SE", "DE"])
        self.assertEqual(len(connect.upstreams), 1)
        self.assertEqual(relay.country, "FI")
        self.assertTrue(client.sent.startswith(b"HTTP/1.1 429"))
        text = log_text(relay.log.path)
        self.assertIn("rotation to SE failed", text)
        self.assertIn("rotation to DE failed", text)
        self.assertIn("429 -> rotate FI -> no other country", text)


# --- CONNECT targets -------------------------------------------------------


class ConnectTargetTest(unittest.TestCase):
    def test_connect_without_a_port_is_a_bad_request_and_no_connect(self):
        connect = Scripted([])
        relay = make_relay(connect=connect)
        client = FakeSocket(b"CONNECT opencode.ai HTTP/1.1\r\n\r\n")
        relay.handle(client)

        self.assertTrue(client.sent.startswith(b"HTTP/1.1 400"))
        self.assertEqual(connect.connects, [])

    def test_an_ipv6_literal_is_tunnelled_with_its_brackets_stripped(self):
        connect = Scripted([b"pong"])
        relay = make_relay(connect=connect)
        client = FakeSocket(
            b"CONNECT [2001:db8::1]:443 HTTP/1.1\r\n\r\nping"
        )
        relay.handle(client)

        self.assertEqual(connect.connects, [("2001:db8::1", 443, False)])
        self.assertTrue(
            client.sent.startswith(b"HTTP/1.1 200 Connection Established")
        )
        self.assertIn(b"pong", bytes(client.sent))
        self.assertEqual(bytes(connect.upstreams[0].sent), b"ping")


# --- MITM really terminates TLS (not just dispatch) ------------------------


def read_http_head(sock):
    data = bytearray()
    while not data.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        data += chunk
    return bytes(data)


def recv_all(sock):
    data = bytearray()
    while True:
        try:
            chunk = sock.recv(65536)
        except (ConnectionError, ssl.SSLError):
            # The relay closes without a TLS close_notify; the bytes already
            # delivered are the response.
            return bytes(data)
        if not chunk:
            return bytes(data)
        data += chunk


@unittest.skipIf(shutil.which("openssl") is None, "openssl is not installed")
class MitmHandshakeTest(unittest.TestCase):
    def test_a_real_tls_client_is_terminated_and_the_call_replayed(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        ca, leaf, key = ffl.ensure_certs(directory)
        connect = Scripted([response(429), response(200, b"ok")])
        relay = make_relay(
            connect=connect,
            rotate=lambda code: None,
            mitm=True,
            server_context=ffl.server_context(leaf, key),
            rotate_wait=2,
        )
        client_side, server_side = socket.socketpair()
        self.addCleanup(client_side.close)
        thread = threading.Thread(
            target=relay.handle, args=(server_side,), daemon=True
        )
        thread.start()
        received = b""
        try:
            client_side.settimeout(10)
            client_side.sendall(
                b"CONNECT api.opencode.ai:443 HTTP/1.1\r\n\r\n"
            )
            self.assertTrue(
                read_http_head(client_side).startswith(b"HTTP/1.1 200")
            )
            context = ssl.create_default_context(cafile=ca)
            with context.wrap_socket(
                client_side, server_hostname="api.opencode.ai"
            ) as tls:
                tls.settimeout(10)
                tls.sendall(request(b"{}"))
                received = recv_all(tls)
        finally:
            thread.join(10)

        self.assertFalse(thread.is_alive(), log_text(relay.log.path))
        self.assertEqual(
            connect.connects, [("api.opencode.ai", 443, True)] * 2
        )
        self.assertTrue(received.startswith(b"HTTP/1.1 200"))
        self.assertTrue(received.endswith(b"ok"))


# --- AC1 end to end with real subprocesses ---------------------------------


class EndToEndWiringTest(unittest.TestCase):
    def test_fake_ffvpn_and_opencode_receive_the_proxy_and_status(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        bin_dir = os.path.join(root, "bin")
        vpn_dir = os.path.join(root, "vpn")
        os.makedirs(bin_dir)
        os.makedirs(vpn_dir)
        with open(os.path.join(vpn_dir, "config.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"proxy": {"listen": "0.0.0.0:10810"}}')

        ffvpn_log = os.path.join(root, "ffvpn-calls.txt")
        opencode_log = os.path.join(root, "opencode-env.txt")
        write_executable(bin_dir, "ffvpn", (
            "#!/bin/sh\n"
            f'echo "$@" >> "{ffvpn_log}"\n'
            "case \"$1\" in\n"
            "  ls) echo 'FI SE';;\n"
            "esac\n"
            "exit 0\n"
        ))
        write_executable(bin_dir, "opencode", (
            "#!/bin/sh\n"
            f'echo "HTTPS_PROXY=$HTTPS_PROXY" > "{opencode_log}"\n'
            f'echo "NO_PROXY=$NO_PROXY" >> "{opencode_log}"\n'
            "echo \"CA=${NODE_EXTRA_CA_CERTS-<unset>}\" >> "
            f'"{opencode_log}"\n'
            f'echo "ARGS=$*" >> "{opencode_log}"\n'
            "exit 7\n"
        ))

        env = {
            "PATH": bin_dir,
            "HOME": root,
            "FFLINGLING_DATA_DIR": os.path.join(root, "data"),
            "FFVPN_DATA_DIR": vpn_dir,
        }
        result = subprocess.run(
            [sys.executable, SCRIPT, "--no-mitm", "run", "hello"],
            capture_output=True, text=True, timeout=30, env=env,
        )

        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("fflingling: FI via ffvpn", result.stdout)
        with open(ffvpn_log, encoding="utf-8") as handle:
            calls = handle.read().splitlines()
        self.assertIn("ls --codes", calls)
        self.assertIn("connect --proxy FI", calls)
        with open(opencode_log, encoding="utf-8") as handle:
            seen = dict(
                line.split("=", 1) for line in handle.read().splitlines()
            )
        self.assertRegex(seen["HTTPS_PROXY"], r"^http://127\.0\.0\.1:\d+$")
        self.assertEqual(seen["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(seen["CA"], "<unset>")
        self.assertEqual(seen["ARGS"], "run hello")


# --- AC log line shape -----------------------------------------------------


class LogFormatTest(unittest.TestCase):
    def test_the_two_log_lines_match_the_documented_shape(self):
        connect = Scripted([response(429), response(200, b"x" * 30310)])
        relay = make_relay(connect=connect, rotate=lambda code: None)
        relay._serve_request(FakeSocket(request(b"{}")),
                             "api.opencode.ai", 443)
        text = log_text(relay.log.path)

        self.assertRegex(
            text,
            r"(?m)^\d{2}:\d{2}:\d{2} #\d+ \{FI\} 429 -> rotate FI -> SE$",
        )
        self.assertRegex(
            text,
            r"(?m)^\d{2}:\d{2}:\d{2} #\d+ \{SE\} 200 api\.opencode\.ai "
            r"/v1/chat 29\.6 KB \d+\.\ds$",
        )

    def test_human_bytes_units(self):
        self.assertEqual(ffl.human_bytes(0), "0.0 B")
        self.assertEqual(ffl.human_bytes(1023), "1023.0 B")
        self.assertEqual(ffl.human_bytes(1024), "1.0 KB")
        self.assertEqual(ffl.human_bytes(1024 * 1024), "1.0 MB")


# --- final review findings -------------------------------------------------


class InterimResponseTest(unittest.TestCase):
    def test_a_100_before_the_429_still_rotates(self):
        connect = Scripted([
            b"HTTP/1.1 100 Continue\r\n\r\n" + response(429),
            response(200, b"ok"),
        ])
        rotations = []
        relay = make_relay(connect=connect, rotate=rotations.append)
        client = FakeSocket(request(b"{}"))

        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertEqual(rotations, ["SE"])
        self.assertTrue(bytes(client.sent).startswith(b"HTTP/1.1 200"))

    def test_a_103_before_the_answer_is_not_delivered(self):
        connect = Scripted([
            b"HTTP/1.1 103 Early Hints\r\nLink: </x>\r\n\r\n"
            + response(200, b"ok"),
        ])
        relay = make_relay(connect=connect)
        client = FakeSocket(request(b"{}"))

        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertTrue(bytes(client.sent).startswith(b"HTTP/1.1 200"))
        self.assertNotIn(b"103", bytes(client.sent))


class AcceptLoopTest(unittest.TestCase):
    def test_one_aborted_accept_does_not_kill_the_proxy(self):
        relay = make_relay()
        served = threading.Event()

        class FlakyListener:
            calls = 0

            def accept(self):
                FlakyListener.calls += 1
                if FlakyListener.calls == 1:
                    raise ConnectionAbortedError()
                relay._sock = None
                return FakeSocket(), ("127.0.0.1", 1)

        relay._sock = FlakyListener()
        relay.handle = lambda client: served.set()

        relay._accept()

        self.assertTrue(served.wait(2))
        self.assertEqual(FlakyListener.calls, 2)


class BodyLimitTest(unittest.TestCase):
    def test_an_oversized_body_is_answered_413(self):
        relay = make_relay()
        client = FakeSocket(request(b"{", headers=(
            b"Host: api.opencode.ai\r\nContent-Length: 70000000\r\n"
        )))

        relay._serve_request(client, "api.opencode.ai", 443)

        self.assertTrue(bytes(client.sent).startswith(b"HTTP/1.1 413"))


class HeaderRewriteTest(unittest.TestCase):
    def test_chunked_drops_a_conflicting_content_length(self):
        head = (
            b"POST /v1/chat HTTP/1.1\r\nHost: api.opencode.ai\r\n"
            b"Transfer-Encoding: chunked\r\nContent-Length: 12\r\n\r\n"
        )

        out = ffl.force_close(head)

        self.assertIn(b"Transfer-Encoding: chunked", out)
        self.assertNotIn(b"Content-Length", out)
        self.assertTrue(out.endswith(b"Connection: close\r\n\r\n"))


class RotationRefusalTest(unittest.TestCase):
    def test_a_proxy_that_did_not_move_is_a_failed_rotation(self):
        with mock.patch.object(
            ffl, "_run_ffvpn",
            return_value=("", "ffvpn: warning: the proxy is still using FI\n"),
        ):
            with self.assertRaises(ffl.FflinglingError):
                ffl.connect_country("ffvpn", "SE")


class ProxyAddressTest(unittest.TestCase):
    def test_a_bracketed_listen_host_is_unwrapped(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        with open(os.path.join(directory, "config.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"proxy": {"listen": "[::1]:10810"}}')
        with mock.patch.dict(os.environ, {"FFVPN_DATA_DIR": directory}):
            self.assertEqual(ffl.proxy_address(), ("::1", 10810))


class ExitCodeTest(unittest.TestCase):
    def test_a_signal_is_reported_as_128_plus_the_signal(self):
        with mock.patch.object(
            ffl.subprocess, "run", return_value=mock.Mock(returncode=-9)
        ):
            self.assertEqual(ffl.run_opencode("opencode", [], {}), 137)
        with mock.patch.object(
            ffl.subprocess, "run", return_value=mock.Mock(returncode=7)
        ):
            self.assertEqual(ffl.run_opencode("opencode", [], {}), 7)


def _pipe_once(source, target, stop):
    try:
        while not stop.is_set():
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class OriginVerificationTest(unittest.TestCase):
    """The real `_vpn_connect` must verify the origin certificate."""

    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is not installed")
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.ca, leaf, key = ffl.ensure_certs(directory)
        context = ffl.server_context(leaf, key)
        self.origin = socket.socket()
        self.origin.bind(("127.0.0.1", 0))
        self.origin.listen(4)
        self.origin_port = self.origin.getsockname()[1]
        self.addCleanup(self.origin.close)
        stop = threading.Event()
        threading.Thread(
            target=self._origin, args=(context, stop), daemon=True
        ).start()
        self.addCleanup(stop.set)

        # A proxy that answers CONNECT and then just relays bytes.
        self.proxy = socket.socket()
        self.proxy.bind(("127.0.0.1", 0))
        self.proxy.listen(4)
        self.proxy_address = self.proxy.getsockname()[1]
        self.addCleanup(self.proxy.close)
        threading.Thread(target=self._proxy, daemon=True).start()

    def _origin(self, context, stop):
        while not stop.is_set():
            try:
                conn, _ = self.origin.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve_origin, args=(conn, context), daemon=True
            ).start()

    def _serve_origin(self, conn, context):
        try:
            tls = context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            conn.close()
            return
        try:
            tls.recv(65536)
        except OSError:
            pass
        finally:
            tls.close()

    def _proxy(self):
        while True:
            try:
                conn, _ = self.proxy.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn):
        try:
            ffl.read_head(conn, 5)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            upstream = socket.create_connection(
                ("127.0.0.1", self.origin_port), timeout=5
            )
        except OSError:
            conn.close()
            return
        stop = threading.Event()
        threading.Thread(
            target=_pipe_once, args=(upstream, conn, stop), daemon=True
        ).start()
        _pipe_once(conn, upstream, stop)
        stop.set()
        for sock in (upstream, conn):
            try:
                sock.close()
            except OSError:
                pass

    def _relay_for(self):
        return ffl.Relay(
            ("127.0.0.1", self.proxy_address),
            ["FI"],
            log=ffl.Log(os.devnull),
            connect_proxy=lambda: socket.create_connection(
                ("127.0.0.1", self.proxy_address), timeout=5
            ),
        )

    def test_a_matching_origin_is_accepted(self):
        context = ssl.create_default_context(cafile=self.ca)
        with mock.patch.object(
            ffl.ssl, "create_default_context", return_value=context
        ):
            tls = self._relay_for()._vpn_connect("api.opencode.ai", 443, True)
        self.assertTrue(tls.cipher())
        tls.close()

    def test_a_mismatched_origin_is_rejected(self):
        context = ssl.create_default_context(cafile=self.ca)
        with mock.patch.object(
            ffl.ssl, "create_default_context", return_value=context
        ):
            with self.assertRaises(ssl.SSLCertVerificationError):
                self._relay_for()._vpn_connect("a.b.opencode.ai", 443, True)


if __name__ == "__main__":
    unittest.main()
