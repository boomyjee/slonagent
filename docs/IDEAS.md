# IDEAS

Идеи для будущих улучшений.

## БАГ:Cron в multi-thread форке
CronSkill регистрируется как обычный skill, поэтому каждый Agent в форке (parent + uuid-треды) получает свой инстанс. CRON.json лежит в `memory_dir` (per-fork, не per-thread) — все инстансы тикают одновременно и читают/пишут один файл

## Показывать инкремент и новые наблюдения в компрессоре

## Хранить историю для веб-транспорта

## Веб-сервер - вся песочница, а не только web

## Объединить host↔container коммуникацию через один канал sandbox-proxy
1. **Skill-скрипты** ([src/skills/sandbox/__init__.py](../src/skills/sandbox/__init__.py) 
2. **Sandbox-proxy** ([src/skills/sandbox/container_lib/sandbox_proxy.py](../src/skills/sandbox/container_lib/sandbox_proxy.py))

