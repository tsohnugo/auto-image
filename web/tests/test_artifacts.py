#!/usr/bin/env python3
"""产物端点主缝测试 —— deploy/ + rpm/ 多根浏览、目录分组排序、内容读取、
单文件下载、批量 zip、路径约束。

缝：同 test_api 的 ASGI 测试客户端；产物目录在临时目录造桩
（artifact_roots 注入），浏览不依赖会话，无需剧本推进。纯 assert，无 pytest。

运行：python web/tests/test_artifacts.py
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.fake import FakeSessionFactory  # noqa: E402
from web.tests.support import async_client, make_test_app  # noqa: E402

# deploy.config.yaml 约定文件名的样例集（nginx 1.25 造桩）
GUIDE_FILES = ("nginx-install.md", "nginx-verify.md")
INSTALL_FILES = ("nginx-install-result.md", "nginx-install-issues.md", "nginx-install-meta.json")
VERIFY_FILES = ("nginx-verify-result.md", "nginx-verify-issues.md")
ARCHIVE_FILES = ("nginx-archive-result.md", "nginx-deploy-list.md", "nginx-archive-issues.md")

# rpm 流水线约定文件名样例集（rpm-build / rpm-verify / rpm-archive 内置默认）
RPM_BUILD_FILES = ("nginx-rpm-result.md", "nginx-rpm-issues.md")
RPM_VERIFY_FILES = ("nginx-rpm-verify-result.md", "nginx-rpm-verify-issues.md")
RPM_ARCHIVE_FILES = ("nginx-rpm-archive-result.md", "nginx-rpm-deliver-list.md", "install-rpm.sh")
# rpm-archive 归档时收集的产物包（rpms/{binary,source,deps}/，.rpm 为二进制）
RPM_PACKAGE_FILES = {
    "rpms/binary": ("nginx-1.25.3-1.aarch64.rpm",),
    "rpms/source": ("nginx-1.25.3-1.src.rpm",),
    "rpms/deps": ("pcre2-10.42-1.aarch64.rpm", "openssl-libs-1.1.1-15.aarch64.rpm"),
}


def make_output_tree(root, rel, names, mtime=None):
    """在产物根下造一个产物目录（rel 相对路径），写入给定产物文件。

    mtime 显式给定：分组排序按组内文件 mtime 最大值，固定时间戳保证
    断言确定（不与真实时钟竞速）。
    """
    out = root / rel if rel else root
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = out / name
        if name.endswith(".json"):
            path.write_text(json.dumps({"path": "create", "server_alias": "srv"}), encoding="utf-8")
        else:
            path.write_text(f"# {name}\n\n产物样例，含 | 表格 | 与 `代码块`。\n", encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
    return out


# 产物文件名约定的契约快照（与项目根 deploy.config.yaml 一致）：权威源
# 改名时此桩不同步、清单测试落空，即提醒两端对齐
DEPLOY_CONFIG_STUB = """\
install_file: "{{software}}-install.md"
verify_file: "{{software}}-verify.md"
install_result_file: "{{software}}-install-result.md"
install_issues_file: "{{software}}-install-issues.md"
install_meta_file: "{{software}}-install-meta.json"
verify_result_file: "{{software}}-verify-result.md"
verify_issues_file: "{{software}}-verify-issues.md"
archive_result_file: "{{software}}-archive-result.md"
deploy_list_file: "{{software}}-deploy-list.md"
archive_issues_file: "{{software}}-archive-issues.md"
"""


# 与生产同构：config 在根、产物在其下 deploy/ 与 rpm/ 两个根目录
# （config 不进浏览清单）
def make_app(root):
    (root / "deploy.config.yaml").write_text(DEPLOY_CONFIG_STUB, encoding="utf-8")
    (root / "deploy").mkdir(exist_ok=True)
    (root / "rpm").mkdir(exist_ok=True)
    return make_test_app(
        session_factory=FakeSessionFactory(script=[]),
        artifact_roots={"deploy": root / "deploy", "rpm": root / "rpm"},
        deploy_config=root / "deploy.config.yaml",
    )


async def run_with_client(root):
    return async_client(make_app(root))


async def test_browse_groups_by_dir_latest_mtime_desc():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy = root / "deploy"
        deploy.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES + INSTALL_FILES, mtime=2000)
        make_output_tree(deploy, "redis/7.2", GUIDE_FILES, mtime=3000)
        # 非约定文件（.v1 备份、杂项）与根下散落文件同样在场
        make_output_tree(deploy, "pi/0.84", ("pi-config", "pi-install-result.md.v1", "pi-install-result.md"), mtime=1000)
        (deploy / "README.md").write_text("根散落", encoding="utf-8")
        os.utime(deploy / "README.md", (4000, 4000))
        async with await run_with_client(root) as client:
            r = await client.get("/api/artifacts")
            assert r.status_code == 200, r.text
            groups = r.json()["groups"]
            # 组序 = 组内最新落盘时间降序（根散落文件最晚 → 在前）；组键带
            # 根前缀（根下散落文件归 "deploy" 组）
            assert [g["dir"] for g in groups] == [
                "deploy", "deploy/redis/7.2", "deploy/nginx/1.25", "deploy/pi/0.84",
            ], groups
            by_dir = {g["dir"]: g for g in groups}
            # 约定文件带正确阶段；组内文件名升序
            nginx = by_dir["deploy/nginx/1.25"]["files"]
            assert [f["name"] for f in nginx] == sorted(f["name"] for f in nginx)
            stages = {f["name"]: f["stage"] for f in nginx}
            assert stages["nginx-install.md"] == "GUIDE"
            assert stages["nginx-install-meta.json"] == "INSTALL"
            # 非约定文件无徽标（stage None）且如实列出
            pi_stages = {f["name"]: f["stage"] for f in by_dir["deploy/pi/0.84"]["files"]}
            assert pi_stages["pi-config"] is None
            assert pi_stages["pi-install-result.md.v1"] is None
            assert pi_stages["pi-install-result.md"] == "INSTALL"
            # size 如实（内容长度）
            by_name = {f["name"]: f for f in nginx}
            assert by_name["nginx-install.md"]["size"] == (deploy / "nginx/1.25/nginx-install.md").stat().st_size


async def test_browse_rpm_root_with_stage_badges():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rpm = root / "rpm"
        rpm.mkdir()
        make_output_tree(rpm, "nginx/1.25.3", RPM_BUILD_FILES + RPM_VERIFY_FILES + RPM_ARCHIVE_FILES, mtime=2000)
        async with await run_with_client(root) as client:
            r = await client.get("/api/artifacts")
            assert r.status_code == 200, r.text
            groups = r.json()["groups"]
            assert [g["dir"] for g in groups] == ["rpm/nginx/1.25.3"], groups
            stages = {f["name"]: f["stage"] for f in groups[0]["files"]}
            assert stages["nginx-rpm-result.md"] == "BUILD"
            assert stages["nginx-rpm-issues.md"] == "BUILD"
            assert stages["nginx-rpm-verify-result.md"] == "VERIFY"
            assert stages["nginx-rpm-verify-issues.md"] == "VERIFY"
            assert stages["nginx-rpm-archive-result.md"] == "ARCHIVE"
            assert stages["nginx-rpm-deliver-list.md"] == "ARCHIVE"
            assert stages["install-rpm.sh"] == "ARCHIVE"


async def test_browse_rpm_packages_binary_flag_and_badge():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rpm = root / "rpm"
        rpm.mkdir()
        make_output_tree(rpm, "nginx/1.25.3", RPM_BUILD_FILES, mtime=1000)
        for rel, names in RPM_PACKAGE_FILES.items():
            make_output_tree(rpm, f"nginx/1.25.3/{rel}", names, mtime=2000)
        async with await run_with_client(root) as client:
            r = await client.get("/api/artifacts")
            assert r.status_code == 200, r.text
            by_dir = {g["dir"]: g for g in r.json()["groups"]}
            assert set(by_dir) == {
                "rpm/nginx/1.25.3/rpms/binary",
                "rpm/nginx/1.25.3/rpms/source",
                "rpm/nginx/1.25.3/rpms/deps",
                "rpm/nginx/1.25.3",
            }, sorted(by_dir)
            # .rpm / .src.rpm 一律 BUILD 徽标 + binary 标记（前端走占位视图）
            for group_dir, names in RPM_PACKAGE_FILES.items():
                files = {f["name"]: f for f in by_dir[f"rpm/nginx/1.25.3/{group_dir}"]["files"]}
                for name in names:
                    assert files[name]["stage"] == "BUILD", (name, files[name])
                    assert files[name].get("binary") is True, name
            # 文本产物不带 binary 键
            for f in by_dir["rpm/nginx/1.25.3"]["files"]:
                assert "binary" not in f, f


async def test_read_binary_package_422_with_download_hint():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rpm = root / "rpm"
        rpm.mkdir()
        make_output_tree(rpm, "nginx/1.25.3/rpms/binary", ("nginx-1.25.3-1.aarch64.rpm",), mtime=1000)
        async with await run_with_client(root) as client:
            rel = "rpm/nginx/1.25.3/rpms/binary/nginx-1.25.3-1.aarch64.rpm"
            r = await client.get(f"/api/artifacts/file/{rel}")
            assert r.status_code == 422, r.text
            assert "/api/artifacts/download/" in r.json()["detail"], r.text
            # 下载端点照常按原始字节服务
            r = await client.get(f"/api/artifacts/download/{rel}")
            assert r.status_code == 200, r.text
            assert r.content == (rpm / "nginx/1.25.3/rpms/binary/nginx-1.25.3-1.aarch64.rpm").read_bytes()


async def test_zip_includes_binary_packages():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rpm = root / "rpm"
        rpm.mkdir()
        make_output_tree(rpm, "nginx/1.25.3/rpms/binary", ("nginx-1.25.3-1.aarch64.rpm",), mtime=1000)
        make_output_tree(rpm, "nginx/1.25.3/rpms/deps", RPM_PACKAGE_FILES["rpms/deps"], mtime=1000)
        async with await run_with_client(root) as client:
            paths = [
                "rpm/nginx/1.25.3/rpms/binary/nginx-1.25.3-1.aarch64.rpm",
                *[
                    f"rpm/nginx/1.25.3/rpms/deps/{n}"
                    for n in RPM_PACKAGE_FILES["rpms/deps"]
                ],
            ]
            r = await client.post("/api/artifacts/zip", json={"paths": paths})
            assert r.status_code == 200, r.text
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            assert sorted(zf.namelist()) == sorted(paths), zf.namelist()
            for p in paths:
                assert zf.read(p) == (rpm / p[len("rpm/"):]).read_bytes(), p


async def test_browse_both_roots_mixed_and_sorted():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy, rpm = root / "deploy", root / "rpm"
        deploy.mkdir()
        rpm.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES, mtime=1000)
        make_output_tree(rpm, "nginx/1.25.3", RPM_BUILD_FILES, mtime=2000)
        async with await run_with_client(root) as client:
            r = await client.get("/api/artifacts")
            groups = r.json()["groups"]
            # 两根混排，统一按组内最新落盘时间降序
            assert [g["dir"] for g in groups] == ["rpm/nginx/1.25.3", "deploy/nginx/1.25"], groups


async def test_browse_empty_roots():
    with tempfile.TemporaryDirectory() as tmp:
        async with await run_with_client(Path(tmp)) as client:
            r = await client.get("/api/artifacts")
            assert r.status_code == 200, r.text
            assert r.json() == {"groups": []}, r.json()


async def test_read_returns_content_across_roots():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy, rpm = root / "deploy", root / "rpm"
        deploy.mkdir()
        rpm.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES + INSTALL_FILES, mtime=1000)
        make_output_tree(deploy, "pi/0.84", ("pi-config",), mtime=1000)
        make_output_tree(rpm, "nginx/1.25.3", RPM_BUILD_FILES + RPM_ARCHIVE_FILES, mtime=1000)
        async with await run_with_client(root) as client:
            r = await client.get("/api/artifacts/file/deploy/nginx/1.25/nginx-install.md")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "nginx-install.md"
            assert body["dir"] == "deploy/nginx/1.25"
            assert body["stage"] == "GUIDE"
            assert body["content"] == (deploy / "nginx/1.25/nginx-install.md").read_text(encoding="utf-8")
            # json 产物与非约定文件同样原文返回（后者 stage 为 None）
            r = await client.get("/api/artifacts/file/deploy/nginx/1.25/nginx-install-meta.json")
            assert r.status_code == 200, r.text
            assert r.json()["stage"] == "INSTALL"
            r = await client.get("/api/artifacts/file/deploy/pi/0.84/pi-config")
            assert r.status_code == 200, r.text
            assert r.json()["stage"] is None
            # rpm 根同一路径形态读取，徽标按 rpm 约定
            r = await client.get("/api/artifacts/file/rpm/nginx/1.25.3/nginx-rpm-result.md")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["dir"] == "rpm/nginx/1.25.3"
            assert body["stage"] == "BUILD"
            r = await client.get("/api/artifacts/file/rpm/nginx/1.25.3/install-rpm.sh")
            assert r.status_code == 200, r.text
            assert r.json()["stage"] == "ARCHIVE"


async def test_read_traversal_and_missing_404():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy = root / "deploy"
        deploy.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES, mtime=1000)
        async with await run_with_client(root) as client:
            # 越界、缺失、指向目录、未知根、二进制/空路径一律 404；明文 `..`
            # 段在客户端即被规范化，到服务端的是编码形式
            for rel in (
                "deploy%2F..%2F..%2Fdeploy.config.yaml",   # URL 编码的越界路径
                "deploy/..%2F..%2Fscope.yaml",             # 根段后夹带 ..
                "%2e%2e%2f%2e%2e%2fscope.yaml",            # 无根前缀的越界
                "%2Fetc%2Fpasswd",                         # 绝对路径（Path / "/abs" 会丢根）
                "guides/nginx/install-guide.md",           # 未注册的根（只有 deploy/rpm）
                "deploy",                                  # 只剩根名，没有文件段
                "deploy/nginx/1.25",                       # 指向目录本身
                "deploy/nginx/1.25/nginx-absent.md",       # 不存在
                "rpm/nginx/1.25.3/nginx-rpm-result.md",    # rpm 根未造桩（缺失）
            ):
                r = await client.get(f"/api/artifacts/file/{rel}")
                assert r.status_code == 404, (rel, r.text)
            # 空 rel（路由命中但路径为空）
            r = await client.get("/api/artifacts/file/")
            assert r.status_code == 404, r.text


async def test_download_serves_raw_bytes_as_attachment():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy, rpm = root / "deploy", root / "rpm"
        deploy.mkdir()
        rpm.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES, mtime=1000)
        make_output_tree(rpm, "nginx/1.25.3", ("install-rpm.sh",), mtime=1000)
        async with await run_with_client(root) as client:
            for rel, path in (
                ("deploy/nginx/1.25/nginx-install.md", deploy / "nginx/1.25/nginx-install.md"),
                ("rpm/nginx/1.25.3/install-rpm.sh", rpm / "nginx/1.25.3/install-rpm.sh"),
            ):
                r = await client.get(f"/api/artifacts/download/{rel}")
                assert r.status_code == 200, (rel, r.text)
                assert r.content == path.read_bytes()  # 原始字节（非 JSON 包装）
                assert "attachment" in r.headers.get("content-disposition", "")
                assert path.name in r.headers.get("content-disposition", "")
            # 下载与内容端点共用路径约束：越界/未知根/缺失同样 404
            for rel in (
                "deploy%2F..%2F..%2Fscope.yaml",
                "guides/nginx/install-guide.md",
                "deploy/nginx/1.25/nginx-absent.md",
            ):
                r = await client.get(f"/api/artifacts/download/{rel}")
                assert r.status_code == 404, (rel, r.text)


async def test_zip_batches_selected_paths_across_roots():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy, rpm = root / "deploy", root / "rpm"
        deploy.mkdir()
        rpm.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES, mtime=1000)
        make_output_tree(rpm, "nginx/1.25.3", RPM_BUILD_FILES + RPM_ARCHIVE_FILES, mtime=1000)
        async with await run_with_client(root) as client:
            paths = [
                "rpm/nginx/1.25.3/nginx-rpm-result.md",
                "deploy/nginx/1.25/nginx-install.md",
                "rpm/nginx/1.25.3/nginx-rpm-result.md",   # 重复项去重
                "rpm/nginx/1.25.3/nginx-absent.md",       # 缺失如实跳过
                "deploy/../scope.yaml",                   # 越界跳过
                "guides/nginx/install-guide.md",          # 未注册根跳过
            ]
            r = await client.post("/api/artifacts/zip", json={"paths": paths})
            assert r.status_code == 200, r.text
            assert r.headers.get("content-type") == "application/zip"
            assert "attachment" in r.headers.get("content-disposition", "")
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            # zip 内保留根前缀目录树；顺序按给定（去重/跳过后）
            assert zf.namelist() == [
                "rpm/nginx/1.25.3/nginx-rpm-result.md",
                "deploy/nginx/1.25/nginx-install.md",
            ], zf.namelist()
            assert zf.read("rpm/nginx/1.25.3/nginx-rpm-result.md") == (
                rpm / "nginx/1.25.3/nginx-rpm-result.md"
            ).read_bytes()
            assert zf.read("deploy/nginx/1.25/nginx-install.md") == (
                deploy / "nginx/1.25/nginx-install.md"
            ).read_bytes()


async def test_zip_rejects_bad_requests():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "deploy").mkdir()
        (root / "rpm").mkdir()
        async with await run_with_client(root) as client:
            # 非法请求体：缺 paths / 空列表 / 非字符串项 → 422
            for body in (None, {}, {"paths": []}, {"paths": [1]}, {"paths": [""]}):
                r = await client.post("/api/artifacts/zip", json=body)
                assert r.status_code == 422, (body, r.text)
            # 合法请求但一个有效产物都没有 → 404
            r = await client.post("/api/artifacts/zip", json={"paths": ["deploy/absent.md"]})
            assert r.status_code == 404, r.text


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
