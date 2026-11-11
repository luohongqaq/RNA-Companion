"""模型设置的持久化、密钥保护与配置优先级。"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from api.app import create_app
from module.model_settings.core.processing.service import ModelSettingsService
from storage.config.settings import Settings
from storage.sqlite.db_session import SQLiteDatabase
from module.model_settings.repository.dotenv import DotenvModelSettingsRepository


@pytest.mark.asyncio
async def test_model_settings_preserve_env_and_handle_concurrency(tmp_path, monkeypatch):
    for key in ('RNA_REPLY_BASE_URL', 'RNA_REPLY_MODEL', 'RNA_REPLY_API_KEY',
                'DEFAULT_BASE_URL', 'DEFAULT_MODEL', 'DEFAULT_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / '.env'
    env_path.write_text("# 注释\nRNA_REPLY_BASE_URL=https://old.example/v1\nRNA_REPLY_MODEL=old\n"
                        "RNA_REPLY_API_KEY=old-secret\nOTHER_VALUE=keep\n\n", encoding='utf-8')
    database = SQLiteDatabase(tmp_path / 'config.sqlite')
    await database.initialize()
    try:
        active = Settings(_env_file=env_path)
        repo = DotenvModelSettingsRepository(database, env_path=env_path, active_settings=active)
        app = create_app()
        app.state.container = SimpleNamespace(model_settings_service=ModelSettingsService(repo))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            url = '/api/v1/model-settings/reply'
            before = await client.get(url)
            assert before.status_code == 200 and before.headers['cache-control'] == 'no-store'
            assert before.json()['base_url'] == 'https://old.example/v1'
            assert before.json()['api_key_configured'] is True
            assert 'old-secret' not in before.text
            assert before.json()['restart_required'] is False
            secret = "new-secret'\\with#symbols"
            payload = {'version': before.json()['version'], 'base_url': 'https://new.example/v1',
                       'model': 'new-model', 'api_key': secret}
            result = await client.patch(url, json=payload)
            assert result.status_code == 200, result.text
            assert secret not in result.text and 'old-secret' not in result.text
            assert result.json()['restart_required'] is True
            contents = env_path.read_text(encoding='utf-8')
            assert '# 注释\n' in contents and 'OTHER_VALUE=keep\n\n' in contents
            loaded = Settings(_env_file=env_path)
            assert loaded.reply_api_key.get_secret_value() == secret
            assert loaded.reply_base_url == 'https://new.example/v1'
            assert (await client.patch(url, json=payload)).status_code == 409
            version = result.json()['version']
            calls = [client.patch(url, json={'version': version, 'model': f'model-{i}'}) for i in range(2)]
            assert sorted(r.status_code for r in await asyncio.gather(*calls)) == [200, 409]
            current = (await client.get(url)).json()
            cleared = await client.patch(url, json={'version': current['version'], 'api_key': None})
            assert cleared.status_code == 200 and cleared.json()['api_key_configured'] is False
            assert (await client.patch(url, json={'version': cleared.json()['version'], 'base_url': 'https://u:p@example.com'})).status_code == 422
            assert (await client.patch(url, json={'version': cleared.json()['version'], 'api_key': 'abc\nX=bad'})).status_code == 422
            assert (await client.get('/api/v1/model-settings/unknown')).status_code == 422
            default = (await client.get('/api/v1/model-settings/default')).json()
            assert default['purpose'] == 'default' and default['model']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_model_settings_environment_overrides_saved_values(tmp_path, monkeypatch):
    monkeypatch.setenv('RNA_REPLY_MODEL', 'environment-model')
    env_path = tmp_path / '.env'
    env_path.write_text('rna_reply_model=saved-model\n', encoding='utf-8-sig')
    repo = DotenvModelSettingsRepository(None, env_path=env_path, active_settings=Settings(_env_file=env_path))
    result = await repo.get_model_settings('reply')
    assert result.model == 'saved-model'
    assert result.effective_model == 'environment-model'
    assert 'model' in result.overridden_fields
    assert result.restart_required is False


@pytest.mark.asyncio
async def test_replace_failure_keeps_configuration(tmp_path, monkeypatch):
    env_path = tmp_path / '.env'
    env_path.write_text('RNA_REPLY_MODEL=before\n', encoding='utf-8')
    database = SQLiteDatabase(tmp_path / 'failure.sqlite')
    await database.initialize()
    try:
        repo = DotenvModelSettingsRepository(database, env_path=env_path, active_settings=Settings(_env_file=env_path))
        app = create_app()
        app.state.container = SimpleNamespace(model_settings_service=ModelSettingsService(repo))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            url = '/api/v1/model-settings/reply'
            version = (await client.get(url)).json()['version']

            def fail_replace(source, target):
                raise OSError('test failure')

            monkeypatch.setattr('module.model_settings.repository.dotenv.os.replace', fail_replace)
            response = await client.patch(url, json={'version': version, 'model': 'after'})
            assert response.status_code == 409
            assert env_path.read_text(encoding='utf-8') == 'RNA_REPLY_MODEL=before\n'
            assert list(tmp_path.glob('.model-settings-*')) == []
    finally:
        await database.close()
