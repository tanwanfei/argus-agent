import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError

from argus.infra.speaker.voiceprint import Voiceprint

VOICEPRINT_FILE_NAME = "voiceprint.json"
_NAMED_PREFIX = "voiceprint-"
_NAME_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class VoiceprintFileError(Exception):
    pass


class _VoiceprintFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    sentence_count: int = Field(gt=0)
    vector: list[FiniteFloat] = Field(min_length=1)


def save_voiceprint(path: Path, voiceprint: Voiceprint) -> None:
    content = _VoiceprintFile(
        model=voiceprint.model,
        sentence_count=voiceprint.sentence_count,
        vector=list(voiceprint.vector),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_bytes(content.model_dump_json(indent=2).encode("utf-8"))
    os.replace(temporary_path, path)


def load_voiceprint(path: Path) -> Voiceprint:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise VoiceprintFileError(f"还没注册声纹：没有 {path}") from error
    except OSError as error:
        raise VoiceprintFileError(f"读不了声纹文件 {path}：{error}") from error
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise VoiceprintFileError(f"声纹文件 {path} 不是 JSON，重新注册一次") from None
    try:
        content = _VoiceprintFile.model_validate(parsed)
    except ValidationError as error:
        wrong_fields = sorted(
            {".".join(map(str, detail["loc"])) or "整个文件" for detail in error.errors()}
        )
        raise VoiceprintFileError(
            f"声纹文件 {path} 的内容不对（{'、'.join(wrong_fields)}），重新注册一次"
        ) from None
    return Voiceprint(content.model, content.sentence_count, tuple(content.vector))


def named_voiceprint_path(home_path: Path, name: str) -> Path:
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"声纹的名字只能用小写字母、数字和连字符，比如 phone、airpods：{name!r} 不行"
        )
    return home_path.with_name(f"{_NAMED_PREFIX}{name}.json")


def named_voiceprint_paths(home_path: Path) -> dict[str, Path]:
    paths = {}
    for path in home_path.parent.glob(f"{_NAMED_PREFIX}*.json"):
        name = path.stem.removeprefix(_NAMED_PREFIX)
        if _NAME_PATTERN.fullmatch(name):
            paths[name] = path
    return dict(sorted(paths.items()))
