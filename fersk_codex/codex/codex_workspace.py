"""按用户初始化 Git 和 Office 环境，保留已有文件并回收超时子进程。"""

import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
from weakref import WeakValueDictionary


# 来源：四份参考技能的常用工作流；首次安装由包管理器解析兼容的最新版本。
PYTHON_PACKAGES = (
    "defusedxml", "lxml", "Pillow", "openpyxl", "pandas", "pypdf", "pdfplumber",
    "pdf2image", "reportlab", "markitdown[pptx,xlsx]", "pytesseract",
)
NODE_PACKAGE = {
    "name": "fersk-office-workspace", "private": True, "version": "1.0.0",
    "engines": {"node": ">=22"},
    "dependencies": {
        name: "latest" for name in ("docx", "pptxgenjs", "react", "react-dom", "react-icons", "sharp")
    },
}
# 来源：项目首次下载依赖的等待策略；Git 仍使用已有配置超时。
ENVIRONMENT_TIMEOUT = 600
_locks = WeakValueDictionary()
_MANIFESTS = ("package.json", "pnpm-lock.yaml")
_PYTHON_MODULES = ("defusedxml", "lxml", "PIL", "openpyxl", "pandas", "pypdf", "pdfplumber",
                   "pdf2image", "reportlab", "markitdown", "pytesseract")
_PYTHON_IMPORTS = "import " + ",".join(_PYTHON_MODULES)


def workspace_environment(workspace: Path) -> dict[str, str]:
    """仅返回当前任务覆盖值，不修改服务的全局环境变量。"""
    workspace = workspace.resolve()
    return {
        "VIRTUAL_ENV": str(workspace / ".venv"),
        "PATH": os.pathsep.join((str(workspace / ".venv/bin"),
                                str(workspace / "node_modules/.bin"), os.environ.get("PATH", ""))),
    }


@asynccontextmanager
async def _workspace_lock(workspace: Path):
    # 弱引用避免用户数量增长时永久保留 asyncio.Lock；flock 处理多个服务进程。
    lock = _locks.setdefault(str(workspace), asyncio.Lock())
    async with lock:
        with (workspace / ".office-init.lock").open("a") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


async def _reap(process):
    # 子进程独立会话：uv/pnpm 启动的后代也必须收到终止信号。
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=2)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def _initialize_git(workspace: Path, timeout: float):
    await _run(["git", "init", "-q", "-b", "main"], workspace, timeout)


async def _run(command, workspace: Path, timeout: float, *, env=None):
    creating = None
    process = None
    try:
        async with asyncio.timeout(timeout):
            creating = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=workspace, env=env, start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            ))
            # 创建过程不能因取消丢失稍后返回的进程句柄。
            process = await asyncio.shield(creating)
            stdout, stderr = await process.communicate()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            return stdout
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


def _write_state(workspace: Path, state: dict):
    temporary = workspace / ".office-env.json.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(workspace / ".office-env.json")


async def _initialize_environment(workspace: Path):
    manifests = {"package.json": (json.dumps(NODE_PACKAGE, indent=2) + "\n").encode()}
    requirements = "\n".join(PYTHON_PACKAGES).encode()
    node_version = (await _run(["node", "--version"], workspace, 30)).decode().strip()
    fingerprint = hashlib.sha256(requirements + b"".join(manifests.values()) + node_version.encode() +
                                 str((sys.version, platform.system(), platform.machine())).encode()).hexdigest()
    state_path = workspace / ".office-env.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    packages = json.loads(manifests["package.json"])["dependencies"]
    python = workspace / ".venv/bin/python"
    site_packages = workspace / f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    if (state and state.get("owner") == "fersk-office" and state.get("status") == "ready"
            and state.get("fingerprint") == fingerprint
            and python.is_file() and (workspace / ".venv/pyvenv.cfg").is_file()
            and all((site_packages / name).exists() for name in _PYTHON_MODULES)
            and all((workspace / "node_modules" / package).exists() for package in packages)
            and (workspace / "pnpm-lock.yaml").is_file()
            and hashlib.sha256((workspace / "pnpm-lock.yaml").read_bytes()).hexdigest()
                == state.get("manifests", {}).get("pnpm-lock.yaml")
            and all((workspace / name).is_file() and (workspace / name).read_bytes() == content
                    for name, content in manifests.items())):
        return

    if state is None:
        conflicts = [name for name in (".venv", "node_modules", *_MANIFESTS, "package-lock.json",
                                       "yarn.lock", "pnpm-workspace.yaml", ".npmrc")
                     if (workspace / name).exists() or (workspace / name).is_symlink()]
        if conflicts:
            raise RuntimeError("工作区存在非初始化器管理的环境，未覆盖：" + ", ".join(conflicts))
    elif state.get("owner") != "fersk-office":
        raise RuntimeError("无法识别工作区 .office-env.json，未修改现有环境")

    lockfile = workspace / "pnpm-lock.yaml"
    if lockfile.exists():
        recorded = (state or {}).get("manifests", {}).get("pnpm-lock.yaml")
        if recorded and hashlib.sha256(lockfile.read_bytes()).hexdigest() != recorded:
            raise RuntimeError("工作区 pnpm-lock.yaml 已被修改，未覆盖")

    # 升级仅更新仍与记录一致的受管清单；用户自行修改的清单必须先人工处理。
    for name, content in manifests.items():
        target = workspace / name
        if target.exists() and target.read_bytes() != content:
            recorded = (state or {}).get("manifests", {}).get(name)
            if hashlib.sha256(target.read_bytes()).hexdigest() != recorded:
                raise RuntimeError(f"工作区 {name} 已被修改，未覆盖")
    if python.exists():
        actual = await _run([str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                            workspace, 30)
        if actual.decode().strip() != f"{sys.version_info.major}.{sys.version_info.minor}":
            raise RuntimeError("工作区 Python 版本与服务不一致，需要人工迁移 .venv，未删除环境")
    elif (workspace / ".venv").exists():
        raise RuntimeError("工作区 .venv 不完整，需要人工检查，未重建或删除")

    required = ("uv", "node", "pnpm", "soffice", "pdftoppm", "pdfinfo", "pdftotext", "pdfimages", "pandoc", "tesseract")
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        raise RuntimeError("缺少系统工具：" + ", ".join(missing))
    if int(node_version.lstrip("v").split(".")[0]) < 22:
        raise RuntimeError("Office 工作区需要 Node.js >=22")

    state = {"owner": "fersk-office", "status": "installing", "fingerprint": fingerprint,
             "manifests": (state or {}).get("manifests", {})}
    _write_state(workspace, state)
    # 安装阶段使用显式 Python 路径，避免 uv 误用服务的虚拟环境。
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_NO_CACHE"] = "false"
    env["CI"] = "true"
    if not python.exists():
        await _run(["uv", "venv", "--python", sys._base_executable, str(workspace / ".venv")],
                   workspace, ENVIRONMENT_TIMEOUT, env=env)
    await _run(["uv", "pip", "install", "--python", str(python), *PYTHON_PACKAGES],
               workspace, ENVIRONMENT_TIMEOUT, env=env)
    for name, content in manifests.items():
        (workspace / name).write_bytes(content)
    state["manifests"].update({name: hashlib.sha256(content).hexdigest() for name, content in manifests.items()})
    _write_state(workspace, state)
    # 首次生成用户自己的 lockfile；已有锁文件由 pnpm 复用，升级直接依赖时允许更新。
    try:
        await _run(["pnpm", "install", "--no-frozen-lockfile", "--ignore-scripts", "--strict-peer-dependencies"],
                   workspace, ENVIRONMENT_TIMEOUT, env=env)
    finally:
        # 安装失败也可能已更新锁文件，记录安装器的写入以便下次重试。
        if lockfile.is_file():
            state["manifests"]["pnpm-lock.yaml"] = hashlib.sha256(lockfile.read_bytes()).hexdigest()
            _write_state(workspace, state)
    await _run([str(python), "-c", _PYTHON_IMPORTS], workspace, 30, env=env)
    await _run(["uv", "pip", "check", "--python", str(python)], workspace, 30, env=env)
    # 实际执行 sharp，验证平台二进制，而不只是检查包目录存在。
    node_probe = """const fs = require('node:fs');
for (const name of Object.keys(JSON.parse(fs.readFileSync('package.json')).dependencies)) require(name);
require('sharp')(Buffer.from('<svg width="8" height="8"><rect width="8" height="8"/></svg>'))
  .png().toBuffer().catch(e => { console.error(e); process.exitCode = 1; });"""
    await _run(["node", "-e", node_probe], workspace, 30, env=env)
    state["status"] = "ready"
    state["node"] = node_version
    _write_state(workspace, state)


async def prepare_workspace(workspace: Path, timeout: float):
    workspace = workspace.resolve()
    def prepare():
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            (workspace / "AGENTS.md").touch(exist_ok=False)
        except FileExistsError:
            pass

    await asyncio.to_thread(prepare)
    async with asyncio.timeout(ENVIRONMENT_TIMEOUT):
        async with _workspace_lock(workspace):
            # worktree 的 .git 是文件，同样视为已初始化；加锁后重新判断。
            if not (workspace / ".git").exists():
                await _initialize_git(workspace, timeout)
            await _initialize_environment(workspace)
