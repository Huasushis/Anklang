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
    try:
        serve(config)
    except Exception:
        # 监听失败、数据文件异常等启动错误可能携带路径或配置原文；命令行只给
        # 固定结论，详细排查应在不记录私密内容的本机诊断中完成。
        sys.stderr.write("Anklang 启动失败，请检查监听地址和本地运行配置。\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
