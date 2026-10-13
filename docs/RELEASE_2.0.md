# Поставка QA-агента 2.0

Дата: **2026-10-09**. Архив: `qa-agent-2.0.zip`, checksum: `qa-agent-2.0.zip.sha256`. Внутренний PACKAGE_MANIFEST.json фиксирует версию, дату, размер и SHA-256 каждого файла. Поставка содержит **13 специализированных ролей**, **5 skills** и команду `/qa`.

## Изменения относительно 1.9

- Критерии всей задачи, проверяемая готовность зависимостей, отдельное содержательное принятие и trace-события. Новые задачи требуют `init --criteria` либо явно выбранной совместимости; валидный отчёт не закрывает критерий автоматически.
- Managed supervisor: собственные процессы/владельцы, heartbeat, сохранённые checkpoints, отмена, ограниченные повторы и восстановление. MCP gateway ограничивает инструменты и параметры; неоднозначная запись требует readback до повтора.
- `qa-result-reviewer`, общий `qa-check-quality` и ограниченный цикл автора с одним/двумя направлениями ревью. Проверяются источник ожидания, различающее наблюдение, выполнение существенных сценариев и точная ревизия.
- `qa-conclusion-reviewer`: проверка значимых полей ответа по первичным источникам, полнота conclusion_checks и точный target. Facts/inference сначала независимы; при расхождении возможны до двух раундов обсуждения. Смена позиции требует доказательства, исходное blocking не стирается, неразрешённый спор блокирует приёмку.
- Новое исполнение в ревью: собственная gateway_policy, обязательные execution_checks и свежие OBS-квитанции реальных локальных вызовов. Старый лог или кеш записи не засчитываются как новый запуск.
- Усиленная приёмка комбинаций: модель/ограничения, математическое покрытие и полезные scenario_bindings каталога оцениваются отдельно. Один общий кейс или общий passed не подтверждают набор комбинаций.
- HTML-ответы ожидаются до 20 минут с проверкой сразу и затем раз в 60 секунд; применение связано с конкретной ревизией вопросов. Timeout и неизвестный ответ сохраняют незавершённость.
- Компактная сборка контекста: разделы по роли, обязательные предметные источники, дедупликация входов/ответов/зависимостей, адресный feedback с сохранением полного аудита. Явный лимит останавливает запуск без обрезки доказательств. Метрики контекста/времени/CLI usage учитывают все попытки; неизвестный расход остаётся null.
- Короткий QWEN.md, подробные процедуры координатора отдельно. Статическое сравнение тех же восьми назначений сократило явный текст на 52,3%, QWEN.md на 68,1%; символы не равны токенам и не доказывают улучшения качества.
- Профильные `qa-test-data` и `qa-stub-contract` включены как кандидаты для явного пилота. Вместе с `qa-test-cases`, `qa-browser-repro`, `qa-check-quality` — пять skills. Кандидаты не включены по умолчанию в managed-профили до подтверждения пользы.

HTML-форма, self_review, Playwright-пул, заглушки, расследование, отчётность, журнал и данные сохраняются. Старые выпуски остаются для отката; архив 1.9 не заменяется.

## Установка и совместимость

Первая установка — INSTALL.md/IMPORT_INTO_APP.md/SETUP_AND_UPDATE.md; переход с 1.9 — UPGRADE_2.0.md и UPDATE.md. Переносите весь пакет и подключённые копии всех ролей/skills/команды; в проекте приложения адаптируйте доступные пути. Пакет предназначен для Qwen Code, Python 3.10+ и POSIX: macOS/Linux, Windows через WSL. Встроенная конфигурация Playwright MCP сохраняет pin **0.0.83**; соединения sbermock/Jira, модель и секреты задаются вашей установкой.

Контракты сообщений сохраняют schema_version; новые проверки и поля включаются назначением/критерием. Исторические отчёты не переписываются. Старый frozen execution без context_profile сохраняет прежние инструкции; после изменения их хешей продолжение требует нового плана и проверки внешнего состояния. Не перенормализуйте старые планы ради обхода блокировки. Runtime-реестры, ответы, gateway-журналы, browser_pool и доказательства хранятся вне заменяемого пакета.

ZIP содержит .qwen, docs, scripts, tests, examples и корневые инструкции. Git/IDE/cache, dist, artifacts, персональные пути и runtime-состояния в поставку не входят. Примеры и тестовые CLI — синтетические материалы, не реальные результаты продукта. Файл VALIDATION_2.0.json находится рядом с ZIP и не включается внутрь: он фиксирует checksum уже собранного архива.

## Проверки распакованной поставки

Из корня новой копии qa-agent:

```bash
python3 scripts/package_agent.py --verify /absolute/path/qa-agent-2.0.zip
python3 -m unittest discover -s tests -v
python3 scripts/validate_test_cases.py --input examples/test-cases.json
python3 -m unittest discover -s examples/test-data -v
python3 scripts/validate_qa_result.py --input examples/task-exchange/result.json --task-id QA-EXCHANGE --request-id QA-EXCHANGE-env-1 --agent qa-test-env-configurator --require-self-review --self-review-assignment examples/task-exchange/assignment.json
python3 scripts/questionnaire.py build --input examples/questions/questions.json --output artifacts/questions/release/questions.html
```

Ожидаются **508 тестов пакета** и один отдельный учебный тест данных. Полный набор включает реальный запуск собственных локальных fixture-процессов, POSIX-идентификацию/отмену и stdio MCP; среде нужны эти локальные возможности. Тесты не вызывают живую модель, Jira или продуктовый стенд.

Учебный критерий/реестр до finish — examples/task-exchange/README.md. Отдельный пример обсуждения:

```bash
python3 examples/review-discussion/build_demo.py --output-dir /tmp/qa-agent-2.0-discussion
python3 scripts/manage_review_cycle.py --state /tmp/qa-agent-2.0-discussion/cycle.json init --plan /tmp/qa-agent-2.0-discussion/plan.json
python3 scripts/manage_review_cycle.py --state /tmp/qa-agent-2.0-discussion/cycle.json serve --max-seconds 30
```

Для повтора используйте новый каталог. Ожидается accepted после первичного расхождения, одного обсуждения и исправления автора; это приёмка артефакта, assess/finish всей задачи остаются отдельными. Другие примеры — examples/execution, review-cycle, review-execution, conclusion-review, test-design и benchmark. Reasoning/profile benchmark при недоступном CLI фиксирует unavailable, не успех.

Рядом с ZIP сохранены RELEASE_2.0.md, UPGRADE_2.0.md, INSTALL_2.0.md, UPDATE_2.0.md, IMPORT_INTO_APP_2.0.md, SETUP_AND_UPDATE_2.0.md и VALIDATION_2.0.json. Проверка конкретной сборки включает тесты исходников и распакованной копии, соответствие файлов манифесту/исходникам, checksum, воспроизводимость и локальные CLI.

## Пределы проверки

Локальные тесты подтверждают механику, не качество суждений живой модели. Qwen CLI в среде сборки недоступен. После установки отдельно проверяйте фактическую модель, поддержку обязательных managed flags, права gateway и доступность источников. Реальные Playwright/sbermock/Jira, продуктовый CI/Allure и A/B токенов/скорости не проверены этой упаковкой. Не объявляйте продуктовую готовность по успешному синтетическому демо.
