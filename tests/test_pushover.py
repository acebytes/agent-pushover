import io
import json
import unittest
import urllib.error
import urllib.parse

from helpers import ROOT
from agent_pushover.config import DEFAULTS
from agent_pushover.pushover import DeliveryError, NoRedirect, post, send, validate


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = {"status": 1} if response is None else response
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.response).encode())


class ClientTests(unittest.TestCase):
    def test_post_fields_and_utf8(self):
        client = FakeOpener()
        send({"token": "t" * 30, "user": "u" * 30}, {"title": "Codex 🔔", "message": "Need approval", "priority": 1},
             DEFAULTS, opener=client)
        req, timeout = client.requests[0]
        self.assertEqual(req.full_url, "https://api.pushover.net/1/messages.json")
        values = urllib.parse.parse_qs(req.data.decode())
        self.assertEqual(values["title"], ["Codex 🔔"])
        self.assertEqual(values["priority"], ["1"])
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, 5)

    def test_validation_endpoint(self):
        client = FakeOpener()
        validate({"token": "t" * 30, "user": "u" * 30}, opener=client)
        self.assertTrue(client.requests[0][0].full_url.endswith("/users/validate.json"))

    def test_status_zero_not_success(self):
        with self.assertRaises(DeliveryError) as context:
            post("messages.json", {}, opener=FakeOpener({"status": 0, "errors": ["secret server body"]}))
        self.assertFalse(context.exception.retryable)
        self.assertNotIn("secret", str(context.exception))

    def test_http_429_not_retried(self):
        err = urllib.error.HTTPError("url", 429, "quota", {}, io.BytesIO(b'private'))
        with self.assertRaises(DeliveryError) as context:
            post("messages.json", {}, opener=FakeOpener(error=err))
        self.assertEqual(context.exception.code, "pushover_http_429")
        self.assertFalse(context.exception.retryable)

    def test_http_500_retried(self):
        err = urllib.error.HTTPError("url", 500, "oops", {}, io.BytesIO())
        with self.assertRaises(DeliveryError) as context:
            post("messages.json", {}, opener=FakeOpener(error=err))
        self.assertTrue(context.exception.retryable)

    def test_network_error_is_sanitized_and_retryable(self):
        err = urllib.error.URLError("error containing SECRET_TOKEN")
        with self.assertRaises(DeliveryError) as context:
            post("messages.json", {}, opener=FakeOpener(error=err))
        self.assertTrue(context.exception.retryable)
        self.assertNotIn("SECRET_TOKEN", str(context.exception))

    def test_endpoint_cannot_be_replaced(self):
        with self.assertRaises(ValueError):
            post("https://attacker.invalid", {})

    def test_no_redirects(self):
        with self.assertRaises(DeliveryError):
            NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://other.invalid")


if __name__ == '__main__':
    unittest.main()
