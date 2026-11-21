import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.gettempdir(), "signaldesk_pytest_" + str(os.getpid()) + ".sqlite"
)
os.environ["DEMO_INLINE_WORKER"] = "1"
os.environ["DEMO_MODE"] = "1"
os.environ.pop("SMTP_HOST", None)
import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.database import Base, engine
from app.seed import seed


@pytest.fixture(autouse=True)
def reset_db():
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        Base.metadata.drop_all(bind=conn)
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    seed()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def login(client):
    def _login(user_id):
        r = client.post("/api/session", json={"user_id": user_id})
        assert r.status_code == 204, r.text
        return client

    return _login
