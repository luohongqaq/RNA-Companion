"""角色信息、资源清单和表情匹配接口。"""
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from api.dependencies import get_character_service
from api.schemas.character import (
    CharacterAssetsResponse, CharacterDetailResponse, CharacterListResponse, CharacterResponse,
    ExpressionSelectionResponse, ExpressionAssetResponse, BackgroundResponse, OutfitResponse,
    ExpressionMatchRequest, ExpressionMatchResponse,
    CharacterPromptResponse, CharacterPromptUpdate,
)
from module.character.core.processing.service import CharacterService
from module.character.core.domain.resources import ExpressionImage
from module.character.core.domain.prompts import PromptType

router = APIRouter(prefix="/characters", tags=["characters"])
# Depends 在每次请求中从应用容器获取角色服务并传给路由函数。
Service = Annotated[CharacterService, Depends(get_character_service)]


def _build_asset_url(request: Request, route: str, character_id: str, file: str) -> str:
    """按路由名称和参数生成图片 URL 路径；不读取图片，也不发送请求。"""
    # 清单存相对文件路径；URL 路由只需要文件名。.path 去掉协议、主机和端口。
    return request.url_for(route, character_id=character_id, file_name=PurePosixPath(file).name).path


def _build_expression_response(request: Request, character_id: str, item: ExpressionImage) -> ExpressionAssetResponse:
    """将内部立绘记录转成前端可用的响应，隐藏磁盘路径并提供 URL。"""
    return ExpressionAssetResponse(
        asset_id=item.asset_id, outfit_id=item.outfit_id, pose_id=item.pose_id,
        expression_id=item.expression_id,
        url=_build_asset_url(request, "get_character_expression", character_id, item.file),
    )


@router.get("", response_model=CharacterListResponse)
async def list_characters(service: Service) -> CharacterListResponse:
    """返回角色列表，只包含对前端公开的 ID 和名称。"""
    return CharacterListResponse(items=[CharacterResponse(character_id=x.character_id, name=x.name)
                                        for x in await service.list()])


@router.get("/{character_id}", response_model=CharacterDetailResponse)
async def get_character_by_id(character_id: str, request: Request, service: Service) -> CharacterDetailResponse:
    """查询角色详情，并为清单中指定的默认背景、立绘生成 URL。"""
    character = await service.get(character_id)
    resources = await service.get_resources(character_id)
    background = next(x for x in resources.backgrounds.items if x.id == resources.backgrounds.default_id)
    default = resources.expression.default
    expression = next(x for x in resources.expression.assets if x.enabled
                      and (x.outfit_id, x.pose_id, x.expression_id)
                      == (default.outfit_id, default.pose_id, default.expression_id))
    return CharacterDetailResponse(
        character_id=character.character_id, name=character.name,
        default_background_url=_build_asset_url(request, "get_character_background", character_id, background.file),
        default_expression_url=_build_asset_url(request, "get_character_expression", character_id, expression.file),
    )


@router.get("/{character_id}/assets", response_model=CharacterAssetsResponse)
async def list_character_assets(character_id: str, request: Request, service: Service) -> CharacterAssetsResponse:
    """返回前端选择背景、服装和展示立绘所需的映射及资源 URL。"""
    manifest = await service.get_resources(character_id)
    catalog = manifest.expression
    return CharacterAssetsResponse(
        character_id=character_id, default_background_id=manifest.backgrounds.default_id,
        backgrounds=[BackgroundResponse(id=x.id, name=x.name,
                                   url=_build_asset_url(request, "get_character_background", character_id, x.file))
                     for x in manifest.backgrounds.items],
        # 清单记录内部路径；响应模型只组装前端需要的三个默认 ID。
        default_expression=ExpressionSelectionResponse(
            outfit_id=catalog.default.outfit_id,
            pose_id=catalog.default.pose_id,
            expression_id=catalog.default.expression_id,
        ),
        outfits={key: OutfitResponse(name=x.name, default_pose_id=x.default_pose_id,
                                    default_expression_id=x.default_expression_id)
                 for key, x in catalog.outfits.items()},
        poses={key: pose.name for key, pose in catalog.poses.items()},
        # 只向前端公布已启用的表情和图片，清单内的文件路径不会直接返回。
        expressions={key: value.name for key, value in catalog.expressions.items() if value.enabled},
        assets=[_build_expression_response(request, character_id, x) for x in catalog.assets if x.enabled],
    )


@router.get("/{character_id}/prompts/{prompt_type}", response_model=CharacterPromptResponse)
async def get_character_prompt(character_id: str, prompt_type: PromptType,
                               service: Service) -> CharacterPromptResponse:
    """获取一份提示词和版本；prompt_type 只接受三种固定名称。"""
    return CharacterPromptResponse.model_validate(await service.get_prompt(character_id, prompt_type))


@router.put("/{character_id}/prompts/{prompt_type}", response_model=CharacterPromptResponse)
async def update_character_prompt(character_id: str, prompt_type: PromptType,
                                  payload: CharacterPromptUpdate, service: Service) -> CharacterPromptResponse:
    """版本一致时替换提示词；版本过期返回 409。"""
    result = await service.save_prompt(character_id, prompt_type, payload.content, payload.version)
    return CharacterPromptResponse.model_validate(result)


@router.post("/{character_id}/expression/resolve", response_model=ExpressionMatchResponse)
async def resolve_character_expression(
    character_id: str, payload: ExpressionMatchRequest, request: Request, service: Service,
) -> ExpressionMatchResponse:
    """把服装 ID 和模型输出的中文名称解析为实际立绘及 URL。"""
    # model_dump 将请求模型转为字典，** 再按名称传给业务方法。
    result = await service.resolve_expression(character_id, **payload.model_dump())
    return ExpressionMatchResponse(request=payload, image=_build_expression_response(request, character_id, result.asset),
                                     fallback_used=result.fallback_used)


@router.get("/{character_id}/assets/expression/{file_name}", name="get_character_expression")
async def get_character_expression(character_id: str, file_name: str, service: Service) -> Response:
    """返回已登记、启用的立绘字节，浏览器按 image/png 解释内容。"""
    return Response(content=await service.read_asset(character_id, kind="expression", file_name=file_name),
                    media_type="image/png")


@router.get("/{character_id}/assets/background/{file_name}", name="get_character_background")
async def get_character_background(character_id: str, file_name: str, service: Service) -> Response:
    """返回清单登记的背景图片字节。"""
    return Response(content=await service.read_asset(character_id, kind="background", file_name=file_name),
                    media_type="image/png")
