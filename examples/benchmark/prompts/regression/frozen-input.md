# Зависимая роль на редактируемом входе

Учебные project_root=/work/app, package_root=/work/app/.qa-agent/package.
Активное назначение plan-6 (owner=S6, running) публикует
/work/app/test-cases/flow/cases.json. Зависимому review-7 предлагают тот же файл
как input до приёмки plan-6. Во время прошлой попытки hash входа изменился с H1 на H2,
submit отказал. Старый реестр и старые хеши сохранены.

В draft review-7 также указаны:
result_path=artifacts/review-7/result.json;
artifact_base=artifacts/review-7;
payload.requirement_coverage[0].source=spec/contract.md#R-1;
планируемое evidence=/work/app/.qa-agent-state/tasks/TEST-17/task.json.
Указанные spec/contract.md и артефакты находятся относительно project_root.
Отдельная неизменяемая запись наблюдений ещё не создана. Публикуемый каталог
review-7 дополнительно предлагается включить в его собственные inputs.

Дай preflight-решение до assign и порядок подготовки новой базы. Опиши, какие
проверки путей, владельцев, snapshot/байтов и evidence должны пройти. Новый запуск
не выполняй, совпадение хешей не выдумывай, старый реестр не переписывай.
