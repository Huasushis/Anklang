"""启动入口：python -m anklang"""
from __future__ import annotations

import sys

from .config import ConfigError, load_config
from .server import serve


def main() -> int:
    try:
        config = load_config()
    except ConfigError as error:
        sys.stderr.write(f"配置错误：{error}\n")
        return 2
    sys.stderr.write(f"Anklang 反向代理已启动，监听端口 {config.port}。\n")
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
