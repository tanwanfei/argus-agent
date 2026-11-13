import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from argus.kernel.tools import Permission

_READ_TOOLS = {
    "Read": "file_path",
    "Glob": "path",
    "Grep": "path",
    "LS": "path",
    "NotebookRead": "notebook_path",
}
_WRITE_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
_COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})
_HARMLESS_TOOLS = frozenset(
    {"TodoWrite", "Task", "Agent", "TaskOutput", "BashOutput", "KillShell", "KillBash"}
)
_COMMAND_BREAK = re.compile(r"[;&|\r\n`]|\$\(")
_QUOTES_AND_BRACKETS = str.maketrans(dict.fromkeys("\"'(){}<>", " "))
_PROGRAM_SUFFIXES = (".exe", ".cmd", ".bat")
_OPTIONS_WITH_VALUE = frozenset({"-c", "--git-dir", "--work-tree", "--namespace"})


@dataclass(frozen=True)
class Refused:
    reason: str


REFUSED_PROTECTED = Refused("这个目录不能碰：是 ARGUS 自己的代码或数据")
REFUSED_WRITE_OUTSIDE = Refused(
    "只能在工作目录里写（临时文件放环境变量 TEMP 指的目录）。要改的文件放在工作目录里，做完用户自己发布回去"
)


def classify_agent_step(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    workspace: Path,
    temp_dir: Path,
    protected_dirs: Sequence[Path],
    refused_commands: Sequence[str],
) -> Permission | Refused:
    if tool_name in _HARMLESS_TOOLS:
        return Permission.L1
    if tool_name in _READ_TOOLS or tool_name in _WRITE_TOOLS:
        is_write = tool_name in _WRITE_TOOLS
        field = _WRITE_TOOLS[tool_name] if is_write else _READ_TOOLS[tool_name]
        raw_path = tool_input.get(field)
        target = workspace if not raw_path else _absolute(str(raw_path), workspace)
        if any(target.is_relative_to(protected.resolve()) for protected in protected_dirs):
            return REFUSED_PROTECTED
        if not is_write:
            return Permission.L1
        if any(target.is_relative_to(writable.resolve()) for writable in (workspace, temp_dir)):
            return Permission.L2
        return REFUSED_WRITE_OUTSIDE
    if tool_name in _COMMAND_TOOLS:
        return _classify_command(
            str(tool_input.get("command", "")), protected_dirs, refused_commands
        )
    return Refused(f"{tool_name} 这个工具在这里用不了，换别的办法")


def describe_agent_step(tool_name: str, tool_input: Mapping[str, Any]) -> str:
    if tool_name in _COMMAND_TOOLS:
        text = str(tool_input.get("command", ""))
    else:
        field = _READ_TOOLS.get(tool_name) or _WRITE_TOOLS.get(tool_name)
        text = str(tool_input.get(field, "")) if field else ""
    text = f"{tool_name}：{text}" if text else tool_name
    return text if len(text) <= 300 else text[:300] + "…"


def step_kind(tool_name: str) -> str | None:
    if tool_name in _READ_TOOLS:
        return "看文件"
    if tool_name in _WRITE_TOOLS:
        return "改文件"
    if tool_name in _COMMAND_TOOLS:
        return "跑命令"
    return None


def refused_command_in(command: str, refused_commands: Sequence[str]) -> str | None:
    rules = [rule.casefold().split() for rule in refused_commands]
    for one_command in _COMMAND_BREAK.split(command):
        words = one_command.translate(_QUOTES_AND_BRACKETS).casefold().split()
        for position, word in enumerate(words):
            program = _program_name(word)
            following_words = words[position + 1 :]
            for rule in rules:
                if program == rule[0] and _rest_of_rule_matches(rule[1:], following_words):
                    return " ".join(rule)
    return None


def _rest_of_rule_matches(rest_of_rule: Sequence[str], following_words: Sequence[str]) -> bool:
    if not rest_of_rule:
        return True
    wanted = rest_of_rule[0]
    if wanted.startswith(("-", "/")):
        return any(
            word.startswith(("-", "/")) and word.lstrip("-/") == wanted.lstrip("-/")
            for word in following_words
        )
    return _subcommand(following_words) == wanted


def _subcommand(following_words: Sequence[str]) -> str | None:
    skip_value = False
    for word in following_words:
        if skip_value:
            skip_value = False
        elif word in _OPTIONS_WITH_VALUE:
            skip_value = True
        elif not word.startswith("-"):
            return word
    return None


def _program_name(word: str) -> str:
    name = PurePosixPath(word.replace("\\", "/")).name
    for suffix in _PROGRAM_SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def _classify_command(
    command: str, protected_dirs: Sequence[Path], refused_commands: Sequence[str]
) -> Permission | Refused:
    normalized = command.replace("\\", "/").casefold()
    for protected in protected_dirs:
        posix = protected.as_posix().casefold()
        bash_style = "/" + posix.replace(":", "", 1) if ":" in posix else posix
        if posix in normalized or bash_style in normalized:
            return REFUSED_PROTECTED
    matched_rule = refused_command_in(command, refused_commands)
    if matched_rule is not None:
        return Refused(
            f"命令里的「{matched_rule}」不让做：推送、发布、连别的机器、按名字杀进程、关机、改系统设置"
            "都不归你管。推送和发布用户自己来；要停你自己起的进程，按进程号停。换个办法接着做"
        )
    return Permission.L2


def _absolute(raw_path: str, workspace: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()
