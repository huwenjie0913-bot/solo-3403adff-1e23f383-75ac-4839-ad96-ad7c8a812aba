import os
import tempfile

import pytest

# 必须在 app.storage 被导入前指定
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix="curve_test_", suffix=".db")
os.close(_DB_FD)
os.unlink(_DB_PATH)
os.environ["CURVE_DB_PATH"] = _DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app import storage  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    storage.init_db(_DB_PATH)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    if os.path.exists(_DB_PATH):
        os.unlink(_DB_PATH)
    storage.init_db(_DB_PATH)
    yield
