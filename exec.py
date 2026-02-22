import os
import logging
import subprocess
from google.genai import types


class ExecSkill:
    """
    Скилл для выполнения команд внутри Docker-контейнера.

    По умолчанию монтируется директория workspace.
    Дополнительные папки хост-машины берутся из config.exec.folders
    и монтируются в контейнер как /mnt/<имя_папки>.
    """

    def __init__(
        self,
        workspace_dir: str | None = None,
        image: str = "python:3.11-slim",
        default_timeout: int = 60,
        runtime: str = "podman",
        config=None,
    ):
        root = os.path.dirname(os.path.abspath(__file__))
        self.workspace_dir = workspace_dir or os.path.join(root, "workspace")
        os.makedirs(self.workspace_dir, exist_ok=True)

        self.image = image
        self.default_timeout = default_timeout
        self.runtime = runtime
        self.config = config

        self.tools = [
            types.FunctionDeclaration(
                name="exec",
                description=(
                    "Выполнить команду внутри Docker-контейнера. "
                    "Всегда доступна директория /workspace. "
                    "Дополнительные папки из exec.folders монтируются как /mnt/<имя_папки>."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "command": types.Schema(
                            type=types.Type.STRING,
                            description="Строка команды для выполнения (bash/sh синтаксис).",
                        ),
                        "timeout": types.Schema(
                            type=types.Type.INTEGER,
                            description="Необязательный таймаут в секундах.",
                        ),
                        "workdir": types.Schema(
                            type=types.Type.STRING,
                            description="Необязательная рабочая директория внутри контейнера (по умолчанию /workspace).",
                        ),
                    },
                    required=["command"],
                ),
            )
        ]

    def get_context_prompt(self) -> str:
        extra_folders = []
        if self.config:
            extra_folders = self.config.get("exec.folders") or []

        lines = [
            "## Инструмент exec",
            "Ты можешь выполнять команды в Docker-контейнере.",
            "Директория /workspace всегда доступна для чтения и записи.",
        ]
        if extra_folders:
            lines.append("Дополнительно примонтированы папки хост-машины:")
            for folder in extra_folders:
                mount_name = os.path.basename(folder.rstrip("/\\")) or "folder"
                lines.append(f"  - {folder}  →  /mnt/{mount_name}  (только чтение)")
        else:
            lines.append(
                "Дополнительных папок нет. Если нужен доступ к папке на хост-машине, "
                "попроси пользователя выполнить:\n"
                "  /config add exec.folders <абсолютный путь к папке>"
            )
        return "\n".join(lines)

    def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name != "exec":
            return {"error": f"Unknown tool: {tool_call.name}"}

        command = tool_call.args.get("command")
        if not command:
            return {"error": "command is required"}

        timeout = tool_call.args.get("timeout", self.default_timeout)
        workdir = tool_call.args.get("workdir", "/workspace")

        host_workspace = self.workspace_dir
        container_workspace = "/workspace"

        extra_folders = []
        if self.config:
            extra_folders = self.config.get("exec.folders") or []

        volume_args = ["-v", f"{host_workspace}:{container_workspace}"]
        for folder in extra_folders:
            mount_name = os.path.basename(folder.rstrip("/\\")) or "folder"
            volume_args += ["-v", f"{folder}:/mnt/{mount_name}:ro"]

        docker_cmd = [
            self.runtime,
            "run",
            "--rm",
            *volume_args,
            "-w",
            workdir,
            self.image,
            "bash",
            "-lc",
            command,
        ]

        logging.info("[exec] Запуск команды: %s", command)
        logging.info("[exec] %s cmd: %s", self.runtime, " ".join(docker_cmd))

        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            err = f"{self.runtime} not found. Установи {self.runtime} и добавь его в PATH."
            logging.error("[exec] %s", err)
            return {"error": err}
        except subprocess.TimeoutExpired:
            err = f"Команда превысила таймаут {timeout} секунд и была прервана."
            logging.error("[exec] %s", err)
            return {"error": err}
        except Exception as e:
            err = f"Ошибка при запуске {self.runtime}: {e}"
            logging.error("[exec] %s", err)
            return {"error": err}

        logging.info("[exec] exit_code=%d", proc.returncode)
        if proc.stdout:
            logging.info("[exec] stdout:\n%s", proc.stdout.rstrip())
        if proc.stderr:
            logging.warning("[exec] stderr:\n%s", proc.stderr.rstrip())

        result = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        return result

