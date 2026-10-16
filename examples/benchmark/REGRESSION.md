# Регрессионные eval-задания

regression-suite.json — 17 синтетических заданий, 68 критериев независимой
оценки. Он дополняет прежние наборы, не переписывая их snapshots и результаты.
Задания находятся в prompts/regression; полный контракт и наблюдения каждого
сценария передаются в одном prompt. Рубрика suite доступна только оценщику.

| Задание | Роль/контур | Проверяемое решение |
| --- | --- | --- |
| stored-value | qa-test-plan-writer | Допустимые различающие данные и независимый эталон |
| observation-review | qa-result-reviewer | Неизменность, попытка записи и причина не смешаны |
| fresh-snapshot | qa-test-plan-writer | Контроль после нужного commit со свежим снимком |
| concise-rewrite | qa-test-plan-writer | Краткость с сохранением всех критериев и данных |
| good-case | qa-result-reviewer | Достаточный хороший кейс не блокируется лишними требованиями |
| fragment-contract | qa-result-reviewer | Контрактный contains/fragment не подменён exact |
| interrupted-result | Координатор | 429 против живого хода без файла; сохранение работы, слот и owner |
| frozen-input | Координатор | Пересечения владельцев, snapshots, абсолютные пути, immutable evidence |
| questionnaire-modes | Координатор | build/serve/wait, ручной ответ и общий комментарий при deferred |
| review-contract | qa-result-reviewer | Boolean blocking, self_review и передача недоступной валидации |
| environment-partial | qa-test-env-configurator | Полнота гайда, текущая версия и готовое подмножество |
| defect-cause | qa-defect-investigator | Подтверждённый факт независимо от неподтверждённой причины |
| automation-oracle | qa-test-automation | Assert ловит пустую, чужую и лишнюю выдачу |
| jira-uncertain | qa-jira-defect-creator | Timeout создания не разрешает дублирующий create |
| report-matrix | qa-test-report-writer | Полная матрица scope и честные not_run/blocked/pending |
| impact-fallback | qa-impact-analyst | Неполнота расширяет регресс, пробел покрытия остаётся |
| conclusion-evidence | qa-conclusion-reviewer | Символы, fixture и captured не доказывают экономию/качество |

Аналитик, feature-tester, autotest-reviewer, data-builder и stub-builder уже
представлены в прежних suite/quality/profile-наборах. Вместе наборы содержат
хотя бы одно задание для каждой из 14 ролей. Для заявления качества отдельной
роли нужны дополнительные независимые случаи и holdout, а не один удачный ответ.

## Передаваемые инструкции

Поле agent задаёт зарегистрированную роль; отсутствие сохраняет координаторский
маршрут или прежний ROLES для старых case_id. Для manual cases test_type задаёт
api/ui/kafka: runner передаёт стандарт, протокол ревью, индекс и один валидный
JSON-пример этого типа. Тип не выводится из рубрики. Для старых suite без поля
пример по догадке не добавляется; новые задания должны задавать канал явно.

instruction_paths содержит только применимые Markdown из docs пакета, например
COORDINATION_SAFETY или QUESTIONS_UI. Это явное расширение контекста задания,
без загрузки всех документов каждому исполнителю. Рубрики, внешние файлы,
неизвестные роли/типы, пустые prompts и выход через симлинк из suite отклоняются
до запуска CLI. Снимки реально переданного текста сохраняются в instructions.json
и system.md; изменение инструкций требует новых запусков, не пересчёта старых хешей.

Reasoning-контекст выбирает разделы стандартов по предмету задания. Для кейса
с заданным каналом не загружается процедура полного тест-плана, а для прочих
ролей — стандарт всех UI/API/Kafka-кейсов. Общая self-review проецируется на роль;
явно запрошенные instruction_paths остаются полными. Примеры JSON не обрезаются.
Это новый профиль инструкций, а не эквивалентность старому контексту: влияние
на качество нужно измерить отдельным сопоставимым запуском.

Размер проверяется в UTF-8 байтах: Linux ограничивает один аргумент execve
32 страницами ([исходник ядра](https://github.com/torvalds/linux/blob/master/include/uapi/linux/binfmts.h)).
Тест сохраняет совместимость поставляемых наборов с 4-КБ страницами. Для
stored-value system уменьшен с 211954 до 121411 байт; это размер текста, не токены
и не доказанная экономия модели. Произвольные дополнительные docs могут увеличить
argv: перед новым suite проверяйте и ограничения реального runtime.

## Запуск и независимая оценка

Для установленного и проверенного runtime с фактически выбранной моделью:

```bash
python3 scripts/run_agent_benchmark.py --suite examples/benchmark/regression-suite.json --output-dir artifacts/benchmarks/regression-baseline --model YOUR_MODEL --repeats 3 --max-wall-time 5m
```

Это reasoning_only: файлы не пишутся моделью, инструменты запрещены, артефакты
представляются в ответе. Программный CLI запускается только с
--measurement-kind fixture. Отсутствующий CLI даёт unavailable; fixture не
измеряет качество/стоимость модели, а capture оставляет semantic_quality=not_reviewed.

После capture:

```bash
python3 scripts/make_benchmark_grading_pack.py --run artifacts/benchmarks/regression-baseline --output artifacts/benchmarks/regression-grading
python3 scripts/check_benchmark_grading_pack.py --pack artifacts/benchmarks/regression-grading --run artifacts/benchmarks/regression-baseline
```

Далее применяй [BENCHMARK_EVIDENCE_GATES.md](../../docs/BENCHMARK_EVIDENCE_GATES.md):
независимый tool-free grader, цитаты текущих response, проверка файлов и приёмка
каждого trial, затем командная сводка. Все 68 критериев каждого повтора обязательны;
not_checked/неполный trial не закрывают этап. Для сравнения направлений нужны
сопоставимые условия и measured dispersion на тех же фиксированных ответах.

Для краткости не задаётся квота слов. Оценщик проверяет, можно ли выполнить шаг
и вынести pass/fail без догадок, сохранились ли отрицания, значения, источники,
корреляция и границы ожидания. Длинное необходимое пояснение не является ошибкой.
Не оценивай ответ только совпадением ключевых слов: «не было попытки» может быть
как ошибочным выводом, так и цитатой критического замечания.

Набор не тестирует живое сохранение фоновой сессии, enforced guard, межсессионный
limiter или серверные квоты. Их гарантии требуют реализации runtime и отдельных
инъекций сбоя. Результаты текущего аудита — локальные тесты механики; живой CLI
недоступен, оценок качества модели пока нет.
