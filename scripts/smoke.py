import asyncio
import shutil
from pathlib import Path

from yotcode.agent import Agent, AgentCallbacks
from yotcode.config import build_config
from yotcode.providers import create_provider
from yotcode.tools import ToolContext, create_tools
from yotcode.utils import configure_stdio

configure_stdio()
ROOT = Path(__file__).resolve().parent
SANDBOX = ROOT / "sandbox"


class CB(AgentCallbacks):
    def on_text(self, text):
        print(text, end="", flush=True)

    def on_thinking(self, text):
        pass

    def on_tool_start(self, block):
        print(f"\n[tool_start] {block.name} {block.input}")

    def on_tool_result(self, block, result, duration):
        print(f"[tool_result] {block.name} err={result.is_error} {duration:.2f}s -> {result.text[:200]!r}")

    def on_notice(self, message, level="info"):
        print(f"\n[notice:{level}] {message}")

    def on_todos(self, todos):
        print(f"\n[todos] {todos}")

    def on_usage(self, usage, cost, total_usage, total_cost):
        print(f"\n[usage] in={usage.input_tokens} out={usage.output_tokens} cost=${cost:.4f} total=${total_cost:.4f}")


async def main():
    shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    (SANDBOX / "hello.py").write_text("def greet(name):\n    return f'hi {name}'\n", encoding="utf-8")
    (SANDBOX / "README.md").write_text("# Demo project\n\nSome docs.\n", encoding="utf-8")

    cfg = build_config(SANDBOX, mock=True)
    tools = {t.name: t for t in create_tools()}
    ctx = ToolContext(cwd=SANDBOX, config=cfg, cancel=asyncio.Event(), session_id="test")

    print("== Write ==")
    r = await tools["Write"].run({"file_path": "src/app.py", "content": "print('hello')\n"}, ctx)
    print(r.is_error, r.text)

    print("== Read ==")
    r = await tools["Read"].run({"file_path": "src/app.py"}, ctx)
    print(r.is_error, repr(r.text))

    print("== Edit ==")
    r = await tools["Edit"].run({"file_path": "src/app.py", "old_string": "hello", "new_string": "world"}, ctx)
    print(r.is_error, r.text[:200])

    print("== MultiEdit ==")
    r = await tools["MultiEdit"].run({"file_path": "src/app.py", "edits": [
        {"old_string": "world", "new_string": "there"},
        {"old_string": "print", "new_string": "console.log"},
    ]}, ctx)
    print(r.is_error, r.text[:200], "| file:", (SANDBOX / "src/app.py").read_text())

    print("== Glob ==")
    r = await tools["Glob"].run({"pattern": "**/*.py"}, ctx)
    print(r.is_error, repr(r.text))

    print("== Grep ==")
    r = await tools["Grep"].run({"pattern": "def ", "output_mode": "content"}, ctx)
    print(r.is_error, repr(r.text))

    print("== LS ==")
    r = await tools["LS"].run({"path": "."}, ctx)
    print(r.is_error, r.text[:300])

    print("== Bash ==")
    r = await tools["Bash"].run({"command": "echo hello-from-bash && python --version"}, ctx)
    print(r.is_error, repr(r.text), r.meta)

    print("== Todo ==")
    r = await tools["TodoWrite"].run({"todos": [
        {"content": "a", "status": "completed", "activeForm": "doing a"},
        {"content": "b", "status": "in_progress", "activeForm": "doing b"},
    ]}, ctx)
    print(r.is_error, r.text[:80])

    print("\n== Agent (mock) ==")
    provider = create_provider(cfg)
    agent = Agent(cfg, provider, list(tools.values()), callbacks=CB())
    text = await agent.run("请列出当前目录的文件")
    print("\n\nFINAL:", text)
    await agent.close()


asyncio.run(main())
