import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from argus.codeagent.runner import ToolVerdict
from argus.governance.code_agent import Refused, classify_agent_step, describe_agent_step
from argus.governance.governance import Allow, Decision, Deny, Governance
from argus.kernel.ids import IdGen
from argus.kernel.tools import Origin, Permission, Speaker, ToolCall, ToolContext, ToolSpec

logger = logging.getLogger(__name__)

_AGENT_CONTEXT = ToolContext(speaker=Speaker.OWNER, origin=Origin.CODE_AGENT, precise_location=None)


class _StepArgs(BaseModel):
    task: str
    step: str


class AgentToolGate:
    def __init__(
        self,
        *,
        task_id: str,
        workspace: Path,
        temp_dir: Path,
        protected_dirs: Sequence[Path],
        refused_commands: Sequence[str],
        governance: Governance,
        ids: IdGen,
    ) -> None:
        self._task_id = task_id
        self._workspace = workspace
        self._temp_dir = temp_dir
        self._protected_dirs = protected_dirs
        self._refused_commands = refused_commands
        self._governance = governance
        self._ids = ids

    async def decide(self, tool_name: str, tool_input: Mapping[str, Any]) -> ToolVerdict:
        level = classify_agent_step(
            tool_name,
            tool_input,
            workspace=self._workspace,
            temp_dir=self._temp_dir,
            protected_dirs=self._protected_dirs,
            refused_commands=self._refused_commands,
        )
        call = ToolCall(
            self._ids.new("agent-step"),
            f"code_agent.{tool_name}",
            {"task": self._task_id, "step": describe_agent_step(tool_name, tool_input)},
        )
        spec = ToolSpec(
            name=call.name,
            description="编码代理的一步",
            params=_StepArgs,
            permission=Permission.L3B if isinstance(level, Refused) else level,
        )
        decision: Decision = (
            Deny(level.reason)
            if isinstance(level, Refused)
            else self._governance.authorize(spec, call, _AGENT_CONTEXT)
        )
        await self._governance.audit_decision(spec, call, _AGENT_CONTEXT, decision)
        if isinstance(decision, Allow):
            return ToolVerdict(True)
        logger.info("派活：代理的一步被拒（%s），任务 %s", tool_name, self._task_id)
        return ToolVerdict(False, decision.reason if isinstance(decision, Deny) else "这一步不让做")
