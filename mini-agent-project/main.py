"""
Mini Agent - 主入口

简化版 AI 代理系统
"""
import asyncio
import logging
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

from config import get_app_config, get_paths, reset_app_config
from agents import create_agent

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def interactive_mode(agent):
    """交互式对话模式"""
    print("\n" + "=" * 50)
    print("Mini Agent - 简化版 AI 代理系统")
    print("=" * 50)
    print("输入 'exit' 或 'quit' 退出\n")

    state = None

    while True:
        try:
            user_input = input("你: ").strip()

            if not user_input:
                continue

            if user_input.lower() in ['exit', 'quit', '退出']:
                print("\n再见!")
                break

            # 处理用户输入
            print("\nAgent: ", end="", flush=True)
            response, state = await agent.chat(user_input, state)
            print(response)
            print()

        except KeyboardInterrupt:
            print("\n\n再见!")
            break
        except Exception as e:
            logger.error(f"处理错误: {e}", exc_info=True)
            print(f"\n错误: {e}\n")


async def main():
    """主函数"""
    logger.info("Mini Agent 启动中...")

    try:
        # 加载配置
        try:
            config = get_app_config()
            logger.info(f"✓ 配置加载成功，默认模型: {config.default_model}")
            logger.info(f"✓ 已配置 {len(config.models)} 个模型")
        except Exception as e:
            logger.error(f"✗ 配置加载失败: {e}")
            print("\n错误: 配置文件加载失败")
            print("\n请确保设置了 API 密钥：")
            print("1. 创建 .env 文件并添加: OPENAI_API_KEY=your_key")
            print("2. 或者创建 config.yaml（参考 config.example.yaml）")
            return

        # 获取路径
        paths = get_paths()
        logger.info(f"✓ 工作目录: {paths.work_dir}")
        logger.info(f"✓ 数据目录: {paths.data_dir}")

        # 检查模型配置
        if not config.models:
            print("\n警告: 未配置任何模型")
            print("请在 .env 文件中设置 OPENAI_API_KEY 或在 config.yaml 中配置模型")
            return

        # 创建代理
        try:
            agent = await create_agent()
            logger.info(f"✓ 代理已创建，可用工具: {len(agent.tools)}")
        except Exception as e:
            logger.error(f"✗ 代理创建失败: {e}", exc_info=True)
            print(f"\n错误: 代理创建失败 - {e}")
            return

        print("\nMini Agent 已就绪!\n")
        print(f"模型: {agent.model_name}")
        if agent.tools:
            print(f"工具: {', '.join([t.name for t in agent.tools])}")
        else:
            print("工具: 无")

        # 进入交互模式
        await interactive_mode(agent)

    except KeyboardInterrupt:
        print("\n\n已中断")
    except Exception as e:
        logger.error(f"✗ 启动失败: {e}", exc_info=True)
        print(f"\n错误: {e}")
    finally:
        logger.info("Mini Agent 已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n已中断")
