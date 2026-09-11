"""Bounded stdlib HTTP requests; errors never include response bodies."""
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import ElkError, validate_url

MAX_RESPONSE = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self, base_url, service, *, opener=None, timeout=5):
        self.base = validate_url(base_url, service)
        self.service = service
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.timeout = timeout

    def request(self, method, path, body=None, *, allow_missing=False, text=False):
        if method not in {"GET", "HEAD", "POST", "PUT"} or not path.startswith("/") or path.startswith("//"):
            raise ElkError("unsupported_request", self.service)
        headers = {"Accept": "application/json", "kbn-xsrf": "day12"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(MAX_RESPONSE + 1)
                if len(payload) > MAX_RESPONSE:
                    raise ElkError("response_too_large", self.service)
                if text:
                    return payload.decode("utf-8").strip()
                if method == "HEAD":
                    return {}
                result = json.loads(payload)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except HTTPError as error:
            code = error.code
            error.close()
            if code == 404 and allow_missing:
                return None
            raise ElkError("http_error", self.service, code) from None
        except (TimeoutError, socket.timeout):
            raise ElkError("timeout", self.service) from None
        except URLError as error:
            category = "timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "unreachable"
            raise ElkError(category, self.service) from None
        except (ValueError, UnicodeError):
            raise ElkError("invalid_response", self.service) from None
        except OSError:
            raise ElkError("connection_error", self.service) from None


def elasticsearch_ready(http):
    result = http.request("GET", "/_cluster/health")
    if result.get("status") not in ("green", "yellow") or result.get("timed_out", False):
        raise ElkError("not_ready", "Elasticsearch")


def kibana_ready(http):
    result = http.request("GET", "/api/status")
    status = result.get("status")
    overall = status.get("overall") if isinstance(status, dict) else None
    if not isinstance(overall, dict) or overall.get("level") != "available":
        raise ElkError("not_ready", "Kibana")


def wait_until(action, seconds=60, interval=2, *, clock=time.monotonic, sleep=time.sleep):
    if not 0 <= seconds <= 300 or not 0 < interval <= 10:
        raise ElkError("invalid_wait")
    deadline = clock() + seconds
    while True:
        try:
            return action()
        except ElkError:
            remaining = deadline - clock()
            if remaining <= 0:
                raise
            sleep(min(interval, remaining))
