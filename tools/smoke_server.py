"""浏览器手动验收的服务端替身：假 SDK 工厂 + 隔离簿记 + 真实静态 dist。

用法：python3 -m tools.smoke_server [port]
只用于本机手动验收多标签页前端，不触真 SDK、不碰全局 ~/.auto-image-web。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from web.app import create_app
from web.fake import FakeSessionFactory


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8199
    app = create_app(
        session_factory=FakeSessionFactory(delay=0.3),
        title_factory=FakeSessionFactory(
            script=[{"type": "result", "subtype": "success", "result": "假标题（验收桩）"}]
        ),
        state_path=Path("/tmp/auto-image-smoke-state.json"),
        artifact_roots={
            "deploy": Path("/tmp/auto-image-smoke-artifacts/deploy"),
            "rpm": Path("/tmp/auto-image-smoke-artifacts/rpm"),
        },
    )
    print(f"smoke server on http://127.0.0.1:{port}（假 SDK，state/artifacts 落 /tmp）")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
