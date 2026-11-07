import os
import tempfile
import pytest

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("LLM_API_KEY", "test")


@pytest.fixture(scope="session", autouse=True)
def _db():
    from app import db
    db.init_db(force=True)
