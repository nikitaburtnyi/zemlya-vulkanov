"""Network-free regression checks: python3 -m unittest -v test_mirror_site.py."""

from email.message import Message
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
import mirror_site as mirror


API_URL = f"https://api.github.com/repos/{mirror.CANONICAL_REPO}/commits/main"
RAW_URL = f"https://raw.githubusercontent.com/{mirror.CANONICAL_REPO}/" + "a" * 40 + "/updates.json"


def http_error(code, headers=None, body=b""):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(API_URL, code, "fixture failure", message, io.BytesIO(body))


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, url, headers=None):
        super().__init__(data)
        self.url = url
        self.headers = headers or {"Content-Length": str(len(data))}

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, Response):
            return action
        return Response(action, request.full_url)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.sleeps = []
        self.monotonic = patch.object(mirror.time, "monotonic", side_effect=lambda: self.now)
        self.sleep = patch.object(mirror.time, "sleep", side_effect=self.sleep_for)
        self.wall_time = patch.object(mirror.time, "time", return_value=1_700_000_000.0)
        self.environment = patch.dict(os.environ, {"GITHUB_TOKEN": "unit-test-token"}, clear=True)
        self.stderr = io.StringIO()
        self.stderr_patch = patch.object(mirror.sys, "stderr", self.stderr)
        for context in [self.monotonic, self.sleep, self.wall_time, self.environment, self.stderr_patch]:
            context.start()
            self.addCleanup(context.stop)

    def sleep_for(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def fetch(self, actions, url=API_URL, deadline=340.0):
        opener = FakeOpener(actions)
        with patch.object(mirror, "PUBLIC_OPENER", opener):
            result = mirror.fetch_public(url, deadline)
        return result, opener

    def test_authentication_is_only_sent_to_api(self):
        result, api = self.fetch([b"{}"])
        self.assertEqual(result, b"{}")
        self.assertEqual(api.requests[0][0].get_header("Authorization"), "Bearer unit-test-token")
        _, raw = self.fetch([b"{}"], RAW_URL)
        self.assertIsNone(raw.requests[0][0].get_header("Authorization"))
        self.assertNotIn("unit-test-token", self.stderr.getvalue())

    def test_missing_token_stays_anonymous(self):
        with patch.dict(os.environ, {}, clear=True):
            _, opener = self.fetch([b"{}"])
        self.assertIsNone(opener.requests[0][0].get_header("Authorization"))

    def test_api_redirect_cannot_forward_the_token(self):
        request = urllib.request.Request(API_URL, headers={"Authorization": "Bearer unit-test-token"})
        with self.assertRaises(urllib.error.HTTPError):
            mirror.NoRedirects().redirect_request(
                request, io.BytesIO(), 302, "redirect", Message(), RAW_URL
            )
        opener = FakeOpener([http_error(302, {"Location": RAW_URL}), b"should not be fetched"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(RuntimeError):
            mirror.fetch_public(API_URL, 340)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_rate_limited_403_retries_with_reset(self):
        result, opener = self.fetch([
            http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000002"}), b"{}"
        ])
        self.assertEqual(result, b"{}")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [3])

    def test_secondary_rate_limit_body_retries(self):
        result, opener = self.fetch([http_error(403, body=b'{"message":"API rate limit exceeded"}'), b"{}"])
        self.assertEqual(result, b"{}")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [1])

    def test_5xx_and_429_retry(self):
        result, opener = self.fetch([http_error(503), http_error(429, {"Retry-After": "2"}), b"{}"])
        self.assertEqual(result, b"{}")
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(self.sleeps, [1, 2])

    def test_transport_errors_retry(self):
        result, opener = self.fetch([urllib.error.URLError("offline"), ConnectionResetError(), b"{}"])
        self.assertEqual(result, b"{}")
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(self.sleeps, [1, 2])
        self.assertNotIn("unit-test-token", self.stderr.getvalue())

    def test_partial_download_restarts_without_partial_bytes(self):
        class BrokenResponse(Response):
            def read(self, size=-1):
                if self.tell():
                    raise ConnectionResetError()
                return super().read(2)
        result, opener = self.fetch([BrokenResponse(b"broken", API_URL), b"correct"])
        self.assertEqual(result, b"correct")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [1])

    def test_deadline_expiring_during_read_stops_without_retry(self):
        test = self
        class SlowResponse(Response):
            def read(self, size=-1):
                test.now += 1
                return super().read(size)
        opener = FakeOpener([SlowResponse(b"{}", API_URL), b"not used"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(TimeoutError):
            mirror.fetch_public(API_URL, self.now + 0.5)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_deadline_expiring_at_eof_is_not_accepted(self):
        test = self
        class LateEOFResponse(Response):
            def read(self, size=-1):
                data = super().read(size)
                if not data:
                    test.now += 1000
                return data
        opener = FakeOpener([LateEOFResponse(b"{}", API_URL), b"not used"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(TimeoutError):
            mirror.fetch_public(API_URL, self.now + 1)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_attempt_limit_is_four(self):
        opener = FakeOpener([http_error(502) for _ in range(4)])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(RuntimeError):
            mirror.fetch_public(API_URL, 340)
        self.assertEqual(len(opener.requests), 4)
        self.assertEqual(self.sleeps, [1, 2, 4])

    def test_404_and_non_rate_limit_403_do_not_retry(self):
        for code in [404, 403]:
            with self.subTest(code=code):
                opener = FakeOpener([http_error(code), b"not used"])
                with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(RuntimeError):
                    mirror.fetch_public(API_URL, 340)
                self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_expired_deadline_never_opens_connection(self):
        opener = FakeOpener([b"not used"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(TimeoutError):
            mirror.fetch_public(API_URL, self.now)
        self.assertEqual(opener.requests, [])

    def test_retry_cannot_cross_deadline(self):
        opener = FakeOpener([http_error(503), b"not used"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(TimeoutError):
            mirror.fetch_public(API_URL, self.now + 0.5)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0][1], 0.5)
        self.assertEqual(self.sleeps, [])

    def test_long_retry_after_is_not_slept_or_ignored(self):
        opener = FakeOpener([http_error(429, {"Retry-After": "3600"}), b"not used"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(TimeoutError):
            mirror.fetch_public(API_URL, 340)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_size_validation_is_not_retried(self):
        response = Response(b"{}", API_URL, {"Content-Length": str(mirror.MAX_FILE_BYTES + 1)})
        opener = FakeOpener([response, b"not used"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(ValueError):
            mirror.fetch_public(API_URL, 340)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_invalid_manifest_json_is_not_retried(self):
        opener = FakeOpener([json.dumps({"sha": "a" * 40}).encode(), b"{"])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(ValueError):
            mirror.remote_snapshot()
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [])

    def test_only_dated_bulletin_can_clear_warnings(self):
        files = {"index.html": b"<html></html>", "updates.json": b'{"files":{"activity":1}}',
                 "activity.json": b'{"version":1}'}
        for stamp in (None, "2026-02-31", "20261124", ""):
            with self.subTest(stamp=stamp):
                files["notices.json"] = json.dumps({"updated": stamp, "items": []}).encode()
                with self.assertRaisesRegex(ValueError, "updated"):
                    mirror.validate_snapshot(files)
        files["notices.json"] = b'{"updated":"2026-11-24","items":[]}'
        self.assertIn("notices.json", mirror.validate_snapshot(files))

    def test_version_validation_is_not_retried(self):
        snapshot = {
            "updates.json": b'{"files":{"activity":1}}',
            "index.html": b"<html></html>",
            "notices.json": b'{"updated":"2026-11-24","items":[]}',
            "activity.json": b'{"version":2}',
        }
        opener = FakeOpener([json.dumps({"sha": "a" * 40}).encode()] + [
            snapshot[name] for name in ["updates.json", "index.html", "notices.json", "activity.json"]
        ])
        with patch.object(mirror, "PUBLIC_OPENER", opener), self.assertRaises(ValueError):
            files, _ = mirror.remote_snapshot()
            mirror.validate_snapshot(files)
        self.assertEqual(len(opener.requests), 5)
        self.assertEqual(self.sleeps, [])
        for request, _ in opener.requests[1:]:
            self.assertIn("/" + "a" * 40 + "/", request.full_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
