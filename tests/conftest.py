import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
sys.path.insert(0, str(EXAMPLES))

import demo_server  # noqa: E402

DEMO_SERVER = EXAMPLES / "demo_server.py"


def _serve(use_sse: bool):
    server = demo_server.make_http_server("127.0.0.1", 0, use_sse=use_sse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/mcp"


@pytest.fixture(params=[False, True], ids=["json", "sse"])
def http_url(request):
    server, url = _serve(request.param)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def json_url():
    server, url = _serve(False)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def stdio_command():
    return [sys.executable, str(DEMO_SERVER), "--stdio"]
