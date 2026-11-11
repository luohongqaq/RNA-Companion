"""下载 RNA Companion 运行所需的本地模型到 backend/models/。

用法(任意目录执行均可,路径以脚本自身位置定位):

    backend/.venv/Scripts/python backend/scripts/download_models.py
    backend/.venv/Scripts/python backend/scripts/download_models.py --source hf
    backend/.venv/Scripts/python backend/scripts/download_models.py --only reranker

默认从 ModelScope 下载(国内网络可直连);--source hf 改用 HuggingFace,
可配合环境变量 HF_ENDPOINT=https://hf-mirror.com 走镜像加速。

模型清单:
  bge-m3    BAAI/bge-m3              记忆编码与场景召回,约 2.3 GB
  reranker  Qwen/Qwen3-Reranker-0.6B  记忆重排,约 1.2 GB
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MODELS_ROOT = BACKEND_ROOT / "models"

MODELS = {
    "bge-m3": {
        "repo_id": "BAAI/bge-m3",
        "dest": MODELS_ROOT / "bge-m3",
        "size": "约 2.3 GB",
        "purpose": "记忆编码与场景召回",
    },
    "reranker": {
        "repo_id": "Qwen/Qwen3-Reranker-0.6B",
        "dest": MODELS_ROOT / "qwen3-reranker-0.6b",
        "size": "约 1.2 GB",
        "purpose": "记忆重排",
    },
}


# 运行时只用 transformers / FlagEmbedding 加载 pytorch 或 safetensors 权重，
# onnx / h5 / tflite 等导出版本用不到，跳过可少下载约 2 GB。
IGNORE_PATTERNS = ["onnx/*", "*.onnx", "*.onnx_data", "*.h5", "*.msgpack", "*.ot", "*.tflite"]


def download(repo_id: str, dest: Path, source: str) -> None:
    if source == "modelscope":
        try:
            from modelscope import snapshot_download
        except ImportError:
            sys.exit(
                "缺少 modelscope 库,请先安装:\n"
                "  backend/.venv/Scripts/pip install modelscope"
            )
        snapshot_download(repo_id, local_dir=str(dest), ignore_patterns=IGNORE_PATTERNS)
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            sys.exit(
                "缺少 huggingface_hub 库,请先安装:\n"
                "  backend/.venv/Scripts/pip install huggingface_hub"
            )
        snapshot_download(repo_id, local_dir=str(dest), ignore_patterns=IGNORE_PATTERNS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source", choices=("modelscope", "hf"), default="modelscope",
        help="下载来源,默认 modelscope",
    )
    parser.add_argument(
        "--only", choices=tuple(MODELS),
        help="只下载指定模型,缺省两个都下载",
    )
    args = parser.parse_args()

    targets = [args.only] if args.only else list(MODELS)
    for name in targets:
        item = MODELS[name]
        print(f"[{name}] {item['purpose']}")
        print(f"  仓库: {item['repo_id']}")
        print(f"  目标: {item['dest']}  大小: {item['size']}")
        download(item["repo_id"], item["dest"], args.source)
        print(f"  完成")

    print(
        "\n模型就绪。可选配置见 backend/.env.example;"
        "启用场景召回前请先构建索引:"
        " cd backend && .venv/Scripts/python -m api.cli.seed_scenes"
    )


if __name__ == "__main__":
    main()
