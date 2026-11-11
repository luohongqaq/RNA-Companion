"""模型设置的存储实现。"""
from __future__ import annotations
import asyncio
import hashlib
import io
import os
import tempfile
from module.shared.common.errors.base import InvalidStateError
from module.model_settings.core.domain.models import ModelPurpose, ModelSettings, ModelSettingsUpdate
from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pathlib import Path
from pydantic import ValidationError
from storage.config.settings import Settings
from storage.sqlite.db_session import SQLiteDatabase


class DotenvModelSettingsRepository:
    """保存 .env，并用 SQLite 写事务串行化版本比较和文件替换。"""

    def __init__(self, database: SQLiteDatabase, *,
                 env_path: Path | None = None, active_settings: Settings | None = None) -> None:
        self._database = database
        self._env_path = (env_path or Path('.env')).resolve()
        self._active_settings = active_settings

    @staticmethod
    def _model_keys(purpose: ModelPurpose) -> dict[str, str]:
        if purpose == 'reply':
            return {'base_url': 'RNA_REPLY_BASE_URL', 'model': 'RNA_REPLY_MODEL', 'api_key': 'RNA_REPLY_API_KEY'}
        if purpose == 'default':
            return {'base_url': 'DEFAULT_BASE_URL', 'model': 'DEFAULT_MODEL', 'api_key': 'DEFAULT_API_KEY'}
        raise InvalidStateError('模型配置类型无效')

    @staticmethod
    def _effective_model(settings: Settings, purpose: ModelPurpose) -> tuple[str, str, str | None]:
        if purpose == 'reply':
            key = settings.reply_api_key
            return settings.reply_base_url, settings.reply_model, key.get_secret_value() if key else None
        key = settings.default_api_key
        return settings.default_base_url, settings.default_model, key.get_secret_value() if key else None

    def _read_model_settings(self, purpose: ModelPurpose) -> ModelSettings:
        keys = self._model_keys(purpose)
        try:
            raw = self._env_path.read_bytes() if self._env_path.exists() else b''
            contents = raw.decode('utf-8-sig')
            bindings = list(parse_stream(io.StringIO(contents)))
            if any(binding.error for binding in bindings):
                raise ValueError('invalid dotenv file')
            # Settings 默认大小写不敏感，查询保存值时也按相同规则识别键名。
            values = {key.upper(): value for key, value in dotenv_values(stream=io.StringIO(contents)).items()}
            # .env 字段不存在时使用 Settings 声明的默认值，不读取进程环境作为保存值。
            prefix = 'reply' if purpose == 'reply' else 'default'
            default_url = Settings.model_fields[f'{prefix}_base_url'].default
            default_model = Settings.model_fields[f'{prefix}_model'].default
            saved_url = values.get(keys['base_url'])
            saved_model = values.get(keys['model'])
            saved_key = values.get(keys['api_key'])
            effective_settings = Settings(_env_file=self._env_path)
            effective = self._effective_model(effective_settings, purpose)
            active = self._effective_model(self._active_settings, purpose) if self._active_settings else effective
            env_keys = {key.upper() for key in os.environ}
            overridden = tuple(name for name, key in keys.items() if key.upper() in env_keys)
            return ModelSettings(
                purpose=purpose, base_url=default_url if saved_url is None else saved_url,
                model=default_model if saved_model is None else saved_model,
                api_key_configured=bool(saved_key), effective_base_url=effective[0], effective_model=effective[1],
                effective_api_key_configured=bool(effective[2]), overridden_fields=overridden,
                restart_required=effective != active, version=hashlib.sha256(raw).hexdigest(),
            )
        except (OSError, UnicodeError, ValueError, ValidationError) as exc:
            raise InvalidStateError('无法读取模型配置文件') from exc

    async def get_model_settings(self, purpose: ModelPurpose) -> ModelSettings:
        return await asyncio.to_thread(self._read_model_settings, purpose)

    async def update_model_settings(self, purpose: ModelPurpose, update: ModelSettingsUpdate) -> ModelSettings:
        async with self._database.transaction() as session:
            # 显式取得连接以触发 BEGIN IMMEDIATE，而不是只创建尚未开始的事务。
            await session.connection()
            task = asyncio.create_task(asyncio.to_thread(self._write_model_settings, purpose, update))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def _write_model_settings(self, purpose: ModelPurpose, update: ModelSettingsUpdate) -> ModelSettings:
        current = self._read_model_settings(purpose)
        if current.version != update.version:
            raise InvalidStateError('配置文件已被修改，请重新读取后保存')
        keys = self._model_keys(purpose)
        updates = {keys[name]: value for name, value in update.changes.items()}
        temporary = None
        try:
            raw = self._env_path.read_bytes() if self._env_path.exists() else b''
            contents = raw.decode('utf-8-sig')
            bindings = list(parse_stream(io.StringIO(contents)))
            if any(binding.error for binding in bindings):
                raise ValueError('invalid dotenv file')
            # 只改目标项；保留其他原始行、注释和空行。单引号与反斜杠按 dotenv 规则转义。
            def format_line(key: str, value: str | None) -> str:
                escaped = (value or '').replace('\\', '\\\\').replace("'", "\\'")
                return f"{key}='{escaped}'\n"

            lines, found = [], set()
            by_name = {key.upper(): key for key in updates}
            for binding in bindings:
                key = by_name.get(binding.key.upper()) if binding.key else None
                if key:
                    lines.append(format_line(key, updates[key]))
                    found.add(key)
                else:
                    lines.append(binding.original.string)
            if lines and not lines[-1].endswith(('\n', '\r')):
                lines[-1] += '\n'
            lines.extend(format_line(key, value) for key, value in updates.items() if key not in found)
            with tempfile.NamedTemporaryFile(mode='wb', prefix='.model-settings-', dir=self._env_path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                if self._env_path.exists():
                    os.chmod(temporary, self._env_path.stat().st_mode & 0o777)
                # 原文件若带 UTF-8 BOM，更新时继续保留。
                prefix = b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b''
                handle.write(prefix + ''.join(lines).encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._env_path)
            temporary = None
            return self._read_model_settings(purpose)
        except (OSError, UnicodeError, ValueError) as exc:
            raise InvalidStateError('无法保存模型配置文件') from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
