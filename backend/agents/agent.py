"""Kiri 多智能体入口。

用法：
    python backend/agents/agent.py "帮我查一下单号 12345"   # 一次性
    python backend/agents/agent.py                          # 交互式（带会话记忆）
"""

import asyncio
import os
import sys
from pathlib import Path

from agents import (
    Agent,
    MaxTurnsExceeded,
    Model,
    OpenAIChatCompletionsModel,
    Runner,
    SQLiteSession,
    function_tool,
    set_tracing_disabled,
)
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from dotenv import load_dotenv
from openai import AsyncOpenAI

REPO_ROOT = Path(__file__).resolve().parents[2]

# 每轮对话最多的 LLM 往返次数，超过就中断，防止工具调用死循环
MAX_TURNS = 40


# ==========================================
# 0. Provider 配置：读 .env，接本地 OpenAI 兼容服务
# ==========================================
def build_model() -> Model:
    """按 .env 里的配置构造 model 对象。"""
    load_dotenv(REPO_ROOT / ".env")

    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL") if not os.getenv(k)]
    if missing:
        raise SystemExit(
            f"缺少环境变量：{', '.join(missing)}。\n"
            f"请从 .env.example 复制一份到 {REPO_ROOT / '.env'} 并填好。"
        )

    client = AsyncOpenAI(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    )

    # 显式构造 model 对象，而不是给 Agent 传模型名字符串，原因有两个：
    # 1) 本地 llama.cpp / vLLM / Ollama 只实现了 /chat/completions，没有 /responses，
    #    而 SDK 默认走 Responses API；
    # 2) 模型名里的 "/"（如 unsloth/Qwen3.6-27B-GGUF）会被 SDK 的 MultiProvider
    #    误判成 provider 前缀，报 "Unknown prefix"。
    return OpenAIChatCompletionsModel(
        model=os.environ["OPENAI_MODEL"],
        openai_client=client,
    )


# 追踪数据默认上传到 OpenAI 官方，用本地模型时没有官方 key，直接关掉
set_tracing_disabled(True)


# ==========================================
# 1. 工具：普通 Python 函数 + @function_tool
# ==========================================
@function_tool
def get_order_status(order_id: str) -> str:
    """根据订单号查询订单状态。

    Args:
        order_id: 用户提供的订单号。
    """
    # 模拟真实世界里的数据库查询
    if order_id == "12345":
        return "订单 12345 状态：已发货，顺丰快递，预计明天上午送达。"
    return f"抱歉，未查找到订单号为 {order_id} 的信息。"


# ==========================================
# 2. 智能体定义
# ==========================================
def build_agents(model: Model) -> Agent:
    """构造 agent 图，返回入口 agent。"""
    order_specialist = Agent(
        name="Order Specialist",
        # handoff_description 是给上级 agent 看的：它决定什么时候该转交过来
        handoff_description="处理订单查询、物流跟踪、售后等具体问题",
        instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
你是一个专注的订单售后客服。请使用工具帮用户查询订单，并用热情友好的语气回复结果。
查询前必须先拿到订单号；用户没给就先问，不要编造订单号。""",
        model=model,
        tools=[get_order_status],
    )

    return Agent(
        name="Manager",
        instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
你是一个全能前台助手，可以处理普通的闲聊。
如果用户有任何关于订单查询、售后相关的具体问题，请立刻转交 (handoff) 给 Order Specialist。""",
        model=model,
        handoffs=[order_specialist],
    )


# ==========================================
# 3. 运行
# ==========================================
async def ask(agent: Agent, user_input: str, session: SQLiteSession | None = None) -> str:
    """跑一轮对话，把 SDK 的异常翻译成能直接展示给用户的文字。"""
    try:
        result = await Runner.run(agent, user_input, session=session, max_turns=MAX_TURNS)
        return result.final_output
    except MaxTurnsExceeded:
        return f"（超过 {MAX_TURNS} 轮仍未得到结果，已中断。请把问题拆小一点再试。）"


async def main() -> None:
    agent = build_agents(build_model())

    # 一次性模式：把命令行参数当成一条 prompt
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        print(f"User: {prompt}\n")
        print(f"Agent: {await ask(agent, prompt)}")
        return

    # 交互模式：session 负责在多轮之间自动带上历史，不用手工拼 to_input_list()
    session = SQLiteSession("cli")
    print("进入交互模式，Ctrl-C 或 Ctrl-D 退出。\n")
    while True:
        try:
            user_input = input("User: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return
        if not user_input:
            continue
        print(f"\nAgent: {await ask(agent, user_input, session)}\n")


if __name__ == "__main__":
    # Windows 终端默认用本地代码页（如 cp950）：输出侧模型吐 emoji 会抛
    # UnicodeEncodeError，输入侧管道进来的中文会被解成代理对，两边都强制 UTF-8
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())