"""运行时配置持久化：$DATA_DIR/runtime.env（key=value 风格）。

在线修改的配置写入该文件（挂载持久卷，重启容器不丢）。
优先级：环境变量 > runtime.env > 默认值（见 Settings 说明）。
"""

from pathlib import Path


def runtime_env_path(data_dir: str) -> Path:
    return Path(data_dir) / "runtime.env"


def load_runtime_env(data_dir: str) -> dict[str, str]:
    """读取运行时覆盖配置；文件不存在或为空返回 {}。"""
    path = runtime_env_path(data_dir)
    if not path.is_file():
        return {}
    overrides: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        overrides[key.strip()] = value.strip()
    return overrides


def save_runtime_env(data_dir: str, overrides: dict[str, str]) -> None:
    """整体覆写 runtime.env；先读旧文件保留未涉及的键，再写回。"""
    path = runtime_env_path(data_dir)
    current = load_runtime_env(data_dir)
    current.update(overrides)
    lines = [f"{k}={v}" for k, v in current.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
