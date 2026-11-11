"""把角色卡文件渲染成发给模型的正文：替换称呼变量，重新生成视觉允许列表。

角色卡的"视觉表达"小节里，姿势和表情的允许列表是 manifest.json 的派生数据：
manifest 才是事实来源（加载器校验它、立绘匹配走它），卡片里手抄一份必然漂移。
因此卡片中用标记注释包住这两段清单，读取时按 manifest 现值重新生成；
清单之外的正文（规则说明、示例、锚点）全部保持手写，不经过模板。
称呼 {{ user_name }} 是唯一的模板变量，取自 characters 表的称呼字段。
"""
import json
import re

from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import TemplateError

from module.shared.character.types.manifest import CharacterResourceManifest

POSES_BEGIN = "{# poses:begin 以下清单由 manifest.json 生成，修改请编辑 manifest #}"
POSES_END = "{# poses:end #}"
EXPRESSIONS_BEGIN = "{# expressions:begin 以下清单由 manifest.json 生成，只列启用的表情；修改请编辑 manifest #}"
EXPRESSIONS_END = "{# expressions:end #}"


class CharacterCardRenderer:
    """将角色卡模板渲染为模型使用的正文。"""

    def __init__(self) -> None:
        self._templates = Environment(undefined=StrictUndefined, autoescape=False,
                                      keep_trailing_newline=True)
        self._templates.filters["json_string"] = self._json_string

    @staticmethod
    def _json_string(value: str) -> str:
        """转义 JSON 字符串的内容，保留模板已有的外层引号。"""
        return json.dumps(value, ensure_ascii=False)[1:-1]

    @staticmethod
    def _normalize_markers(text: str) -> str:
        for marker in (POSES_BEGIN, POSES_END, EXPRESSIONS_BEGIN, EXPRESSIONS_END):
            text = text.replace("<!--" + marker[2:-2] + "-->", marker)
        return text

    def render(self, text: str, manifest: CharacterResourceManifest | None, user_appellation: str) -> str:
        """渲染角色卡：先按 manifest 重生成两段清单，再做称呼变量替换。

        manifest 为 None（卡片没带生成标记）时跳过清单重生成，只做变量替换。
        """
        rendered = self._normalize_markers(text)
        if manifest is not None:
            catalog = manifest.expression
            # 只列"至少有一张启用立绘"的姿势和启用表情，保证卡片允许的都渲染得出来。
            pose_items = [(pose.name, pose.description) for pose in catalog.available_poses()]
            expression_items = [(item.name, item.description) for item in catalog.available_expressions()]
            rendered = self._replace_block(rendered, POSES_BEGIN, POSES_END, self._bullet_lines(pose_items))
            rendered = self._replace_block(rendered, EXPRESSIONS_BEGIN, EXPRESSIONS_END, self._bullet_lines(expression_items))
        try:
            # StrictUndefined：卡片里出现未声明的 {{ 变量 }} 时直接报错，而不是悄悄渲染成空串。
            template = self._templates.from_string(rendered)
            return template.render(user_name=user_appellation)
        except TemplateError as exc:
            raise ValueError(f"角色卡模板渲染失败: {exc}") from exc

    @staticmethod
    def needs_manifest(text: str) -> bool:
        """卡片里带生成标记时才要求 manifest；纯手写卡片不依赖清单也能读取。"""
        text = CharacterCardRenderer._normalize_markers(text)
        return POSES_BEGIN in text or EXPRESSIONS_BEGIN in text

    @staticmethod
    def _bullet_lines(items: list[tuple[str, str]]) -> str:
        """把（名称，说明）列表排版成卡片现有的条目样式：分号分隔、末条句号。"""
        if not items:
            return ""
        lines = [f"- `{name}`" + (f"：{description}" if description else "") for name, description in items]
        return "；\n".join(lines[:-1]) + ("\n" + lines[-1] + "。" if len(lines) > 1 else lines[0] + "。")

    @staticmethod
    def _replace_block(text: str, begin: str, end: str, body: str) -> str:
        """替换 begin..end 标记之间的内容；标记不存在时原样返回（清单段是可选的）。"""
        pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
        return pattern.sub(lambda _: body, text, count=1)


if __name__ == "__main__":
    from pathlib import Path

    # 演示：渲染 resources/characters 下按路径排序的第一个角色
    root = Path(__file__).resolve().parents[3]
    manifests = sorted((root / "resources" / "characters").glob("*/manifest.json"))
    if not manifests:
        raise SystemExit("resources/characters 下没有角色")
    character_dir = manifests[0].parent
    card = (character_dir / "角色卡.md").read_text(encoding="utf-8-sig")
    manifest = None
    if CharacterCardRenderer.needs_manifest(card):
        manifest = CharacterResourceManifest.model_validate_json(
            (character_dir / "manifest.json").read_bytes()
        )
    import json
    appellation = json.loads((character_dir / "manifest.json").read_text(encoding="utf-8-sig"))["user_appellation"]
    print(CharacterCardRenderer().render(card, manifest, user_appellation=appellation), end="")

