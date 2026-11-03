"""Acceptance-gap tests: command behaviour, payloads, ordering and exit codes.

The existing tests cover parsing helpers, the HTTP error path and one login
step. These drive the command functions with fakes so no app or network is
needed. All state comes from the test, so every assertion is deterministic.
"""

import base64
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from test_ffvpn import cli, LOCATIONS

RUNNING_STATUS = {
    "state": {"phase": "running", "message": "Connected"},
    "selection_name": "Finland",
    "proxy": {"listen": "127.0.0.1:10821", "country_code": "FI"},
}


# --- fakes ------------------------------------------------------------------


class ConnectApi:
    def __init__(self, statuses=None, locations=None):
        self.locations = LOCATIONS if locations is None else locations
        self.statuses = list(statuses or [RUNNING_STATUS])
        self.calls = []

    def get(self, path):
        self.calls.append(("get", path))
        if path == "/api/locations":
            return {"locations": self.locations}
        if path == "/api/status":
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body=None, timeout=None):
        self.calls.append(("post", path, body))
        return {}

    def posted(self):
        return [call for call in self.calls if call[0] == "post"]


class LsApi:
    def __init__(self, locations, selection="", country_code=None):
        self.locations = locations
        self.selection = selection
        self.country_code = country_code

    def get(self, path):
        if path == "/api/locations":
            return {"locations": self.locations}
        if path == "/api/status":
            status = {"selection": self.selection}
            if self.country_code is not None:
                status["proxy"] = {"country_code": self.country_code}
            return status
        raise AssertionError(f"unexpected GET {path}")


class AccountsApi:
    def __init__(self, accounts):
        self.accounts = accounts
        self.calls = []

    def get(self, path):
        self.calls.append(("get", path))
        if path == "/api/accounts":
            return {"accounts": self.accounts}
        if path == "/api/status":
            return {"state": {"message": "off", "phase": "off"}, "selection": ""}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body=None, timeout=None):
        self.calls.append(("post", path, body))
        return {}

    def delete(self, path):
        self.calls.append(("delete", path))
        return {}

    def posted(self):
        return [call for call in self.calls if call[0] == "post"]

    def deleted(self):
        return [call for call in self.calls if call[0] == "delete"]


def run_connect(api, **overrides):
    options = {
        "location": None,
        "proxy": False,
        "system_proxy": False,
        "tun": False,
        "data_dir": None,
    }
    options.update(overrides)
    args = mock.Mock(**options)
    with mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(
        io.StringIO()
    ):
        return cli.cmd_connect(args)


def run_ls(api, codes=False):
    args = mock.Mock(codes=codes, data_dir=None)
    out = io.StringIO()
    with mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(out):
        cli.cmd_ls(args)
    return out.getvalue()


def run_account(api, command, **overrides):
    options = {"account": None, "yes": False, "names": False, "data_dir": None}
    options.update(overrides)
    args = mock.Mock(**options)
    out = io.StringIO()
    with mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(out):
        result = command(args)
    return result, out.getvalue()


# --- AC1 / AC4: connect order, payloads, mode flags -------------------------


class ConnectTest(unittest.TestCase):
    def test_posts_location_then_power_and_leaves_mode_alone(self):
        api = ConnectApi()
        self.assertEqual(run_connect(api, location="FI"), 0)
        self.assertEqual(
            api.calls,
            [
                ("get", "/api/locations"),
                ("post", "/api/location", {"country": "FI"}),
                ("post", "/api/power", {"on": True}),
                ("get", "/api/status"),
            ],
        )

    def test_without_location_selects_fastest(self):
        api = ConnectApi()
        run_connect(api)
        self.assertEqual(api.posted(), [("post", "/api/location", {"country": "fastest"}),
                                        ("post", "/api/power", {"on": True})])

    def test_unique_name_substring_is_sent_as_code(self):
        api = ConnectApi()
        run_connect(api, location="fin")
        self.assertEqual(api.posted()[0], ("post", "/api/location", {"country": "FI"}))

    def test_recommended_sends_empty_country(self):
        for value in ("rec", "recommended", "REC"):
            api = ConnectApi()
            run_connect(api, location=value)
            self.assertEqual(api.posted()[0], ("post", "/api/location", {"country": ""}), value)

    def test_system_proxy_mode_comes_before_location(self):
        api = ConnectApi()
        run_connect(api, location="fi", system_proxy=True)
        self.assertEqual(
            api.posted(),
            [
                ("post", "/api/system-proxy", {"mode": "set"}),
                ("post", "/api/location", {"country": "FI"}),
                ("post", "/api/power", {"on": True}),
            ],
        )

    def test_proxy_mode_is_clear(self):
        api = ConnectApi()
        run_connect(api, location="FI", proxy=True)
        self.assertEqual(api.posted()[0], ("post", "/api/system-proxy", {"mode": "clear"}))

    def test_tun_mode_when_supported(self):
        api = ConnectApi(statuses=[{"tun_supported": True}, RUNNING_STATUS])
        self.assertEqual(run_connect(api, location="FI", tun=True), 0)
        self.assertEqual(
            api.calls,
            [
                ("get", "/api/locations"),
                ("get", "/api/status"),
                ("post", "/api/system-proxy", {"mode": "tun"}),
                ("post", "/api/location", {"country": "FI"}),
                ("post", "/api/power", {"on": True}),
                ("get", "/api/status"),
            ],
        )

    def test_tun_unsupported_fails_without_any_post(self):
        api = ConnectApi(statuses=[{"tun_supported": False}])
        with self.assertRaises(cli.FfvpnError) as raised:
            run_connect(api, location="FI", tun=True)
        self.assertIn("TUN", str(raised.exception))
        self.assertEqual(api.posted(), [])

    def test_unknown_location_does_not_change_anything(self):
        api = ConnectApi()
        with self.assertRaises(cli.FfvpnError):
            run_connect(api, location="Atlantis")
        self.assertEqual(api.posted(), [])

    def test_non_running_state_exits_nonzero(self):
        api = ConnectApi(statuses=[{"state": {"phase": "error", "message": "No route"}}])
        with self.assertRaises(cli.FfvpnError) as raised:
            run_connect(api)
        self.assertIn("No route", str(raised.exception))

    def test_starting_then_running_prints_final_state(self):
        api = ConnectApi(
            statuses=[
                {"state": {"phase": "starting", "message": "Connecting"}},
                RUNNING_STATUS,
            ]
        )
        args = mock.Mock(
            location=None, proxy=False, system_proxy=False, tun=False, data_dir=None
        )
        out = io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_connect(args), 0)
        self.assertIn("Connected", out.getvalue())
        self.assertEqual(
            [call for call in api.calls if call == ("get", "/api/status")],
            [("get", "/api/status"), ("get", "/api/status")],
        )

    def test_starting_until_deadline_exits_nonzero(self):
        api = ConnectApi(statuses=[{"state": {"phase": "starting", "message": "Starting"}}])
        err = io.StringIO()
        with mock.patch.object(cli, "POWER_TIMEOUT", 0.05), mock.patch.object(
            cli, "POLL_INTERVAL", 0.01
        ), mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["connect"]), 1)
        self.assertIn("Starting", err.getvalue())
        self.assertGreater(
            len([call for call in api.calls if call == ("get", "/api/status")]), 1
        )

    def test_connect_waits_for_engine_country(self):
        # Right after a location change the old engine still reports running/DE.
        old_engine = {
            "state": {"phase": "running", "message": "Connected"},
            "selection_name": "Finland",
            "proxy": {"listen": "127.0.0.1:10821", "country_code": "DE"},
            "exit": {"ipv4": "198.51.100.7", "country_name": "Germany"},
        }
        new_engine = {
            "state": {"phase": "running", "message": "Connected"},
            "selection_name": "Finland",
            "proxy": {"listen": "127.0.0.1:10821", "country_code": "FI"},
            "exit": {"ipv4": "203.0.113.9", "country_name": "Finland"},
        }
        api = ConnectApi(statuses=[old_engine, new_engine])
        err = io.StringIO()
        with mock.patch.object(cli, "POWER_TIMEOUT", 1), mock.patch.object(
            cli, "POLL_INTERVAL", 0.01
        ), mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(
            io.StringIO()
        ) as out, contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["connect", "FI"]), 0)
        self.assertIn("exit: 203.0.113.9", out.getvalue())
        self.assertNotIn("198.51.100.7", out.getvalue())
        self.assertNotIn("warning", err.getvalue())

    def test_connect_warns_when_engine_country_never_changes(self):
        api = ConnectApi(
            statuses=[
                {
                    "state": {"phase": "running", "message": "Connected"},
                    "proxy": {"country_code": "DE"},
                    "exit": {"ipv4": "198.51.100.7", "country_name": "Germany"},
                }
            ]
        )
        err = io.StringIO()
        with mock.patch.object(cli, "POWER_TIMEOUT", 0.05), mock.patch.object(
            cli, "POLL_INTERVAL", 0.01
        ), mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(
            io.StringIO()
        ) as out, contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["connect", "FI"]), 0)
        self.assertIn("exit: 198.51.100.7", out.getvalue())
        self.assertIn("warning: the proxy is still using DE", err.getvalue())
        self.assertNotIn("warning", out.getvalue())

    def test_connect_warns_unknown_when_proxy_has_no_country(self):
        api = ConnectApi(
            statuses=[
                {
                    "state": {"phase": "running", "message": "Connected"},
                    "proxy": {},
                    "exit": {"ipv4": "203.0.113.9"},
                }
            ]
        )
        err = io.StringIO()
        with mock.patch.object(cli, "POWER_TIMEOUT", 0.05), mock.patch.object(
            cli, "POLL_INTERVAL", 0.01
        ), mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["connect", "FI"]), 0)
        self.assertIn("warning: the proxy is still using unknown", err.getvalue())

    def test_mode_flags_are_mutually_exclusive(self):
        pairs = [
            ("--proxy", "--system-proxy"),
            ("--proxy", "--tun"),
            ("--system-proxy", "--tun"),
        ]
        for pair in pairs:
            with self.subTest(pair=pair), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.build_parser().parse_args(["connect", *pair])
                self.assertEqual(raised.exception.code, 2)


# --- AC2: disconnect --------------------------------------------------------


class DisconnectTest(unittest.TestCase):
    def test_posts_power_off(self):
        api = AccountsApi([])
        args = mock.Mock(data_dir=None)
        with mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(cli.cmd_disconnect(args), 0)
        self.assertEqual(api.posted(), [("post", "/api/power", {"on": False})])

    def test_running_then_off_prints_off_message(self):
        api = ConnectApi(
            statuses=[
                {"state": {"phase": "running", "message": "Proxy running"}},
                {"state": {"phase": "off", "message": "Disconnected"}},
            ]
        )
        args = mock.Mock(data_dir=None)
        out = io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=api), contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_disconnect(args), 0)
        self.assertIn("Disconnected", out.getvalue())
        self.assertNotIn("Proxy running", out.getvalue())
        self.assertEqual(
            [call for call in api.calls if call == ("get", "/api/status")],
            [("get", "/api/status"), ("get", "/api/status")],
        )


    def test_disconnect_requires_off_phase(self):
        error_api = ConnectApi(
            statuses=[{"state": {"phase": "error", "message": "Engine failed"}}]
        )
        err = io.StringIO()
        with mock.patch.object(cli, "POWER_TIMEOUT", 1), mock.patch.object(
            cli, "POLL_INTERVAL", 0.01
        ), mock.patch.object(
            cli, "make_api", return_value=error_api
        ), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["disconnect"]), 1)
        self.assertIn("Engine failed", err.getvalue())

        success_api = ConnectApi(
            statuses=[
                {"state": {"phase": "running", "message": "Proxy running"}},
                {"state": {"phase": "off", "message": "Disconnected"}},
            ]
        )
        out = io.StringIO()
        with mock.patch.object(cli, "POWER_TIMEOUT", 1), mock.patch.object(
            cli, "POLL_INTERVAL", 0.01
        ), mock.patch.object(
            cli, "make_api", return_value=success_api
        ), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["disconnect"]), 0)
        self.assertIn("Disconnected", out.getvalue())


    def test_disconnect_never_reaching_off_exits_one(self):
        api = ConnectApi(
            statuses=[{"state": {"phase": "running", "message": "Proxy running"}}]
        )
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "POWER_TIMEOUT", 0.05), mock.patch.object(
            cli, "POLL_INTERVAL", 0.01
        ), mock.patch.object(
            cli, "make_api", return_value=api
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["disconnect"]), 1)
        self.assertIn("Proxy running", err.getvalue())
        self.assertNotIn("Disconnected", out.getvalue())
        self.assertGreater(
            len([call for call in api.calls if call == ("get", "/api/status")]), 1
        )


# --- AC3: ls ----------------------------------------------------------------


class LsTest(unittest.TestCase):
    def test_codes_prints_only_codes(self):
        api = LsApi(LOCATIONS, selection="FI")
        self.assertEqual(run_ls(api, codes=True).split(), ["REC", "FI", "DE", "US"])

    def test_selected_row_is_marked(self):
        out = run_ls(LsApi(LOCATIONS, selection="FI"))
        marked = [line for line in out.splitlines() if line.startswith(">")]
        self.assertEqual(len(marked), 1, out)
        self.assertIn("FI", marked[0])

    def test_recommended_marked_when_nothing_selected(self):
        out = run_ls(LsApi(LOCATIONS, selection=""))
        marked = [line for line in out.splitlines() if line.startswith(">")]
        self.assertEqual(len(marked), 1, out)
        self.assertIn("REC", marked[0])

    def test_fastest_marks_running_proxy_country(self):
        out = run_ls(LsApi(LOCATIONS, selection="fastest", country_code="FI"))
        marked = [line for line in out.splitlines() if line.startswith(">")]
        self.assertEqual(len(marked), 1, out)
        self.assertIn(" FI ", marked[0])

    def test_ping_column_only_when_data_present(self):
        plain = run_ls(LsApi(LOCATIONS, selection="FI"))
        self.assertNotIn("PING", plain)

        with_ping = [dict(location) for location in LOCATIONS]
        with_ping[1]["ping_ms"] = 24
        out = run_ls(LsApi(with_ping, selection="FI"))
        self.assertIn("PING", out)
        self.assertIn("24 ms", out)
        us_lines = [line for line in out.splitlines() if " US " in line]
        self.assertEqual(len(us_lines), 1, out)
        self.assertTrue(us_lines[0].rstrip().endswith("-"), us_lines[0])


# --- AC5: account usage / set / logout --------------------------------------


class AccountLsTest(unittest.TestCase):
    def test_lists_accounts_with_state_flags(self):
        api = AccountsApi(
            [
                {
                    "id": "a1",
                    "email": "a@b",
                    "position": 0,
                    "status": "active",
                    "active": True,
                    "enabled": True,
                },
                {"id": "a2", "email": "c@d", "active": False, "enabled": False},
            ]
        )
        result, out = run_account(api, cli.cmd_account_ls)
        self.assertEqual(result, 0)
        first = next(line for line in out.splitlines() if line.startswith("a1"))
        self.assertEqual(first.split(), ["a1", "a@b", "0", "active", "yes", "yes"])
        second = next(line for line in out.splitlines() if line.startswith("a2"))
        self.assertEqual(second.split(), ["a2", "c@d", "-", "-", "no", "no"])

    def test_names_prints_email_or_id_one_per_line(self):
        api = AccountsApi([{"id": "a1", "email": "a@b"}, {"id": "a2"}])
        result, out = run_account(api, cli.cmd_account_ls, names=True)
        self.assertEqual(result, 0)
        self.assertEqual(out.splitlines(), ["a@b", "a2"])


class AccountUsageTest(unittest.TestCase):
    def test_used_is_limit_minus_remaining_and_missing_usage_is_dashes(self):
        api = AccountsApi(
            [
                {
                    "email": "a@b",
                    "usage": {
                        "limit_bytes": 5 * 1024 ** 3,
                        "remaining_bytes": 2 * 1024 ** 3,
                        "reset_at": "2026-11-15T00:00:00Z",
                    },
                    "used_percent": 60,
                },
                {"email": "c@d"},
            ]
        )
        result, out = run_account(api, cli.cmd_account_usage)
        self.assertEqual(result, 0)
        row = next(line for line in out.splitlines() if line.startswith("a@b"))
        self.assertEqual(row.split()[1:], ["3.0", "GB", "5.0", "GB", "2.0", "GB", "60.0%", "2026-11-15"])
        empty = next(line for line in out.splitlines() if line.startswith("c@d"))
        self.assertEqual(empty.split(), ["c@d", "-", "-", "-", "-", "-"])

    def test_used_percent_keeps_one_decimal(self):
        api = AccountsApi(
            [
                {
                    "email": "a@b",
                    "usage": {
                        "limit_bytes": 100,
                        "remaining_bytes": 92,
                        "reset_at": "2026-11-15T00:00:00Z",
                    },
                    "used_percent": 7.84,
                }
            ]
        )
        result, out = run_account(api, cli.cmd_account_usage)
        self.assertEqual(result, 0)
        self.assertIn("7.8%", out)
        self.assertNotIn(" 8%", out)


class AccountSetTest(unittest.TestCase):
    def test_activates_resolved_account_and_prints_email(self):
        api = AccountsApi(
            [
                {"id": "a1", "email": "alice@example.com"},
                {"id": "a2", "email": "bob@example.com"},
            ]
        )
        result, out = run_account(api, cli.cmd_account_set, account="bob")
        self.assertEqual(result, 0)
        self.assertEqual(api.posted(), [("post", "/api/accounts/a2/activate", None)])
        self.assertIn("bob@example.com", out)

    def test_unknown_account_does_not_activate(self):
        api = AccountsApi([{"id": "a1", "email": "alice@example.com"}])
        with self.assertRaises(cli.FfvpnError):
            run_account(api, cli.cmd_account_set, account="nobody")
        self.assertEqual(api.posted(), [])


class AccountLogoutTest(unittest.TestCase):
    def test_single_account_with_yes_is_deleted(self):
        api = AccountsApi([{"id": "a1", "email": "alice@example.com"}])
        result, out = run_account(api, cli.cmd_account_logout, yes=True)
        self.assertEqual(result, 0)
        self.assertEqual(api.deleted(), [("delete", "/api/accounts/a1")])
        self.assertIn("alice@example.com", out)

    def test_several_accounts_without_name_refuses(self):
        api = AccountsApi(
            [
                {"id": "a1", "email": "alice@example.com"},
                {"id": "a2", "email": "bob@example.com"},
            ]
        )
        with self.assertRaises(cli.FfvpnError) as raised:
            run_account(api, cli.cmd_account_logout, yes=True)
        self.assertIn("name the account", str(raised.exception))
        self.assertEqual(api.deleted(), [])

    def test_no_accounts_says_how_to_sign_in(self):
        api = AccountsApi([])
        with self.assertRaises(cli.FfvpnError) as raised:
            run_account(api, cli.cmd_account_logout, yes=True)
        self.assertIn("no accounts", str(raised.exception))
        self.assertIn("ffvpn account login", str(raised.exception))
        self.assertEqual(api.deleted(), [])

    def test_named_account_in_non_tty_refuses_without_yes(self):
        api = AccountsApi(
            [
                {"id": "a1", "email": "alice@example.com"},
                {"id": "a2", "email": "bob@example.com"},
            ]
        )
        with mock.patch.object(cli.sys, "stdin", io.StringIO("y\n")):
            with self.assertRaises(cli.FfvpnError) as raised:
                run_account(api, cli.cmd_account_logout, account="bob")
        self.assertIn("--yes", str(raised.exception))
        self.assertEqual(api.deleted(), [])

    def test_single_account_in_non_tty_refuses_without_yes(self):
        api = AccountsApi([{"id": "a1", "email": "alice@example.com"}])
        with mock.patch.object(cli.sys, "stdin", io.StringIO("y\n")):
            with self.assertRaises(cli.FfvpnError):
                run_account(api, cli.cmd_account_logout)
        self.assertEqual(api.deleted(), [])

    def test_zero_accounts_without_yes_refuses(self):
        api = AccountsApi([])
        with mock.patch.object(cli.sys, "stdin", io.StringIO("y\n")), \
                self.assertRaises(cli.FfvpnError) as raised:
            run_account(api, cli.cmd_account_logout)
        self.assertIn("no accounts", str(raised.exception))
        self.assertEqual(api.deleted(), [])

    def test_single_account_tty_declined_keeps_account(self):
        api = AccountsApi([{"id": "a1", "email": "alice@example.com"}])

        class Tty:
            def isatty(self):
                return True

        with mock.patch.object(cli.sys, "stdin", Tty()), mock.patch(
            "builtins.input", return_value="n"
        ), contextlib.redirect_stderr(io.StringIO()):
            result, _ = run_account(api, cli.cmd_account_logout)
        self.assertEqual(result, 1)
        self.assertEqual(api.deleted(), [])

    def test_unknown_name_refuses_without_delete(self):
        api = AccountsApi(
            [
                {"id": "a1", "email": "alice@example.com"},
                {"id": "a2", "email": "bob@example.com"},
            ]
        )
        with self.assertRaises(cli.FfvpnError) as raised:
            run_account(api, cli.cmd_account_logout, account="nobody", yes=True)
        self.assertIn("no account matches", str(raised.exception))
        self.assertEqual(api.deleted(), [])

    def test_named_account_in_several_removes_only_that_one(self):
        api = AccountsApi(
            [
                {"id": "a1", "email": "alice@example.com"},
                {"id": "a2", "email": "bob@example.com"},
            ]
        )
        result, out = run_account(api, cli.cmd_account_logout, account="bob", yes=True)
        self.assertEqual(result, 0)
        self.assertEqual(api.deleted(), [("delete", "/api/accounts/a2")])
        self.assertIn("bob@example.com", out)
        self.assertNotIn("alice@example.com", out)


# --- AC5: login steps -------------------------------------------------------


class LoginStepsTest(unittest.TestCase):
    def test_typed_code_steps(self):
        steps = [
            ("need_email_code", "Email code: "),
            ("need_unblock_code", "Unblock code: "),
            ("need_totp", "Authentication code: "),
        ]
        for step, label in steps:
            with self.subTest(step=step):

                class StepApi:
                    def __init__(self):
                        self.calls = []

                    def post(self, path, body=None, timeout=None):
                        self.calls.append((path, body))
                        if path == "/api/signin":
                            return {"id": "s1", "step": step, "message": ""}
                        return {"step": "done", "email": "a@b"}

                    def get(self, path):
                        raise AssertionError("should not poll")

                api = StepApi()
                with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
                    cli, "read_password", return_value="secret"
                ), mock.patch.object(cli, "ask", return_value="999") as asked, contextlib.redirect_stdout(
                    io.StringIO()
                ), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.cmd_account_login(mock.Mock(email="a@b")), 0)
                asked.assert_called_once_with(label)
                self.assertEqual(api.calls[-1], ("/api/signin/s1", {"value": "999"}))

    def test_email_link_step_posts_empty_value(self):
        class LinkApi:
            def __init__(self):
                self.calls = []

            def post(self, path, body=None, timeout=None):
                self.calls.append((path, body))
                if path == "/api/signin":
                    return {"id": "s1", "step": "need_email_link", "message": ""}
                return {"step": "done", "email": "a@b"}

        api = LinkApi()
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(cli, "ask", return_value="") as asked, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_account_login(mock.Mock(email="a@b")), 0)
        self.assertIn("Press Enter", asked.call_args.args[0])
        self.assertEqual(api.calls[-1], ("/api/signin/s1", {"value": ""}))

    def test_captcha_writes_png_and_posts_typed_characters(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

        class CaptchaApi:
            def __init__(self):
                self.calls = []

            def post(self, path, body=None, timeout=None):
                self.calls.append((path, body))
                if path == "/api/signin":
                    return {
                        "id": "s1",
                        "step": "need_captcha",
                        "message": "",
                        "captcha": data_uri,
                    }
                return {"step": "done", "email": "a@b"}

            def get(self, path):
                raise AssertionError("should not poll")

        api = CaptchaApi()
        err = io.StringIO()
        seen = {}

        def open_image(command, **kwargs):
            seen["image"] = command[1]
            with open(command[1], "rb") as f:
                seen["head"] = f.read(8)

        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(cli, "ask", return_value="AB12"), mock.patch.object(
            cli.shutil, "which", return_value="/usr/bin/xdg-open"
        ), mock.patch.object(
            cli.subprocess, "run", side_effect=open_image
        ), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_account_login(mock.Mock(email="a@b")), 0)

        image = seen["image"]
        self.assertTrue(image.endswith(".png"))
        self.assertEqual(seen["head"], png[:8])
        self.assertFalse(os.path.exists(image), "CAPTCHA temp file should be removed")
        self.assertIn("CAPTCHA image", err.getvalue())
        self.assertEqual(api.calls[-1], ("/api/signin/s1", {"value": "AB12"}))

    def test_reissued_captcha_prompts_again(self):
        first = "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"\x00" * 8).decode("ascii")
        second = "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"\x00" * 24).decode("ascii")

        class RetryCaptchaApi:
            def __init__(self):
                self.calls = []
                self.views = [
                    {
                        "id": "s1",
                        "step": "need_captcha",
                        "message": "Try again",
                        "error": "wrong characters",
                        "updated": 1,
                        "captcha": first,
                    },
                    {
                        "id": "s1",
                        "step": "need_captcha",
                        "message": "Try again",
                        "error": "wrong characters",
                        "updated": 2,
                        "captcha": second,
                    },
                    {"step": "done", "email": "a@b"},
                ]

            def post(self, path, body=None, timeout=None):
                self.calls.append((path, body))
                return self.views.pop(0)

            def get(self, path):
                raise AssertionError("should not poll")

        api = RetryCaptchaApi()
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(
            cli, "ask", side_effect=["AA11", "BB22"]
        ) as asked, mock.patch.object(
            cli.shutil, "which", return_value=None
        ), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(cli.cmd_account_login(mock.Mock(email="a@b")), 0)
        self.assertEqual(asked.call_count, 2)
        self.assertEqual(
            api.calls[-2:],
            [
                ("/api/signin/s1", {"value": "AA11"}),
                ("/api/signin/s1", {"value": "BB22"}),
            ],
        )

    def test_error_deletes_server_side_session(self):
        class TotpApi:
            def __init__(self):
                self.calls = []

            def post(self, path, body=None, timeout=None):
                self.calls.append(("post", path, body))
                return {"id": "s1", "step": "need_totp", "message": ""}

            def delete(self, path):
                self.calls.append(("delete", path))
                return {}

        api = TotpApi()
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(
            cli, "ask", side_effect=cli.FfvpnError("no input")
        ), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(cli.FfvpnError):
                cli.cmd_account_login(mock.Mock(email="a@b"))
        self.assertIn(("delete", "/api/signin/s1"), api.calls)

    def test_failed_step_exits_one_with_server_error(self):
        class FailedApi:
            def post(self, path, body=None, timeout=None):
                return {"id": "s1", "step": "failed", "error": "bad password"}

            def delete(self, path):
                return {}

            def get(self, path):
                raise AssertionError("should not poll")

        err = io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=FailedApi()), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["account", "login", "a@b"]), 1)
        self.assertIn("bad password", err.getvalue())

    def test_ctrl_c_sends_delete_and_returns_130(self):
        class TotpApi:
            def __init__(self):
                self.calls = []

            def post(self, path, body=None, timeout=None):
                self.calls.append(("post", path, body))
                if path == "/api/signin":
                    return {"id": "s1", "step": "need_totp", "message": ""}
                return {"step": "done"}

            def get(self, path):
                raise AssertionError("should not poll")

            def delete(self, path):
                self.calls.append(("delete", path))
                return {}

        api = TotpApi()
        err = io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(cli, "ask", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_account_login(mock.Mock(email="a@b")), 130)
        self.assertIn(("delete", "/api/signin/s1"), api.calls)
        self.assertIn("cancelled", err.getvalue())


# --- AC5: polling sign-in state machine -------------------------------------


class LoginPollTest(unittest.TestCase):
    def test_poll_drives_step_change_and_deduplicates_messages(self):
        class PollingApi:
            def __init__(self):
                self.calls = []
                self.get_views = [
                    {"id": "s1", "step": "waiting", "message": "Waiting for the browser"},
                    {"id": "s1", "step": "need_email_link", "message": "Open the link"},
                    {"id": "s1", "step": "done", "email": "a@b"},
                ]

            def post(self, path, body=None, timeout=None):
                self.calls.append(("post", path, body))
                if path == "/api/signin":
                    return {
                        "id": "s1",
                        "step": "waiting",
                        "message": "Waiting for the browser",
                    }
                return {
                    "id": "s1",
                    "step": "waiting",
                    "message": "Waiting for the browser",
                }

            def get(self, path):
                self.calls.append(("get", path))
                return self.get_views.pop(0)

        api = PollingApi()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(cli.time, "sleep"), mock.patch.object(
            cli, "ask", return_value=""
        ) as asked, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_account_login(mock.Mock(email="a@b")), 0)

        self.assertEqual(
            api.calls,
            [
                ("post", "/api/signin", {"email": "a@b", "password": "secret"}),
                ("get", "/api/signin/s1"),
                ("get", "/api/signin/s1"),
                ("post", "/api/signin/s1", {"value": ""}),
                ("get", "/api/signin/s1"),
            ],
        )
        asked.assert_called_once_with("Press Enter once you opened the link")
        # Printed once from the POST view and once after the step changes back;
        # the identical consecutive GET view is not reprinted.
        self.assertEqual(err.getvalue().count("Waiting for the browser"), 2)
        self.assertEqual(err.getvalue().count("Open the link"), 1)
        self.assertIn("Added a@b", out.getvalue())

    def test_polled_failure_cancels_session_and_exits_one(self):
        class FailingPollApi:
            def __init__(self):
                self.calls = []

            def post(self, path, body=None, timeout=None):
                self.calls.append(("post", path, body))
                return {"id": "s1", "step": "waiting", "message": "Waiting"}

            def get(self, path):
                self.calls.append(("get", path))
                return {"id": "s1", "step": "failed", "error": "email not verified"}

            def delete(self, path):
                self.calls.append(("delete", path))
                return {}

        api = FailingPollApi()
        err = io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(cli.time, "sleep"), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["account", "login", "a@b"]), 1)
        self.assertIn("email not verified", err.getvalue())
        self.assertIn(("get", "/api/signin/s1"), api.calls)
        self.assertIn(("delete", "/api/signin/s1"), api.calls)

    def test_poll_timeout_cancels_session_and_exits_one(self):
        class WaitingApi:
            def __init__(self):
                self.calls = []

            def post(self, path, body=None, timeout=None):
                self.calls.append(("post", path, body))
                return {"id": "s1", "step": "waiting", "message": "Waiting"}

            def get(self, path):
                self.calls.append(("get", path))
                return {"id": "s1", "step": "waiting", "message": "Waiting"}

            def delete(self, path):
                self.calls.append(("delete", path))
                return {}

        api = WaitingApi()
        err = io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=api), mock.patch.object(
            cli, "read_password", return_value="secret"
        ), mock.patch.object(cli, "SIGNIN_TIMEOUT", -1), mock.patch.object(
            cli.time, "sleep"
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["account", "login", "a@b"]), 1)
        self.assertIn("sign-in timed out", err.getvalue())
        self.assertIn(("delete", "/api/signin/s1"), api.calls)


# --- AC5: password handling -------------------------------------------------


class PasswordTest(unittest.TestCase):
    def test_getpass_used_on_tty_and_nothing_echoed(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        out = io.StringIO()
        with mock.patch.object(cli.sys, "stdin", stdin), mock.patch.object(
            cli.getpass, "getpass", return_value="hunter2"
        ) as getpass_mock, contextlib.redirect_stdout(out):
            self.assertEqual(cli.read_password(), "hunter2")
        getpass_mock.assert_called_once_with("Password: ")
        stdin.readline.assert_not_called()
        self.assertEqual(out.getvalue(), "")

    def test_password_is_not_an_argument(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.build_parser().parse_args(["account", "login", "a@b", "--password", "x"])
        self.assertEqual(raised.exception.code, 2)

    def test_password_never_printed(self):
        class DoneApi:
            def post(self, path, body=None, timeout=None):
                return {"id": "s1", "step": "done", "email": "a@b"}

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "make_api", return_value=DoneApi()), mock.patch.object(
            cli, "read_password", return_value="TOPSECRET"
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["account", "login", "a@b"]), 0)
        self.assertNotIn("TOPSECRET", out.getvalue() + err.getvalue())


class StdinInputTest(unittest.TestCase):
    def test_ask_prompt_goes_to_stderr_and_value_comes_from_pipe(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "stdin", io.StringIO("hunter2\n")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.ask("Password: "), "hunter2")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "Password: ")

    def test_ask_eof_raises_no_input(self):
        with mock.patch.object(cli.sys, "stdin", io.StringIO("")), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(cli.FfvpnError) as raised:
            cli.ask("Password: ")
        self.assertIn("no input", str(raised.exception))

    def test_read_password_reads_one_line_from_pipe(self):
        with mock.patch.object(cli.sys, "stdin", io.StringIO("p@ss word\n")):
            self.assertEqual(cli.read_password(), "p@ss word")

    def test_read_password_eof_raises(self):
        with mock.patch.object(cli.sys, "stdin", io.StringIO("")), \
                self.assertRaises(cli.FfvpnError) as raised:
            cli.read_password()
        self.assertIn("no password", str(raised.exception))


# --- AC7: main exit-code mapping -------------------------------------------


class ExitCodeTest(unittest.TestCase):
    def test_ffvpn_error_returns_one_and_prints_message(self):
        err = io.StringIO()
        with mock.patch.object(cli, "make_api", side_effect=cli.FfvpnError("no token")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["ls"]), 1)
        self.assertIn("ffvpn: no token", err.getvalue())

    def test_argparse_error_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["not-a-command"])
        self.assertEqual(raised.exception.code, 2)

    def test_keyboard_interrupt_returns_130(self):
        with mock.patch.object(cli, "make_api", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["disconnect"]), 130)


# --- AC6: completions content ----------------------------------------------


class CompletionContentTest(unittest.TestCase):
    def test_bash_names_all_commands_and_options(self):
        script = cli.completion_script("bash")
        for token in (
            "connect",
            "disconnect",
            "ls",
            "account",
            "completion",
            "--proxy",
            "--system-proxy",
            "--tun",
            "ls usage set logout login",
            "bash fish",
            "--data-dir",
            "account ls --names 2>/dev/null",
        ):
            self.assertIn(token, script)

    def test_fish_names_all_commands_and_options(self):
        script = cli.completion_script("fish")
        for token in (
            "-a connect",
            "-a disconnect",
            "-a ls",
            "-a account",
            "-a completion",
            "-l proxy",
            "-l system-proxy",
            "-l tun",
            "ls usage set logout login",
            "bash fish",
            "account ls --names 2>/dev/null",
        ):
            self.assertIn(token, script)

    def test_bash_skips_data_dir_before_command_and_completes_account_names(self):
        if not shutil.which("bash"):
            self.skipTest("bash is not installed")
        probe = """
ffvpn() { if [ "$*" = "account ls --names" ]; then printf 'a@b\\nc@d\\n'; fi; }
COMP_WORDS=(ffvpn -d /tmp account set "")
COMP_CWORD=5
_ffvpn
printf '%s\\n' "${COMPREPLY[@]}"
"""
        result = subprocess.run(
            ["bash", "-c", cli.completion_script("bash") + probe],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["a@b", "c@d"])

    def test_bash_completes_account_names_for_logout(self):
        if not shutil.which("bash"):
            self.skipTest("bash is not installed")
        probe = """
ffvpn() { if [ "$*" = "account ls --names" ]; then printf 'a@b\\nc@d\\n'; fi; }
COMP_WORDS=(ffvpn --data-dir /tmp account logout "")
COMP_CWORD=5
_ffvpn
printf '%s\\n' "${COMPREPLY[@]}"
"""
        result = subprocess.run(
            ["bash", "-c", cli.completion_script("bash") + probe],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["a@b", "c@d"])


class DataDirArgumentTest(unittest.TestCase):
    def test_accepted_after_every_subcommand(self):
        parser = cli.build_parser()
        cases = [
            (["ls", "--data-dir", "/x"], "/x"),
            (["connect", "-d", "/x"], "/x"),
            (["disconnect", "--data-dir", "/x"], "/x"),
            (["account", "ls", "--data-dir", "/x"], "/x"),
            (["account", "set", "a", "-d", "/x"], "/x"),
            (["account", "logout", "--data-dir", "/x"], "/x"),
            (["account", "usage", "-d", "/x"], "/x"),
            (["account", "-d", "/x", "ls"], "/x"),
            (["completion", "bash", "--data-dir", "/x"], "/x"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).data_dir, expected)

    def test_before_and_after_command_after_wins(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["--data-dir", "/a", "ls"]).data_dir, "/a")
        self.assertEqual(
            parser.parse_args(["--data-dir", "/a", "ls", "-d", "/b"]).data_dir, "/b"
        )
        self.assertIsNone(parser.parse_args(["ls"]).data_dir)

    def test_help_lists_data_dir(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(["ls", "--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--data-dir", out.getvalue())


class DataDirAuthorityTest(unittest.TestCase):
    def make_dir(self, token):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        if token:
            with open(os.path.join(directory, "local-api.token"), "w", encoding="utf-8") as f:
                f.write("secret\n")
        return directory

    def make_default_with_token(self):
        xdg = self.make_dir(token=False)
        default = os.path.join(xdg, "firefox-vpn-client")
        os.makedirs(default)
        with open(os.path.join(default, "local-api.token"), "w", encoding="utf-8") as f:
            f.write("secret\n")
        return xdg, default

    def assert_no_fallback(self, argv, expected, default, forbidden=()):
        err = io.StringIO()
        with mock.patch.object(cli, "Api") as api_class, contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(argv), 1)
        message = err.getvalue()
        self.assertIn(f"no local API token in {expected}", message)
        self.assertNotIn(default, message)
        for path in forbidden:
            self.assertNotIn(path, message)
        api_class.assert_not_called()

    def test_flag_after_command_names_that_dir_and_never_falls_back(self):
        explicit = self.make_dir(token=False)
        xdg, default = self.make_default_with_token()
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": ""}, clear=False
        ):
            self.assert_no_fallback(["ls", "-d", explicit], explicit, default)

    def test_environment_dir_names_that_dir_and_never_falls_back(self):
        env_dir = self.make_dir(token=False)
        xdg, default = self.make_default_with_token()
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": env_dir}, clear=False
        ):
            self.assert_no_fallback(["ls"], env_dir, default)

    def test_last_flag_wins_and_never_falls_back(self):
        first = self.make_dir(token=False)
        second = self.make_dir(token=False)
        xdg, default = self.make_default_with_token()
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": xdg, "FFVPN_DATA_DIR": ""}, clear=False
        ):
            self.assert_no_fallback(
                ["-d", first, "ls", "--data-dir", second], second, default, [first]
            )


if __name__ == "__main__":
    unittest.main()
