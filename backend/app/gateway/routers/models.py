"""AI 模型列表查询路由。

本模块提供可用 AI 模型信息的查询接口。模型配置在应用配置文件中定义，
本路由仅做只读展示，不涉及模型的创建或修改。

核心功能：
- 列出所有已配置的 AI 模型及其元数据
- 按名称查询单个模型的详细信息

数据安全：
- 响应中排除敏感字段（如 API 密钥和内部配置）
- 仅返回前端展示所需的模型元数据

返回信息包括：
- 模型名称和显示名称
- 模型描述
- 是否支持思考模式（thinking）
- 是否支持推理力度调节（reasoning_effort）
- 令牌用量显示配置

路由前缀：/api
标签：models
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.gateway.deps import get_config
from deerflow.config.app_config import AppConfig

router = APIRouter(prefix="/api", tags=["models"])


class ModelResponse(BaseModel):
    """单个 AI 模型的响应模型。

    Attributes:
        name: 模型的唯一标识符。
        model: 实际的提供商模型标识符。
        display_name: 面向用户的显示名称。
        description: 模型描述。
        supports_thinking: 是否支持思考模式。
        supports_reasoning_effort: 是否支持推理力度调节。
    """

    name: str = Field(..., description="Unique identifier for the model")
    model: str = Field(..., description="Actual provider model identifier")
    display_name: str | None = Field(None, description="Human-readable name")
    description: str | None = Field(None, description="Model description")
    supports_thinking: bool = Field(default=False, description="Whether model supports thinking mode")
    supports_reasoning_effort: bool = Field(default=False, description="Whether model supports reasoning effort")


class TokenUsageResponse(BaseModel):
    """令牌用量显示配置模型。

    Attributes:
        enabled: 是否在前端显示令牌用量。
    """

    enabled: bool = Field(default=False, description="Whether token usage display is enabled")


class ModelsListResponse(BaseModel):
    """模型列表响应模型。

    Attributes:
        models: 所有已配置模型的列表。
        token_usage: 令牌用量显示配置。
    """

    models: list[ModelResponse]
    token_usage: TokenUsageResponse


@router.get(
    "/models",
    response_model=ModelsListResponse,
    summary="List All Models",
    description="Retrieve a list of all available AI models configured in the system.",
)
async def list_models(config: AppConfig = Depends(get_config)) -> ModelsListResponse:
    """列出所有已配置的 AI 模型。

    从应用配置中读取模型列表，转换为前端友好的响应格式。
    排除敏感字段（API 密钥等），仅返回展示所需的元数据。

    Args:
        config: 应用配置对象（通过依赖注入获取）。

    Returns:
        ModelsListResponse，包含模型列表和令牌用量显示配置。
    """
    models = [
        ModelResponse(
            name=model.name,
            model=model.model,
            display_name=model.display_name,
            description=model.description,
            supports_thinking=model.supports_thinking,
            supports_reasoning_effort=model.supports_reasoning_effort,
        )
        for model in config.models
    ]
    return ModelsListResponse(
        models=models,
        token_usage=TokenUsageResponse(enabled=config.token_usage.enabled),
    )


@router.get(
    "/models/{model_name}",
    response_model=ModelResponse,
    summary="Get Model Details",
    description="Retrieve detailed information about a specific AI model by its name.",
)
async def get_model(model_name: str, config: AppConfig = Depends(get_config)) -> ModelResponse:
    """按名称查询单个 AI 模型的详细信息。

    Args:
        model_name: 模型的唯一名称。
        config: 应用配置对象（通过依赖注入获取）。

    Returns:
        ModelResponse，包含模型详细信息。

    Raises:
        HTTPException: 状态码 404，当模型名称不存在时抛出。
    """
    model = config.get_model_config(model_name)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

    return ModelResponse(
        name=model.name,
        model=model.model,
        display_name=model.display_name,
        description=model.description,
        supports_thinking=model.supports_thinking,
        supports_reasoning_effort=model.supports_reasoning_effort,
    )
