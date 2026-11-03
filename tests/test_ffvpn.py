import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "ffvpn")


def load_cli():
    loader = importlib.machinery.SourceFileLoader("ffvpn_cli", SCRIPT)
    spec = importlib.util.spec_from_file_location("ffvpn_cli", SCRIPT, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli()


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextlib.contextmanager
def serve(status, body, content_type="application/json", record=None):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            if record is not None:
                record.append({"headers": dict(self.headers), "body": payload})
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = _respond
        do_POST = _respond
        do_DELETE = _respond

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


class HumanBytesTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(cli.human_bytes(0), "0.0 B")
        self.assertEqual(cli.human_bytes(1023), "1023.0 B")
        self.assertEqual(cli.human_bytes(1024), "1.0 KB")
        self.assertEqual(cli.human_bytes(1536 * 1024 ** 2), "1.5 GB")
        self.assertEqual(cli.human_bytes(2.5 * 1024 ** 4), "2.5 TB")

    def test_missing_value(self):
        self.assertEqual(cli.human_bytes(None), "-")


class DashboardAddressTest(unittest.TestCase):
    def config_dir(self, config):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        if config is not None:
            with open(os.path.join(directory, "config.json"), "w", encoding="utf-8") as f:
                json.dump(config, f)
        return directory

    def test_normal(self):
        directory = self.config_dir({"dashboard": {"listen": "127.0.0.1:10820"}})
        self.assertEqual(cli.dashboard_address(directory), "127.0.0.1:10820")

    def test_wildcard(self):
        directory = self.config_dir({"dashboard": {"listen": "0.0.0.0:10820"}})
        self.assertEqual(cli.dashboard_address(directory), "127.0.0.1:10820")

    def test_empty_host(self):
        directory = self.config_dir({"dashboard": {"listen": ":10820"}})
        self.assertEqual(cli.dashboard_address(directory), "127.0.0.1:10820")

    def test_missing_file(self):
        directory = self.config_dir(None)
        self.assertEqual(cli.dashboard_address(directory), cli.DEFAULT_ADDRESS)

    def test_numeric_listen_falls_back(self):
        directory = self.config_dir({"dashboard": {"listen": 10820}})
        self.assertEqual(cli.dashboard_address(directory), cli.DEFAULT_ADDRESS)

    def test_ipv6_listen_keeps_one_pair_of_brackets(self):
        for listen in ("::1:10820", "[::1]:10820"):
            self.assertEqual(cli.parse_listen(listen), "[::1]:10820", listen)


class WarnPlainHttpTest(unittest.TestCase):
    def setUp(self):
        cli.warned_plain_http = False
        self.addCleanup(setattr, cli, "warned_plain_http", False)

    def test_warns_once_for_non_loopback(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli.warn_plain_http("192.0.2.10:10820")
            cli.warn_plain_http("192.0.2.11:10820")
        lines = err.getvalue().splitlines()
        self.assertEqual(
            lines,
            [
                "ffvpn: warning: talking to 192.0.2.10:10820 over plain HTTP; "
                "the local API token is sent unencrypted"
            ],
        )

    def test_loopback_never_warns(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for address in ("127.0.0.1:10820", "localhost:10820", "[::1]:10820"):
                cli.warn_plain_http(address)
        self.assertEqual(err.getvalue(), "")


class DataDirTest(unittest.TestCase):
    def make_dir(self, token=True):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        if token:
            with open(os.path.join(directory, "local-api.token"), "w", encoding="utf-8") as f:
                f.write("secret\n")
        return directory

    def test_explicit_wins(self):
        explicit = self.make_dir()
        xdg = self.make_dir()
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": ""}, clear=False
        ):
            self.assertEqual(cli.find_data_dir(explicit), explicit)

    def test_explicit_missing_token_does_not_fall_back(self):
        explicit = self.make_dir(token=False)
        xdg = self.make_dir()
        default = os.path.join(xdg, "firefox-vpn-client")
        os.makedirs(default)
        with open(os.path.join(default, "local-api.token"), "w", encoding="utf-8") as f:
            f.write("secret\n")
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": ""}, clear=False
        ):
            self.assertEqual(cli.find_data_dir(explicit), explicit)
            with self.assertRaises(cli.FfvpnError) as raised:
                cli.make_api(mock.Mock(data_dir=explicit))
        self.assertIn(explicit, str(raised.exception))

    def test_environment_missing_token_does_not_fall_back(self):
        env_dir = self.make_dir(token=False)
        xdg = self.make_dir()
        default = os.path.join(xdg, "firefox-vpn-client")
        os.makedirs(default)
        with open(os.path.join(default, "local-api.token"), "w", encoding="utf-8") as f:
            f.write("secret\n")
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": env_dir}, clear=False
        ):
            self.assertEqual(cli.find_data_dir(None), env_dir)
            with self.assertRaises(cli.FfvpnError) as raised:
                cli.make_api(mock.Mock(data_dir=None))
        self.assertIn(env_dir, str(raised.exception))

    def test_token_detection_in_default_location(self):
        xdg = self.make_dir()
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": ""}, clear=False
        ):
            expected = os.path.join(xdg, "firefox-vpn-client")
            os.makedirs(expected)
            with open(os.path.join(expected, "local-api.token"), "w", encoding="utf-8") as f:
                f.write("secret\n")
            self.assertEqual(cli.find_data_dir(None), expected)

    def test_environment_variable(self):
        env_dir = self.make_dir()
        xdg = self.make_dir()
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": env_dir}, clear=False
        ):
            self.assertEqual(cli.find_data_dir(None), env_dir)

    def test_fallback_is_default_folder(self):
        xdg = self.make_dir(token=False)
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": ""}, clear=False
        ):
            self.assertEqual(
                cli.find_data_dir(None), os.path.join(xdg, "firefox-vpn-client")
            )


LOCATIONS = [
    {"code": "REC", "name": "Recommended", "city": "Recommended", "recommended": True},
    {"code": "FI", "name": "Finland", "city": "Helsinki"},
    {"code": "DE", "name": "Germany", "city": "Berlin"},
    {"code": "US", "name": "United States", "city": "New York"},
]


class ResolveLocationTest(unittest.TestCase):
    def test_default_is_fastest(self):
        self.assertEqual(cli.resolve_location(None, LOCATIONS), "fastest")
        self.assertEqual(cli.resolve_location("fastest", LOCATIONS), "fastest")

    def test_codes_any_case(self):
        self.assertEqual(cli.resolve_location("FI", LOCATIONS), "FI")
        self.assertEqual(cli.resolve_location("fi", LOCATIONS), "FI")

    def test_recommended_aliases(self):
        for value in ("rec", "REC", "recommended", "auto"):
            self.assertEqual(cli.resolve_location(value, LOCATIONS), "", value)

    def test_unique_name_substring(self):
        self.assertEqual(cli.resolve_location("fin", LOCATIONS), "FI")
        self.assertEqual(cli.resolve_location("new york", LOCATIONS), "US")

    def test_ambiguous_substring(self):
        with self.assertRaises(cli.FfvpnError) as raised:
            cli.resolve_location("e", LOCATIONS)
        self.assertIn("ambiguous", str(raised.exception))

    def test_unknown(self):
        with self.assertRaises(cli.FfvpnError):
            cli.resolve_location("zz", LOCATIONS)
        with self.assertRaises(cli.FfvpnError):
            cli.resolve_location("Atlantis", LOCATIONS)


ACCOUNTS = [
    {"id": "a1", "name": "Alice", "email": "alice@example.com"},
    {"id": "a2", "name": "Bob", "email": "bob@example.com"},
    {"id": "a3", "name": "Alice Work", "email": "alice.work@example.com"},
]


class ResolveAccountTest(unittest.TestCase):
    def test_id(self):
        self.assertEqual(cli.resolve_account("a2", ACCOUNTS)["id"], "a2")

    def test_exact_email(self):
        self.assertEqual(cli.resolve_account("BOB@example.com", ACCOUNTS)["id"], "a2")

    def test_unique_substring(self):
        self.assertEqual(cli.resolve_account("bob", ACCOUNTS)["id"], "a2")

    def test_ambiguous(self):
        with self.assertRaises(cli.FfvpnError) as raised:
            cli.resolve_account("alice", ACCOUNTS)
        self.assertIn("ambiguous", str(raised.exception))

    def test_unknown(self):
        with self.assertRaises(cli.FfvpnError):
            cli.resolve_account("carol", ACCOUNTS)


class ApiTest(unittest.TestCase):
    def test_api_error_message_surfaces(self):
        with serve(400, '{"error":"boom"}') as port:
            api = cli.Api(f"http://127.0.0.1:{port}", "tok")
            with self.assertRaises(cli.FfvpnError) as raised:
                api.get("/api/status")
        self.assertIn("boom", str(raised.exception))

    def test_non_json_error_body_does_not_crash(self):
        with serve(415, "<html>not json</html>", "text/html") as port:
            api = cli.Api(f"http://127.0.0.1:{port}", "tok")
            with self.assertRaises(cli.FfvpnError) as raised:
                api.post("/api/status", {"on": False})
        self.assertIn("415", str(raised.exception))

    def test_post_sends_token_and_json_content_type(self):
        record = []
        with serve(200, '{"ok":true}', record=record) as port:
            api = cli.Api(f"http://127.0.0.1:{port}", "tok")
            api.post("/api/status", {"on": False})
        headers = {k.lower(): v for k, v in record[0]["headers"].items()}
        self.assertEqual(headers["x-local-token"], "tok")
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(json.loads(record[0]["body"]), {"on": False})

    def test_connection_refused_names_url(self):
        port = free_port()
        api = cli.Api(f"http://127.0.0.1:{port}", "tok", timeout=1)
        with self.assertRaises(cli.FfvpnError) as raised:
            api.get("/api/status")
        message = str(raised.exception)
        self.assertIn(f"127.0.0.1:{port}", message)
        self.assertIn("running", message)


class LoginFlowTest(unittest.TestCase):
    def test_verification_code_then_done(self):
        class FakeApi:
            def __init__(self):
                self.calls = []

            def post(self, path, body=None, timeout=None):
                self.calls.append((path, body))
                if path == "/api/signin":
                    return {
                        "id": "s1",
                        "step": "need_email_code",
                        "message": "Check your email",
                        "email": "a@b",
                    }
                return {"id": "s1", "step": "done", "message": "", "email": "a@b"}

            def get(self, path):
                raise AssertionError("should not poll")

        api = FakeApi()
        args = mock.Mock(email="a@b")
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(cli, "ask", return_value="123456"), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_account_login(args), 0)
        self.assertEqual(
            api.calls[0], ("/api/signin", {"email": "a@b", "password": "secret"})
        )
        self.assertEqual(api.calls[1], ("/api/signin/s1", {"value": "123456"}))


class CompletionTest(unittest.TestCase):
    def test_bash_anchors_and_syntax(self):
        script = cli.completion_script("bash")
        self.assertIn("_ffvpn", script)
        self.assertIn("complete -F _ffvpn ffvpn", script)
        if shutil.which("bash"):
            result = subprocess.run(
                ["bash", "-n"], input=script, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_fish_anchors_and_syntax(self):
        script = cli.completion_script("fish")
        self.assertIn("complete -c ffvpn", script)
        if shutil.which("fish"):
            result = subprocess.run(
                ["fish", "-n"], input=script, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
