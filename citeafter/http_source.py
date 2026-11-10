"""Bounded policy transport. Cache identity and observation time belong to the caller.

The supported 200 profile requires exactly one Content-Length, JSON MIME and
identity content coding. All Transfer-Encoding and close-delimited responses are
refused. This narrow profile permits checking that every declared byte arrived.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit
from typing import Literal

__all__ = ["FetchResult", "TypedFetchError", "validate_source_url", "fetch_policy"]

_MAX_BYTES = 262144
_MAX_REQUEST = 16384
_MAX_ETAG = 1024
_MESSAGES = {
    "invalid_url": "Source URL is outside the supported pinned endpoint profile.",
    "configuration": "Fetch limits are invalid.",
    "etag": "Source validator is invalid or does not match the conditional request.",
    "redirect": "Source redirects are not followed.",
    "http_status": "Source did not return a supported successful status.",
    "content_type": "Source must provide UTF-8 JSON content.",
    "content_encoding": "Source content coding must be identity.",
    "framing": "Source response framing is invalid, incomplete or unsupported.",
    "too_large": "Source response exceeds the configured byte limit.",
    "utf8": "Source response is not valid UTF-8.",
    "json": "Source response is not a supported JSON document.",
    "timeout": "Source fetch exceeded its wall deadline.",
    "tls": "Source TLS verification or negotiation failed.",
    "network": "Source connection could not complete.",
    "worker": "Isolated source fetch could not complete.",
}


class TypedFetchError(Exception):
    """A failure without publisher body, URL, credentials or raw exception text."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int | None = None,
        elapsed_ms: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.elapsed_ms = elapsed_ms


@dataclass(frozen=True)
class FetchResult:
    status: Literal[200, 304]
    body: bytes | None
    etag: str | None
    response_sha256: str | None
    elapsed_ms: float


def _fail(code: str, status: int | None = None) -> None:
    raise TypedFetchError(code, _MESSAGES[code], status) from None


def validate_source_url(url: str) -> str:
    """Return the unchanged pinned ASCII URI, without resolving a DNS name."""
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 4096
        or not url.isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        or any(c in url for c in "\\?#")
        or re.search(r"%(?![0-9A-Fa-f]{2})", url)
    ):
        _fail("invalid_url")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        if (
            parts.scheme not in ("http", "https")
            or not host
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
            or parts.netloc.endswith(":")
            or "%" in host
            or (port is not None and not 1 <= port <= 65535)
        ):
            _fail("invalid_url")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if parts.scheme == "http":
            if address is None or not address.is_loopback:
                _fail("invalid_url")
        elif address is None:
            labels = host.split(".")
            if len(host) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels
            ):
                _fail("invalid_url")
    except (ValueError, UnicodeError):
        _fail("invalid_url")
    return url


def _etag(value: str | None, status: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_ETAG:
        _fail("etag", status)
    opaque = value[2:] if value.startswith("W/") else value
    if len(opaque) < 2 or opaque[0] != '"' or opaque[-1] != '"':
        _fail("etag", status)
    if any(not (ord(c) == 33 or 35 <= ord(c) <= 126 or 128 <= ord(c) <= 255) for c in opaque[1:-1]):
        _fail("etag", status)
    return opaque


def _limits(timeout: float, max_bytes: int) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 60
        or not math.isfinite(timeout)
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= _MAX_BYTES
    ):
        _fail("configuration")


def _one_header(response: http.client.HTTPResponse, name: str) -> str | None:
    values = response.headers.get_all(name, [])
    if len(values) > 1 or any("\r" in v or "\n" in v for v in values):
        _fail("framing", response.status)
    return values[0].strip() if values else None


def _content_headers(response: http.client.HTTPResponse) -> None:
    coding = _one_header(response, "Content-Encoding")
    if coding is not None and coding.lower() != "identity":
        _fail("content_encoding", response.status)
    content_type = _one_header(response, "Content-Type")
    if content_type is None:
        if response.status == 200:
            _fail("content_type", response.status)
        return
    mime = content_type.split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"application/(?:json|[a-z0-9!#$&^_.+-]+\+json)", mime):
        _fail("content_type", response.status)
    parameters = response.headers.get_params(header="Content-Type", failobj=[])[1:]
    charsets = [value for name, value in parameters if name.lower() == "charset"]
    if len(charsets) > 1 or (
        charsets and (not isinstance(charsets[0], str) or charsets[0].lower() not in ("utf-8", "utf8"))
    ):
        _fail("content_type", response.status)


def _json_document(body: bytes, status: int) -> None:
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeError:
        _fail("utf8", status)

    def reject_constant(_value: str) -> None:
        raise ValueError("Non-JSON constant")

    try:
        # Syntax only; policy types, duplicate keys and authority are core concerns.
        json.loads(text, parse_int=str, parse_float=str, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        _fail("json", status)


def _perform(url: str, etag: str | None, timeout: float, max_bytes: int) -> dict:
    validate_source_url(url)
    _etag(etag)
    _limits(timeout, max_bytes)
    parts = urlsplit(url)
    connection = None
    status = None
    try:
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(
                parts.hostname,
                parts.port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if etag is not None:
            headers["If-None-Match"] = etag
        connection.request("GET", parts.path or "/", headers=headers)
        response = connection.getresponse()
        status = response.status
        if 300 <= status <= 399 and status != 304:
            _fail("redirect", status)
        if status not in (200, 304):
            _fail("http_status", status)
        if response.headers.defects:
            _fail("framing", status)
        _content_headers(response)
        if _one_header(response, "Transfer-Encoding") is not None:
            _fail("framing", status)
        length = _one_header(response, "Content-Length")
        if length is not None and not re.fullmatch(r"[0-9]{1,20}", length):
            _fail("framing", status)
        response_etag = _one_header(response, "ETag")
        opaque = _etag(response_etag, status)
        if status == 304:
            if etag is None or (response_etag is not None and opaque != _etag(etag)):
                _fail("etag", status)
            # A304 ends at the headers; Content-Length can describe the cached200.
            return {
                "status": 304,
                "body": None,
                "etag": response_etag if response_etag is not None else etag,
                "response_sha256": None,
                "content_length": None,
            }
        if length is None:
            _fail("framing", status)
        declared = int(length)
        if declared > max_bytes:
            _fail("too_large", status)
        body = response.read(declared)
        if len(body) != declared:
            _fail("framing", status)
        _json_document(body, status)
        return {
            "status": 200,
            "body": base64.b64encode(body).decode("ascii"),
            "etag": response_etag,
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "content_length": declared,
        }
    except TypedFetchError:
        raise
    except (ssl.SSLError, ssl.CertificateError):
        _fail("tls", status)
    except (TimeoutError, socket.timeout):
        _fail("timeout", status)
    except http.client.HTTPException:
        _fail("framing", status)
    except (OSError, ValueError, UnicodeError):
        _fail("network", status)
    finally:
        if connection is not None:
            connection.close()


def _worker_main() -> int:
    try:
        data = sys.stdin.buffer.read(_MAX_REQUEST + 1)
        if len(data) > _MAX_REQUEST:
            _fail("worker")
        request = json.loads(data.decode("ascii"))
        if not isinstance(request, dict) or set(request) != {
            "url",
            "etag",
            "timeout",
            "max_bytes",
        }:
            _fail("worker")
        reply = {"ok": True, **_perform(**request)}
    except TypedFetchError as error:
        reply = {"ok": False, "code": error.code, "status": error.status}
    except Exception:
        reply = {"ok": False, "code": "worker", "status": None}
    sys.stdout.write(json.dumps(reply, ensure_ascii=True, separators=(",", ":")))
    return 0


def fetch_policy(
    url: str,
    *,
    etag: str | None = None,
    timeout: float = 2.0,
    max_bytes: int = _MAX_BYTES,
) -> FetchResult:
    """Fetch once with a parent wall deadline spanning DNS, TLS, headers and body.

    The parent kills and reaps only its direct worker on expiry/cancellation.
    OS process creation/termination scheduling can add small cleanup overhead;
    elapsed_ms includes it. No observation/effective timestamp is manufactured.
    """
    started = time.monotonic()
    process = None
    failure = None

    def elapsed() -> float:
        return round((time.monotonic() - started) * 1000, 3)

    try:
        validate_source_url(url)
        _etag(etag)
        _limits(timeout, max_bytes)
        deadline = started + timeout
        request = json.dumps(
            {"url": url, "etag": etag, "timeout": timeout, "max_bytes": max_bytes},
            ensure_ascii=True,
        ).encode("ascii")
        if len(request) > _MAX_REQUEST:
            _fail("configuration")
        # Isolated startup bypasses user site/config and all proxy/credential env.
        environment = (
            {"SystemRoot": os.environ["SystemRoot"]} if os.name == "nt" and "SystemRoot" in os.environ else {}
        )
        process = subprocess.Popen(
            [
                str(Path(sys.executable).resolve()),
                "-I",
                "-S",
                "-B",
                str(Path(__file__).resolve()),
                "--worker",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
            cwd=Path(__file__).resolve().parent,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("timeout")
        try:
            output, errors = process.communicate(request, timeout=remaining)
        except subprocess.TimeoutExpired:
            _fail("timeout")
        if process.returncode != 0 or errors or len(output) > 4 * ((max_bytes + 2) // 3) + 8192:
            _fail("worker")
        try:
            reply = json.loads(output.decode("ascii"))
        except (ValueError, UnicodeError):
            _fail("worker")
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            _fail("worker")
        if not reply["ok"]:
            if set(reply) != {"ok", "code", "status"} or reply["code"] not in _MESSAGES:
                _fail("worker")
            status = reply["status"]
            if status is not None and (
                isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 999
            ):
                _fail("worker")
            _fail(reply["code"], status)
        if set(reply) != {
            "ok",
            "status",
            "body",
            "etag",
            "response_sha256",
            "content_length",
        }:
            _fail("worker")
        _etag(reply["etag"])
        if (
            reply["status"] == 304
            and etag is not None
            and reply["body"] is None
            and reply["response_sha256"] is None
            and reply["content_length"] is None
        ):
            if _etag(reply["etag"]) != _etag(etag):
                _fail("worker")
            body = None
        elif reply["status"] == 200 and isinstance(reply["body"], str):
            try:
                body = base64.b64decode(reply["body"], validate=True)
            except ValueError:
                _fail("worker")
            if (
                len(body) > max_bytes
                or isinstance(reply["content_length"], bool)
                or not isinstance(reply["content_length"], int)
                or len(body) != reply["content_length"]
                or hashlib.sha256(body).hexdigest() != reply["response_sha256"]
            ):
                _fail("worker")
        else:
            _fail("worker")
        if time.monotonic() >= deadline:
            _fail("timeout")
        return FetchResult(reply["status"], body, reply["etag"], reply["response_sha256"], elapsed())
    except TypedFetchError as error:
        failure = error
        error.elapsed_ms = elapsed()
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        failure = TypedFetchError("worker", _MESSAGES["worker"], elapsed_ms=elapsed())
        raise failure from None
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.communicate()
        if failure is not None:
            failure.elapsed_ms = elapsed()


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    raise SystemExit(_worker_main())
