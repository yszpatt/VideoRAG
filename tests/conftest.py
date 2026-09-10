import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _restore_cwd():
    """隔离 CWD 副作用：db.init_db → ensure_dirs 会 os.chdir(data_dir)。

    data_dir 常是 pytest 的 tmp_path，测试结束该目录可能被清理；若不恢复
    CWD，后续测试会在「已删除的当前目录」下建文件（LanceDB/SQLite → OSError）。
    每个测试前后保存/恢复 CWD，从根上消除跨测试污染。
    """
    cwd = os.getcwd()
    try:
        yield
    finally:
        try:
            os.chdir(cwd)
        except OSError:
            pass


@pytest.fixture
async def app(tmp_path):
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    return create_app(settings=settings)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
