"""组员启动入口：仅用标准库，选择空闲本机端口，不安装依赖。"""

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import webbrowser


def main():
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更新版本。")
        return 2
    from finblocks.web import Workspace, make_handler
    from finblocks.data import DataError
    parser = argparse.ArgumentParser(description="启动 FinBlocks 组员工作台")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--port", type=int, help="指定本机端口；未指定时优先8765，否则使用空闲端口")
    args = parser.parse_args()
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("端口须为0–65535；0表示自动分配")
    root = Path(__file__).resolve().parent
    try:
        workspace = Workspace(root)
        handler = make_handler(workspace)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.port if args.port is not None else 8765), handler)
        except OSError:
            if args.port is not None:
                raise
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        with server:
            url = f"http://127.0.0.1:{server.server_port}/"
            print("FINBLOCKS_WORKBENCH: " + url, flush=True)
            if not workspace.archive.is_file():
                print("尚未导入行情数据：可以浏览、编辑策略和使用本机账号；回测请先阅读 TEAM_README.md。", flush=True)
            print("关闭此窗口或按 Ctrl+C 停止服务。", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("工作台已停止", flush=True)
        return 0
    except (OSError, DataError, ValueError):
        print("启动失败，请检查端口和资料完整性；不会绕过数据校验。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
