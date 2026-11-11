"""将 daily 意向分区改名，保留正文字符串并同步内容 hash 和引用。"""
import hashlib
import json


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def rename(value, hashes):
    if isinstance(value, list):
        return [rename(item, hashes) for item in value]
    if not isinstance(value, dict):
        return value
    result = {("intentions" if key == "open_loops" else key): rename(item, hashes)
              for key, item in value.items()}
    if "open_loops" in value and "memory_date" in value and "memorable_moments" in value:
        hashes[digest(value)] = digest(result)
    document = value.get("document")
    if isinstance(document, dict) and "open_loops" in document and "content_hash" in value:
        hashes[value["content_hash"]] = digest(result["document"])
    # 审核字段及召回分区是协议中的名字，正文 text/statement 等字符串保持不变。
    for key in ("field", "section_key"):
        if result.get(key) == "open_loops":
            result[key] = "intentions"
    if isinstance(result.get("section_keys"), list):
        result["section_keys"] = ["intentions" if item == "open_loops" else item for item in result["section_keys"]]
    if result.get("job_family") in ("daily", "object", "topic") and result.get("schema_version") == 5:
        result["schema_version"] = 6
    return result


def update_refs(value, hashes):
    if isinstance(value, list):
        return [update_refs(item, hashes) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: update_refs(item, hashes) for key, item in value.items()}
    for key, item in result.items():
        if isinstance(item, str) and (key.endswith("hash") or key == "vector_document_key"):
            if item in hashes:
                result[key] = hashes[item]
            elif key == "vector_document_key":
                prefix, separator, old_hash = item.rpartition(":")
                if separator and old_hash in hashes:
                    result[key] = prefix + separator + hashes[old_hash]
    return result


async def migrate(connection):
    hashes, pending = {}, []
    tables = (await connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")).scalars().all()
    for table in tables:
        columns = (await connection.exec_driver_sql(f'PRAGMA table_info("{table}")')).all()
        json_columns = [row[1] for row in columns if row[1].endswith("_json")]
        for column in json_columns:
            rows = (await connection.exec_driver_sql(f'SELECT rowid,"{column}" FROM "{table}" WHERE "{column}" IS NOT NULL')).all()
            for rowid, raw in rows:
                data = json.loads(raw)
                converted = rename(data, hashes)
                pending.append((table, column, rowid, raw, converted))
                if table == "daily_memories" and column == "document_json" and "open_loops" in data:
                    old_hash = (await connection.exec_driver_sql('SELECT content_hash FROM daily_memories WHERE rowid=?', (rowid,))).scalar_one()
                    hashes[old_hash] = digest(converted)
    for table, column, rowid, raw, value in pending:
        converted = update_refs(value, hashes)
        if converted != json.loads(raw):
            await connection.exec_driver_sql(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?',
                (json.dumps(converted, ensure_ascii=False), rowid))
    # 独立列中的版本引用与 JSON 中的引用保持一致。触发器同步召回更新信号。
    for table in tables:
        columns = (await connection.exec_driver_sql(f'PRAGMA table_info("{table}")')).all()
        for row in columns:
            column = row[1]
            if column.endswith("hash"):
                for old, new in hashes.items():
                    await connection.exec_driver_sql(f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?', (new, old))
            elif column == "vector_document_key":
                rows = (await connection.exec_driver_sql(f'SELECT rowid,"{column}" FROM "{table}"')).all()
                for rowid, value in rows:
                    converted = update_refs({column: value}, hashes)[column]
                    if converted != value:
                        await connection.exec_driver_sql(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (converted, rowid))
