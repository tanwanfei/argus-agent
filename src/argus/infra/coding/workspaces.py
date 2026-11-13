import asyncio
import filecmp
import logging
import shutil
import subprocess
from pathlib import Path

from argus.codeagent.workspaces import Workspace, WorkspaceError, WorkspaceKind

logger = logging.getLogger(__name__)

_MAX_COPY_BYTES = 500 * 1024 * 1024
_GIT_TIMEOUT_S = 120
_COMMIT_IDENTITY = ("-c", "user.name=ARGUS", "-c", "user.email=argus@localhost")


class LocalWorkspaces:
    def __init__(self, work_root: Path) -> None:
        self._work_root = work_root

    async def create(self, source: Path, task_id: str, folder: str) -> Workspace:
        return await asyncio.to_thread(self._create, source, task_id, folder)

    async def changed_files(self, workspace: Workspace) -> list[str]:
        return await asyncio.to_thread(self._changed_files, workspace)

    async def publish(self, workspace: Workspace) -> str:
        return await asyncio.to_thread(self._publish, workspace)

    async def remove(self, workspace: Workspace) -> None:
        await asyncio.to_thread(self._remove, workspace)

    def _create(self, source: Path, task_id: str, folder: str) -> Workspace:
        if not source.is_dir():
            raise WorkspaceError(f"目录不在了：{source}，看看 code_projects.yaml 里写的对不对")
        target = self._work_root / folder
        if target.exists():
            raise WorkspaceError(f"工作目录已经有了：{target}")
        self._work_root.mkdir(parents=True, exist_ok=True)
        if (source / ".git").exists():
            return self._create_worktree(source, target, task_id)
        return self._copy_folder(source, target)

    def _create_worktree(self, source: Path, target: Path, task_id: str) -> Workspace:
        base_commit = _git(source, "rev-parse", "HEAD", doing="读当前的提交").strip()
        branch = f"argus/{task_id}"
        _git(source, "worktree", "add", "-b", branch, str(target), base_commit, doing="建 worktree")
        logger.info("派活：建好了 worktree，任务 %s", task_id)
        return Workspace(WorkspaceKind.GIT, target, source, branch=branch, base_commit=base_commit)

    def _copy_folder(self, source: Path, target: Path) -> Workspace:
        total_bytes = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
        if total_bytes > _MAX_COPY_BYTES:
            raise WorkspaceError(
                f"目录太大（{total_bytes // (1024 * 1024)} MB），超过 {_MAX_COPY_BYTES // (1024 * 1024)} MB 不复制。"
                "在 code_projects.yaml 里配小一点的目录"
            )
        try:
            shutil.copytree(source, target)
        except OSError as error:
            shutil.rmtree(target, ignore_errors=True)
            raise WorkspaceError(f"复制成任务文件夹没成：{type(error).__name__}") from error
        logger.info("派活：复制好了任务文件夹，%d 字节", total_bytes)
        return Workspace(WorkspaceKind.FOLDER, target, source)

    def _changed_files(self, workspace: Workspace) -> list[str]:
        if workspace.kind is WorkspaceKind.GIT:
            changed = _git(
                workspace.path, "diff", "--name-only", str(workspace.base_commit), doing="比改动"
            ).splitlines()
            untracked = _git(
                workspace.path, "ls-files", "--others", "--exclude-standard", doing="找新文件"
            ).splitlines()
            return sorted(set(changed) | set(untracked))
        changed_paths = []
        for path in workspace.path.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace.path)
            original = workspace.source / relative
            if not original.is_file() or not filecmp.cmp(path, original, shallow=False):
                changed_paths.append(relative.as_posix())
        return sorted(changed_paths)

    def _publish(self, workspace: Workspace) -> str:
        if workspace.kind is WorkspaceKind.GIT:
            return self._publish_branch(workspace)
        changed = self._changed_files(workspace)
        for relative in changed:
            destination = workspace.source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workspace.path / relative, destination)
        return f"改过、新加的 {len(changed)} 个文件复制回了原目录"

    def _publish_branch(self, workspace: Workspace) -> str:
        branch = str(workspace.branch)
        if _git(workspace.path, "status", "--porcelain", doing="看有没有没提交的").strip():
            _git(workspace.path, "add", "--all", doing="提交改动")
            _git(
                workspace.path,
                *_COMMIT_IDENTITY,
                "commit",
                "--quiet",
                "-m",
                f"ARGUS 派活：{branch}",
                doing="提交改动",
            )
        remotes = _git(workspace.path, "remote", doing="看有没有远端").split()
        if "origin" not in remotes:
            return f"仓库没有 origin，改动提交在本地分支 {branch} 上，没有合并，合并你自己来"
        _git(workspace.path, "push", "--quiet", "-u", "origin", branch, doing="推送")
        return f"推到了 origin 的 {branch} 分支，没有合并"

    def _remove(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.temp_dir, ignore_errors=True)
        if not workspace.path.exists():
            return
        if workspace.kind is WorkspaceKind.GIT:
            _git(
                workspace.source,
                "worktree",
                "remove",
                "--force",
                str(workspace.path),
                doing="删 worktree",
            )
            return
        shutil.rmtree(workspace.path)


def _git(directory: Path, *arguments: str, doing: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.quotePath=false", "-C", str(directory), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"git {doing}没成：{type(error).__name__}") from error
    if completed.returncode != 0:
        first_line = completed.stderr.strip().splitlines()[0] if completed.stderr.strip() else ""
        logger.warning("git %s没成（退出码 %d）：%s", doing, completed.returncode, first_line)
        raise WorkspaceError(f"git {doing}没成")
    return completed.stdout
