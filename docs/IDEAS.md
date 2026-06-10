# IDEAS

Идеи для будущих улучшений.

## sandbox.exec — синхронизировать сигнатуру с Bash-тулом Claude Code

Сейчас модель часто срёт `# rationale`-комментариями внутри `command` — потому что в схеме `sandbox.exec` нет отдельного поля для описания. У Claude Code Bash-тул имеет required `description` (5-10 слов, active voice), модель туда складывает «зачем», а в `command` — чистая команда.

Нужно: добавить параметр `description: str` в `SandboxSkill.exec`, обязательный по схеме. Залоггировать рядом с командой (`[exec] <description>: <command>`). Параметр толкает модель не разводить `#`-комменты в shell.

Тонкость: в проекте ~25 тестовых вызовов `sb.exec(...)` без description — нужно либо сделать default `""` (мягкая версия), либо пройти по тестам и проставить description. Вариант 1 проще, вариант 2 строже соответствует Claude Code.

## tool-call interrupt — отмена одного тула, не всего ответа

Сейчас `/stop` рубит весь LLM-цикл. Иногда модель запускает не ту bash-команду в sandbox (заведомо долгую/неправильную) и хочется прервать ровно её, чтобы модель увидела `{"error": "interrupted"}` и продолжила разговор.

Что нужно:
- Sandbox `_run` перевести с `subprocess.run + asyncio.to_thread` на `asyncio.create_subprocess_exec` (чтобы `task.cancel()` действительно гасил host-процесс).
- Команды запускать в новой process-group (`setsid bash -c ...`), на отмене делать `podman exec ... kill -- -<pgid>` чтобы погасить всё дерево внутри контейнера.
- В `Agent`/`Skill.dispatch_tool_call` реестр активных тул-коллов `{tool_call_id: asyncio.Task}`.
- WS-событие `{type: 'transport', method: 'stop_tool', id}` + UI-кнопка на «тул-карточке».
- В скиллах ловить `CancelledError`, возвращать `{"error": "Прервано пользователем"}`, не пробрасывая исключение наверх (иначе LLM-цикл падает).

## Auth-cookie — подписанный session-токен вместо сырого хэша пароля

Сейчас кука `auth` хранит сам `password_hash`, и сервер пускает по `cookie == password_hash` (см. `WebTransportServer.auth_middleware` / `_with_ws_auth` в `src/transport/web/server.py`). То есть кука = долгоживущий пароль: утечёт через XSS/логи/шаринг URL — атакующий получает постоянный доступ, отозвать нельзя без смены пароля у всех.

Нужно: выдавать в куку не хэш, а подписанный токен — `HMAC(server_secret, user|issued_at)` (механизм уже есть рядом — `agent_token` / `_secret_seed`). Сервер проверяет подпись и срок, а не равенство паролю. Тогда:
- кука перестаёт быть самим креденшлом;
- появляется срок жизни и возможность ротации `server_secret` (= разлогинить всех);
- `password_hash` остаётся только для первичного входа (basic-auth / daily-token).

Тонкость: daily-token (`_make_auth_token`) и кросс-процессный agent-rpc токен завязаны на `password_hash` — при переходе свести их к единой схеме на `_secret_seed`, чтобы не плодить три формата токенов. Const-time сравнение (`_ct_eq`) уже на месте, его переиспользовать.
