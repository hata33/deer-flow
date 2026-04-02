"""
配置系统测试
"""
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from config import AppConfig, ModelConfig, get_app_config, reset_app_config


def test_model_config():
    """测试模型配置"""
    config = ModelConfig(
        name="test-model",
        provider="openai",
        model="gpt-4",
        api_key="test-key",
    )

    assert config.name == "test-model"
    assert config.supports_tools is True
    assert config.supports_vision is False


def test_env_resolution():
    """测试环境变量解析"""
    os.environ["TEST_VAR"] = "test-value"

    result = AppConfig.resolve_env_variables({"key": "$TEST_VAR"})
    assert result == {"key": "test-value"}

    del os.environ["TEST_VAR"]


def test_config_from_file():
    """测试从文件加载配置"""
    config_data = {
        "log_level": "DEBUG",
        "models": [
            {
                "name": "test-model",
                "provider": "openai",
                "model": "gpt-4",
                "api_key": "test-key",
            }
        ],
        "default_model": "test-model",
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(config_data, f)
        temp_path = f.name

    try:
        config = AppConfig.from_file(temp_path)
        assert config.log_level == "DEBUG"
        assert len(config.models) == 1
        assert config.default_model == "test-model"
    finally:
        os.unlink(temp_path)


def test_get_model_config():
    """测试获取模型配置"""
    config = AppConfig(
        models=[
            ModelConfig(name="model1", provider="openai", model="gpt-4"),
            ModelConfig(name="model2", provider="anthropic", model="claude-3"),
        ],
        default_model="model1",
    )

    # 获取默认模型
    model = config.get_model_config()
    assert model.name == "model1"

    # 获取指定模型
    model = config.get_model_config("model2")
    assert model.name == "model2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
