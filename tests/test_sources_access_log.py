"""A real Uvicorn process must not log untrusted request targets."""

import http.client
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4

from uvicorn.logging import AccessFormatter


def test_real_uvicorn_access_log_redacts_request_targets():
    marker = "synthetic-access-secret-" + uuid4().hex
    # Positive control: the unprotected Uvicorn formatter reveals this marker.
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ("-", "GET", "/?password=" + marker, "1.1", 422), None)
    assert marker in AccessFormatter('%(message)s').format(record)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("DFM_DB_") and k != "DATAFLOW_FERNET_KEY"}
    process = subprocess.Popen(
        [str(Path('.venv/bin/python').absolute()), "-B", "-m", "uvicorn", "dataflow_manager.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    output = b""
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/api/v1/sources?password=" + marker)
                response = connection.getresponse()
                assert response.status == 422
                assert marker.encode() not in response.read()
                connection.close()
                break
            except (OSError, http.client.HTTPException):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("local server did not become available") from None
                time.sleep(0.05)
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate(timeout=5)[0]
    assert marker.encode() not in output
    assert b"/[redacted]" in output
