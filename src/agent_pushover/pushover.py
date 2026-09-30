"""Small HTTPS client. Never log tokens, API response bodies, or notification content."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API = "https://api.pushover.net/1/"


@dataclass
class DeliveryError(Exception):
    code: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeliveryError("unexpected_redirect", False)


def post(path: str, payload: dict, timeout: float = 5, opener=None) -> dict:
    if path not in ("messages.json", "users/validate.json"):
        raise ValueError("Unsupported Pushover endpoint")
    request = urllib.request.Request(API + path, data=urllib.parse.urlencode(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": "agent-pushover/1.0.0"}, method="POST")
    try:
        client = opener or urllib.request.build_opener(NoRedirect())
        with client.open(request, timeout=timeout) as response:
            result = json.loads(response.read(65536))
        if not isinstance(result, dict) or result.get("status") != 1:
            raise DeliveryError("api_rejected_request", False)
        return result
    except urllib.error.HTTPError as exc:
        # 429 is Pushover's delivery quota, not a coding agent rate-limit event.
        raise DeliveryError(f"pushover_http_{exc.code}", 500 <= exc.code <= 599) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DeliveryError("network_or_tls_error", True) from None
    except (ValueError, UnicodeError):
        raise DeliveryError("invalid_api_response", True) from None


def validate(credentials: dict, timeout: float = 5, opener=None) -> None:
    post("users/validate.json", credentials, timeout, opener)


def send(credentials: dict, message: dict, config: dict, opener=None) -> None:
    payload = {**credentials, **message}
    for key in ("device", "sound"):
        if config["pushover"].get(key):
            payload[key] = config["pushover"][key]
    post("messages.json", payload, config["pushover"]["timeout_seconds"], opener)
