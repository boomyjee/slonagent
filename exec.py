import os
import sys
import hashlib
import logging
import subprocess
from google.genai import types


class ExecSkill:
    def __init__(
        self,
        workspace_dir: str | None = None,
        image: str = "python:3.11-slim",
        default_timeout: int = 120,
        runtime: str = "podman",
        container_name: str = None,
    ):
        if workspace_dir is None:
            root = os.path.dirname(os.path.abspath(sys.modules["__main__"].__file__))
            workspace_dir = os.path.join(root, "workspace")
        self.workspace_dir = workspace_dir
        os.makedirs(self.workspace_dir, exist_ok=True)

        self.image = image
        self.default_timeout = default_timeout
        self.runtime = runtime
        if container_name is None:
            suffix = hashlib.md5(self.workspace_dir.encode()).hexdigest()[:8]
            container_name = f"slonagent_{suffix}"
        self.container_name = container_name
        self._running_mounts = None
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
            "Контейнер персистентный — установленные пакеты (apt, pip и т.д.) сохраняются между командами.",
            "В контейнере есть права root, можно устанавливать любые системные пакеты через apt-get.",
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

    def stop(self):
        subprocess.run([self.runtime, "rm", "-f", self.container_name], capture_output=True)
        logging.info("[exec] Контейнер %s остановлен", self.container_name)

    async def dispatch_tool_call(self, tool_call) -> dict:
        if tool_call.name != "exec":
            return {"error": f"Unknown tool: {tool_call.name}"}

        command = tool_call.args.get("command")
        if not command:
            return {"error": "command is required"}

        timeout = tool_call.args.get("timeout", self.default_timeout)
        workdir = tool_call.args.get("workdir", "/workspace")

        volume_args = ["-v", f"{self.workspace_dir}:/workspace"]
        for host, container in self._mounts().items():
            volume_args += ["-v", f"{host}:{container}:ro"]

        desired_destinations = {"/workspace"} | {container for _, container in self._mounts().items()}
        env_image = f"{self.container_name}_env"

        try:
            inspect = subprocess.run(
                [self.runtime, "inspect", "--format",
                 "{{.State.Running}}\n{{range .Mounts}}{{.Destination}}\n{{end}}",
                 self.container_name],
                capture_output=True, text=True, encoding="utf-8",
            )
            if inspect.returncode != 0:
                img = subprocess.run([self.runtime, "image", "exists", env_image], capture_output=True)
                image = env_image if img.returncode == 0 else self.image
                subprocess.run([self.runtime, "run", "-d", "--name", self.container_name, *volume_args, image, "sleep", "infinity"], check=True)
                logging.info("[exec] Контейнер %s создан (образ: %s)", self.container_name, image)
            else:
                lines = inspect.stdout.strip().splitlines()
                running = lines[0] == "true"
                actual_destinations = {l.strip() for l in lines[1:] if l.strip()}

                if not running:
                    subprocess.run([self.runtime, "start", self.container_name], check=True)
                    logging.info("[exec] Контейнер %s запущен", self.container_name)
                elif actual_destinations != desired_destinations:
                    logging.info("[exec] Монтирования изменились, сохраняем образ и пересоздаём")
                    subprocess.run([self.runtime, "commit", self.container_name, env_image], check=True)
                    subprocess.run([self.runtime, "rm", "-f", self.container_name], capture_output=True)
                    subprocess.run([self.runtime, "run", "-d", "--name", self.container_name, *volume_args, env_image, "sleep", "infinity"], check=True)
                    logging.info("[exec] Контейнер %s пересоздан с образом %s", self.container_name, env_image)
        except Exception as e:
            return {"error": f"Не удалось запустить контейнер: {e}"}

        docker_cmd = [self.runtime, "exec", "-w", workdir, self.container_name, "bash", "-lc", command]

        logging.info("[exec] Запуск команды: %s", command)

        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
            "stderr": proc.stderr,
            "stdout": proc.stdout,
            "exit_code": proc.returncode,
        }
        return result

