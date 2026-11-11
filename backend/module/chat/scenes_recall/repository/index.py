from __future__ import annotations
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import uuid
from qdrant_client import AsyncQdrantClient, models
from module.chat.scenes_recall.repository.schema import SCENE_COLLECTION, SCENE_SCHEMA


class QdrantSceneIndex:
    """场景示例索引；与正式长期记忆的分区读取分离。"""

    @staticmethod
    def scene_line(role: str, text: str) -> str:
        # 入库与检索两侧共用同一渲染；改名需重建场景索引。
        name = "角色" if role in ("assistant", "character") else "用户" if role == "user" else role
        return f"{name}：{text}"

    @staticmethod
    def load_scenes(directory: Path, character_id: str) -> list[dict]:
        rows, seen, skipped = [], set(), 0
        for path in sorted(directory.glob("*.jsonl")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("role") not in ("assistant", "character"):
                    continue  # 只索引角色台词行；旁白、用户行留在 context 里。
                key = f"{item['session_id']}#{item['idx']}"
                if key in seen:
                    raise ValueError(f"重复场景：{key}")
                seen.add(key)
                context = [QdrantSceneIndex.scene_line(part["role"], part["text"]) for part in item["context"]]
                reply = item["text"].strip("「」")
                if not reply.strip() or not context:
                    skipped += 1
                    continue  # 无可检索情境或台词的原文保留在文件，不制造数据。
                payload = {"scene_id": key, "session_id": item["session_id"], "context": context,
                           "reply": reply, "expression_code": item["expr"], "pose_code": item["pose"],
                           "character_id": character_id, "source_file": path.name, "schema": SCENE_SCHEMA}
                payload["content_hash"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                payload["query_text"] = "\n".join(context[-4:])
                rows.append(payload)
        if not rows:
            raise ValueError("没有可入库的场景示例")
        if skipped:
            logging.getLogger(__name__).warning("跳过%d条缺少情境或台词的场景记录；原文件完整保留", skipped)
        return rows

    @staticmethod
    def encode_scenes(texts: list[str], encoder) -> list[list[float]]:
        # 与原脚本一致：BGE-M3 CLS/归一化，最大长度384，只编码回复前最近四条情境。
        result = encoder.encode(texts, batch_size=16, max_length=384,
                                return_dense=True, return_sparse=False, return_colbert_vecs=False)
        return [vector.tolist() for vector in result["dense_vecs"]]

    @staticmethod
    async def import_scenes(client: AsyncQdrantClient, rows: list[dict], encoder) -> dict:
        exists = await client.collection_exists(SCENE_COLLECTION)
        identifiers = [str(uuid.uuid5(uuid.NAMESPACE_URL, row["character_id"] + "|" + row["scene_id"])) for row in rows]
        current = {}
        if exists:
            for start in range(0, len(rows), 128):
                points = await client.retrieve(SCENE_COLLECTION, ids=identifiers[start:start+128], with_payload=True)
                current.update({str(point.id): point.payload for point in points})
        changed = [(identifier, row) for identifier, row in zip(identifiers, rows, strict=True)
                   if current.get(identifier, {}).get("content_hash") != row["content_hash"]]
        for start in range(0, len(changed), 32):
            batch = changed[start:start+32]
            vectors = await asyncio.to_thread(QdrantSceneIndex.encode_scenes, [row["query_text"] for _, row in batch], encoder)
            if not exists:
                await client.create_collection(SCENE_COLLECTION, vectors_config=models.VectorParams(
                    size=len(vectors[0]), distance=models.Distance.COSINE))
                exists = True
            await client.upsert(SCENE_COLLECTION, points=[models.PointStruct(id=identifier, vector=vector, payload=row)
                for (identifier, row), vector in zip(batch, vectors, strict=True)], wait=True)
            print(f"场景向量写入：{min(start+32, len(changed))}/{len(changed)}", flush=True)
        return {"total": len(rows), "indexed": len(changed), "unchanged": len(rows)-len(changed), "collection": SCENE_COLLECTION}
