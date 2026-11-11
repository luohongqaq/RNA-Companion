"""组装便携版发行 zip：exe 产物 + git 跟踪的 backend 文件 + 首次使用物料。

用法（先在 frontend/ 跑 npm run electron:dist 产出 exe）：
    python scripts/assemble_release.py [--version 0.1.0]

后端文件按 git 跟踪清单复制：凡是 gitignore 的内容（.venv、模型、记忆库、
本地角色、语音包、内部脚本）天然不会进入发行包。
"""
import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Windows 上控制台默认不是 UTF-8（GitHub runner 是 cp1252），
# 直接 print 中文会抛 UnicodeEncodeError。这里统一按 UTF-8 输出，
# 无法编码的字符降级替换，避免"打包成功但脚本崩溃"。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def tracked_backend_files() -> list[str]:
    """git 跟踪的 backend/ 文件清单——发行内容与仓库快照严格一致。

    用 -z 输出（NUL 分隔、不做路径转义），中文路径原样保留。
    未跟踪的本地个人角色天然不会进入发行包。
    """
    result = subprocess.run(["git", "ls-files", "-z", "backend"], cwd=REPO_ROOT,
                            capture_output=True, check=True)
    return [line for line in result.stdout.decode("utf-8").split("\0") if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="发行版本号；缺省读 frontend/package.json 的 version")
    args = parser.parse_args()

    version = args.version or json.loads(
        (REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    version = version.removeprefix("v")

    unpacked = REPO_ROOT / "frontend" / "release-build" / "win-unpacked"
    if not unpacked.is_dir():
        raise SystemExit("未找到 exe 产物，请先在 frontend/ 执行: npm run electron:dist")

    stage = REPO_ROOT / "dist" / f"RNA-Companion-{version}-portable-win64"
    if stage.exists():
        shutil.rmtree(stage)

    # 1) Electron 产物（exe + 界面 + 运行时）
    shutil.copytree(unpacked, stage)

    # 2) backend/：只复制 git 跟踪的文件，结构原样保留
    for relative in tracked_backend_files():
        target = stage / "backend" / relative.removeprefix("backend/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, target)

    # 3) 首次使用物料
    for name in ("安装依赖.bat", "下载模型.bat", "首次使用说明.txt"):
        shutil.copy2(REPO_ROOT / name, stage / name)

    # 4) 打 zip（不能 with_suffix：版本号里的 .0 会被误当作扩展名）
    zip_path = stage.parent / f"{stage.name}.zip"
    with zipfile.ZipFile(zip_path, "w", compresslevel=6) as bundle:
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                bundle.write(file, file.relative_to(stage.parent))

    print(f"发行目录: {stage}")
    print(f"发行包  : {zip_path}  ({zip_path.stat().st_size / 1048576:.1f} MB)")
    print("发布：把 zip 上传到 GitHub Release（或直接 git tag 推送触发 Actions）")


if __name__ == "__main__":
    main()
