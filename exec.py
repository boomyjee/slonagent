import os
import sys
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
    ):
        if workspace_dir is None:
            root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
            workspace_dir = os.path.join(root, "workspace")
        self.workspace_dir = workspace_dir
        os.makedirs(self.workspace_dir, exist_ok=True)

        self.image = image
        self.default_timeout = default_timeout
        self.runtime = runtime
        self.agent = None

        self.tools = [
            types.FunctionDeclaration(
                name="exec",
                description=(
                    "Выполнить команду внутри Docker-контейнера. "
                    "Всегда доступна директория /workspace. "
                    "Папки хост-машины монтируются по WSL-схеме: C:\\\\foo → /mnt/c/foo."
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

    def _mounts(self) -> dict[str, str]:
        from config import ConfigSkill
        config = next((s for s in self.agent.skills if isinstance(s, ConfigSkill)), None) if self.agent else None
        folders = config.get("exec.folders") or [] if config else []
        result = {}
        for f in folders:
            if len(f) >= 2 and f[1] == ":":
                drive, rest = f[0].lower(), f[2:].replace("\\", "/").lstrip("/")
                result[f] = f"/mnt/{drive}/{rest}"
            else:
                result[f] = f.replace("\\", "/")
        return result

    def resolve_path(self, container_path: str) -> str | None:
        if container_path.startswith("/workspace/"):
            return os.path.join(self.workspace_dir, container_path[len("/workspace/"):])
        for host, container in self._mounts().items():
            prefix = container.rstrip("/") + "/"
            if container_path.startswith(prefix):
                return os.path.join(host, container_path[len(prefix):].replace("/", os.sep))
        return None

    def get_context_prompt(self) -> str:
        lines = [
            "## Инструмент exec",
            "Ты можешь выполнять команды в Docker-контейнере.",
            "Директория /workspace всегда доступна для чтения и записи.",
        ]
        mounts = self._mounts()
        if mounts:
            lines.append("Дополнительно примонтированы папки хост-машины:")
            for host, container in mounts.items():
                lines.append(f"  - {host}  →  {container}  (только чтение)")
        else:
            lines.append(
                "Дополнительных папок нет. Если нужен доступ к папке на хост-машине, "
                "попроси пользователя выполнить:\n"
                "  /config write exec.folders[] <абсолютный путь к папке>"
            )
        return "\n".join(lines)

    async def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name != "exec":
            return {"error": f"Unknown tool: {tool_call.name}"}

        command = tool_call.args.get("command")
        if not command:
            return {"error": "command is required"}

        timeout = tool_call.args.get("timeout", self.default_timeout)
        workdir = tool_call.args.get("workdir", "/workspace")

        host_workspace = self.workspace_dir
        container_workspace = "/workspace"

        volume_args = ["-v", f"{host_workspace}:{container_workspace}"]
        for host, container in self._mounts().items():
            volume_args += ["-v", f"{host}:{container}:ro"]

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

