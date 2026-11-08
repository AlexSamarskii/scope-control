"""Fixed-destination, bounded HTTP child. No Django, logging, or config files.

The parent enforces a wall deadline by terminating this process. The child also
sets a POSIX alarm so a lost worker does not leave an unbounded network operation.
"""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys

import httpx

HOST = "https://ai.api.cloud.yandex.net"
PATHS = {"tokenizeCompletion", "completion"}
WALL_SECONDS = 40
INPUT_LIMIT = 1048576


@dataclass(frozen=True)
class HTTPReply:
    status: int = 0
    body: bytes = b""
    error: str = ""


class _Deadline(BaseException):
    pass


@contextmanager
def _wall_alarm(seconds):
    def expired(*args):
        raise _Deadline()

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def perform_request(
    path, payload, api_key, *, transport=None, base_url=HOST, wall_seconds=WALL_SECONDS
):
    """Private transport/URL hooks support local tests, never public configuration."""
    if path not in PATHS or not api_key or not 0 < wall_seconds <= WALL_SECONDS:
        return HTTPReply(error="configuration_missing")
    maximum = 4 * 1048576 if path == "tokenizeCompletion" else 262144
    try:
        with _wall_alarm(wall_seconds):
            with httpx.Client(
                trust_env=False,
                follow_redirects=False,
                verify=True,
                timeout=httpx.Timeout(connect=3, read=30, write=5, pool=1),
                transport=transport,
            ) as client:
                with client.stream(
                    "POST",
                    f"{base_url}/foundationModels/v1/{path}",
                    json=payload,
                    headers={
                        "Authorization": f"Api-Key {api_key}",
                        "x-folder-id": payload["modelUri"].split("/")[2],
                        "x-data-logging-enabled": "false",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    # Error bodies are neither parsed nor propagated.
                    if response.status_code != 200:
                        return HTTPReply(status=response.status_code)
                    if response.headers.get("content-encoding", "identity") not in {
                        "identity",
                        "",
                    }:
                        return HTTPReply(error="provider_transport")
                    body = bytearray()
                    for chunk in response.iter_raw():
                        if len(body) + len(chunk) > maximum:
                            return HTTPReply(error="response_too_large")
                        body.extend(chunk)
                    return HTTPReply(status=200, body=bytes(body))
    except (_Deadline, httpx.TimeoutException):
        return HTTPReply(error="provider_timeout")
    except httpx.ConnectError:
        return HTTPReply(error="provider_connection")
    except Exception:
        return HTTPReply(error="provider_transport")


def request_http(path, payload):
    """One child per HTTP request; no body/key in argv, files, or inherited stdout."""
    data = json.dumps({"path": path, "payload": payload}, ensure_ascii=False).encode()
    if len(data) > INPUT_LIMIT or path not in PATHS:
        return HTTPReply(error="input_too_large")
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "mainapp.services.analysis_yandex_http"],
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        output, _ = process.communicate(data, timeout=WALL_SECONDS)
        if process.returncode != 0 or len(output) > 6 * 1048576:
            return HTTPReply(error="provider_transport")
        value = json.loads(output)
        if (
            set(value) != {"status", "body", "error"}
            or type(value["status"]) is not int
        ):
            return HTTPReply(error="provider_transport")
        allowed = {
            "",
            "configuration_missing",
            "provider_timeout",
            "provider_connection",
            "provider_transport",
            "input_too_large",
            "response_too_large",
        }
        if value["error"] not in allowed:
            return HTTPReply(error="provider_transport")
        body = base64.b64decode(value["body"], validate=True)
        maximum = 4 * 1048576 if path == "tokenizeCompletion" else 262144
        if len(body) > maximum:
            return HTTPReply(error="response_too_large")
        return HTTPReply(value["status"], body, value["error"])
    except subprocess.TimeoutExpired:
        return HTTPReply(error="provider_timeout")
    except Exception:
        return HTTPReply(error="provider_transport")
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            # Reap the child, including timed-out networking, before returning.
            process.communicate()


def main():
    logging.disable(logging.CRITICAL)
    result = HTTPReply(error="provider_transport")
    try:
        data = sys.stdin.buffer.read(INPUT_LIMIT + 1)
        if len(data) <= INPUT_LIMIT:
            value = json.loads(data)
            if type(value) is dict and set(value) == {"path", "payload"}:
                result = perform_request(
                    value["path"],
                    value["payload"],
                    os.environ.get("SCOPE_YANDEX_API_KEY", ""),
                )
    except BaseException:
        pass
    wire = {
        "status": result.status,
        "body": base64.b64encode(result.body).decode(),
        "error": result.error,
    }
    sys.stdout.write(json.dumps(wire))


if __name__ == "__main__":
    main()
