import argparse
import asyncio
import logging
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

from argus.app.build import Argus, build
from argus.app.logs import log_to_daily_files
from argus.app.server import ServerSetupError, bind_localhost
from argus.app.settings import (
    DEFAULT_CONFIG_PATH,
    MIGRATIONS_ROOT,
    ConfigError,
    check_calendar_credentials,
    check_code_projects,
    check_home_assistant_token,
    check_mail_credentials,
    code_projects_path,
    fallback_api_key,
    intents_path,
    load_code_projects,
    load_config,
    load_intents,
    load_models_config,
    load_persona,
    load_secrets,
    load_sensitive_words,
    load_tools_config,
    load_triggers_config,
    models_config_path,
    persona_path,
    tools_config_path,
    triggers_config_path,
)
from argus.app.voice import VoiceSetupError
from argus.infra.sqlite.migrator import MigrationError, migrate_all

logger = logging.getLogger("argus.app.main")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m argus.app.main", description="启动 ARGUS 主进程"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="主配置文件，默认是仓库里的 config/argus.yaml；models.yaml、intents.yaml、tools.yaml、triggers.yaml、persona.md 到它所在的目录找",
    )
    arguments = parser.parse_args(argv)
    config_path: Path = arguments.config

    try:
        config = load_config(config_path)
        secrets = load_secrets(config.secrets_env_path)
        models = load_models_config(models_config_path(config_path))
        persona = load_persona(persona_path(config_path))
        intents = load_intents(intents_path(config_path))
        tools = load_tools_config(tools_config_path(config_path))
        triggers = load_triggers_config(triggers_config_path(config_path))
        fallback_api_key(models, secrets)
        check_home_assistant_token(config, secrets)
        check_calendar_credentials(config, secrets)
        check_mail_credentials(config, secrets)
        code_projects = load_code_projects(code_projects_path(config_path))
        check_code_projects(config, tools, code_projects)
        sensitive_words = load_sensitive_words(config.sensitive_words_path)
    except ConfigError as error:
        print(f"ARGUS 没有启动。{error}", file=sys.stderr)
        return 1

    for directory in (config.logs_dir, config.data_dir):
        try:
            directory.mkdir(exist_ok=True)
        except OSError as error:
            print(f"ARGUS 没有启动。建不了目录：{directory}\n  {error}", file=sys.stderr)
            return 1

    with log_to_daily_files(config.logs_dir):
        logger.info("ARGUS 启动，配置文件：%s", config_path)
        try:
            with bind_localhost(config.server.port) as http_socket:
                migrate_all(MIGRATIONS_ROOT, config.data_dir)
                argus = build(
                    config,
                    persona=persona,
                    intents=intents,
                    tools=tools,
                    triggers=triggers,
                    models=models,
                    secrets=secrets,
                    sensitive_words=sensitive_words,
                    code_projects=code_projects,
                )
                asyncio.run(_serve(argus, http_socket))
        except (ServerSetupError, MigrationError, VoiceSetupError) as error:
            logger.error("ARGUS 没有启动。%s", error)
            print(f"ARGUS 没有启动。{error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            pass
        except Exception:
            logger.exception("ARGUS 崩溃")
            raise
        logger.info("ARGUS 停机")
    return 0


async def _serve(argus: Argus, http_socket: socket.socket) -> None:
    print("ARGUS ready", flush=True)
    await argus.run(http_socket)


if __name__ == "__main__":
    sys.exit(main())
