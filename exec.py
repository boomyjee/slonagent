import os
import logging
import subprocess
from google.genai import types


class ExecSkill:
    """
    Скилл для выполнения команд внутри Docker-контейнера.

    По умолчанию в контейнер пробрасывается директория workspace.
    """

    def __init__(
        self,
        workspace_dir: str | None = None,
        image: str = "python:3.11-slim",
        default_timeout: int = 60,
        runtime: str = "podman",
    ):
        root = os.path.dirname(os.path.abspath(__file__))
        self.workspace_dir = workspace_dir or os.path.join(root, "workspace")
        os.makedirs(self.workspace_dir, exist_ok=True)

        self.image = image
        self.default_timeout = default_timeout
        self.runtime = runtime

        self.tools = [
            types.FunctionDeclaration(
                name="exec",
                description="Выполнить команду внутри Docker-контейнера с примонтированной директорией workspace.",
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

        docker_cmd = [
            self.runtime,
            "run",
            "--rm",
            "-v",
            f"{host_workspace}:{container_workspace}",
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

