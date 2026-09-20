"""异步准备工作区，Git 初始化具有超时与子进程回收保证。"""

import asyncio
from pathlib import Path
import subprocess


async def _reap(process):
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=2)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()


async def _initialize_git(workspace: Path, timeout: float):
    command = ["git", "init", "-q", "-b", "main"]
    creating = None
    process = None
    try:
        async with asyncio.timeout(timeout):
            creating = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            ))
            # 创建过程不能因取消丢失稍后返回的进程句柄。
            process = await asyncio.shield(creating)
            stdout, stderr = await process.communicate()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    except BaseException:
        async def cleanup():
            child = process
            if child is None and creating is not None:
                try:
                    child = await creating
                except Exception:
                    return
            if child is not None:
                await _reap(child)
        cleaning = asyncio.create_task(cleanup())
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                continue
        cleaning.result()
        raise


async def prepare_workspace(workspace: Path, timeout: float):
    def prepare():
        workspace.mkdir(parents=True, exist_ok=True)
        # worktree 的 .git 是文件，同样视为已初始化。
        initialized = (workspace / ".git").exists()
        try:
            (workspace / "AGENTS.md").touch(exist_ok=False)
        except FileExistsError:
            pass
        return initialized

    if not await asyncio.to_thread(prepare):
        await _initialize_git(workspace, timeout)
