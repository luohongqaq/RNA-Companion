-- 结构化正文是正式内容；取消 Markdown 文件及任务副本。
ALTER TABLE daily_memories DROP COLUMN markdown_path;
ALTER TABLE topic_memories DROP COLUMN markdown_path;
UPDATE memory_jobs SET payload_json = json_set(
    json_remove(payload_json, '$.exported_artifact', '$.exported_artifacts',
                '$.write_result.markdown', '$.vector_document.daily_memory'),
    '$.schema_version', 5
) WHERE json_extract(payload_json, '$.schema_version') = 4;
UPDATE memory_jobs SET payload_json = json_set(payload_json, '$.write_results', json((
    SELECT json_group_array(json_remove(value, '$.write.markdown'))
    FROM json_each(memory_jobs.payload_json, '$.write_results')
))) WHERE json_type(payload_json, '$.write_results') = 'array';
