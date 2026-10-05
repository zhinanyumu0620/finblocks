"""启动组内网页版；账户与上传资料存放在独立持久化目录。"""

from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import shutil

from finblocks.deployment import OnlinePolicy, DataUploads
from finblocks.web import Workspace, make_handler


def prepare_workspace(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination in source.parents:
        raise ValueError("网页版持久化目录须独立于源码根目录")
    for relative in ("data/demo_manifest.json", "data/csi300_manifest.json", "data/fundamental_capabilities.json",
                     "data/data_upload_spec.json", "docs/audit_evidence/data_statistics.json",
                     "docs/audit_evidence/workspace_inventory.json", "examples/ma_strategy.json"):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    for path in (source / "web").iterdir():
        if path.is_file():
            target = destination / "web" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    return destination


def main():
    policy = OnlinePolicy(os.environ.get("FINBLOCKS_PUBLIC_ORIGIN", os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/"),
                          os.environ.get("FINBLOCKS_INVITE_CODE", ""), os.environ.get("FINBLOCKS_ADMIN_CODE", ""))
    destination = os.environ.get("FINBLOCKS_WORKSPACE")
    if not destination:
        raise ValueError("须设置FINBLOCKS_WORKSPACE，保存资料与账号")
    root = prepare_workspace(Path(__file__).resolve().parent, destination)
    workspace = Workspace(root, policy)
    workspace.uploads = DataUploads(root)
    port = int(os.environ.get("PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("端口无效")
    with ThreadingHTTPServer(("0.0.0.0", port), make_handler(workspace)) as server:
        print("FINBLOCKS_ONLINE: " + policy.origin + "; invite registration required; data download disabled", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError):
        raise SystemExit("网页版启动失败，请检查域名、两项独立口令、资料配置和持久化目录；不显示凭据或私人路径")
