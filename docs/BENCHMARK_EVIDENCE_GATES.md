# Гейты доказательности оценки QA-агента

Контроль смысла verdict дополняет проверку происхождения цитат: слепые пары
корректных/ошибочных ответов и ошибки одобрения/блокировки описаны в
QA_IMPROVEMENTS.md. Предварительная учебная разметка требует экспертного ревью.

Штатный benchmark-маршрут использует команды пакета. Уведомление исполнителя означает необходимость прочитать файл. Оно не является источником verdict, числа или завершённости. Формат первичных данных и все ожидаемые записи проверяются до итоговых формулировок. Совпадение цитаты подтверждает её происхождение, содержательный смысл оценки требует отдельного ревью.

## Штатный маршрут

1. run_agent_benchmark.py сохраняет prompt/system/raw/response, execution и метрики. Пустой финальный ответ получает failed, доступный usage сохраняется независимо от пригодности ответа.
2. make_benchmark_grading_pack.py одной командой копирует весь набор вне каталога прогона: suite/prompts, все trial-N/case/{prompt,system,response}.md и шаблоны ведомостей. manifest.json закрепляет SHA-256, ID, число повторов и проверок (20 на повтор для suite.json). Неполный/проваленный захват не становится пакетом успешного исполнения.
3. check_benchmark_grading_pack.py одной командой сверяет копии и оригиналы. Пакет не раскрывает абсолютный адрес оригинального прогона. Его каталог получает только координатор, оценивающая роль получает копии.
4. run_benchmark_grader.py передаёт только пакет и рубрику новой bare-сессии GIGACODE. Filesystem/shell/discovery/delegation, память и MCP отключены, cwd пустой. Нет чужого verdict или уведомления. Несовместимые флаги блокируют запуск. Это граница инструментов модели в штатном GIGACODE-маршруте, не изоляция произвольного доверенного CLI-адаптера на уровне ОС.
5. accept_benchmark_review.py читает сохранённый файл по абсолютному пути внутри пакета. Проверяет схему, все ID и число записей, принадлежность trial, хеши, метрики, цитаты, язык notes и исключения. Ошибка даёт rc 2 без приёмки. Успешная файловая приёмка сохраняет квитанцию и точную зеркальную ведомость, fail/not_checked остаются явными.

Для live-пакета также обязательна квитанция run_benchmark_grader: точные хеши prompt/raw/ведомости, validated, нулевой exit и отключённые инструменты. Приёмка сверяет сохранённые checks с raw-ответом оценивающего. Ведомость, вручную перенесённая в пакет из другого маршрута, не получает приёмку live. Fixture-пакеты проверяют механику без заявления об исполнении живой роли.
6. build_benchmark_summary.py вычисляет числа из первичных execution/raw/response и принятых оценок. check_benchmark_summary.py пересчитывает результат. Назначение реестра с evidence_session_id требует benchmark_summary_artifact и точного совпадения summary с summary_text. validate_qa_result.py/submit отклоняют готовое событие при открытом этапе. Свободный текст с числами не заменяет сводку.

```bash
python3 scripts/make_benchmark_grading_pack.py --run /absolute/run --output /absolute/grading-pack
python3 scripts/check_benchmark_grading_pack.py --pack /absolute/grading-pack --run /absolute/run
python3 scripts/run_benchmark_grader.py --pack /absolute/grading-pack --trial 1 --output /absolute/grading-pack/trial-1/review-eval1.json --model <grader-model>
python3 scripts/check_benchmark_review.py --suite /absolute/grading-pack/suite.json --input /absolute/grading-pack/trial-1/review-eval1.json --grading-pack /absolute/grading-pack
python3 scripts/accept_benchmark_review.py --run /absolute/run --pack /absolute/grading-pack --input /absolute/grading-pack/trial-1/review-eval1.json --session-id <coordinator-session>
python3 scripts/build_benchmark_summary.py --run /absolute/run --session-id <coordinator-session> --output /absolute/new-summary.json
python3 scripts/check_benchmark_summary.py --input /absolute/new-summary.json --session-id <coordinator-session>
```

Повторите grading/accept для каждого trial. Числа берутся из stdout команды этой evidence-сессии и её файла. session-id задаёт координатор, а не автор. Это привязка к маршруту, не криптографическое подтверждение личности оператора. Снимки неизменны, старые/изменённые квитанции не закрывают этап.

## Цитаты и нормативы

Каждое pass/fail evidence — дословный фрагмент response.md соответствующего case. Разделители `…` и `...` допускают несколько частей. Пробелы схлопываются, регистр/пунктуация сохраняются. Все части длиннее 12 символов должны присутствовать, иначе rc 2. Только короткие части тоже отклоняются. Объяснение хранится в note, дополнительные ключи допустимы.

Для старых ведомостей есть --verify-quotes с каталогом trial либо прогона. Для полного прогона нужен grading_trial или расположение ведомости в trial-N. rc 0/1 соответствует проверенным pass/fail/not_checked, rc 2 — подставной/чужой цитате. Это аудит, не разрешение закрыть этап: штатная приёмка и сравнение требуют пакетную квитанцию.

Утверждение об отсутствии норматива требует missing_sources с конкретными именами документов. Если их заголовки «Источник инструкций» присутствуют в system.md, утверждение отклоняется. Простое упоминание документа не считается передачей его тела. Benchmark-контекст явно включает AUTOTEST_NAMING.md и AUTOTEST_QUALITY.md для автоматизации/reviewer. Не сводимые к механическому правилу смысловые заявления требуют первичного источника и независимого содержательного ревью.

## Разброс и сравнение

Повторно оцените фиксированный пакет в независимых сессиях, без предыдущего verdict. Каждая оценка содержит все trial/факторы, без not_checked. Конфигурация:

```json
{"schema_version": 1, "pack_path": "/absolute/grading-pack", "evaluations": [
  {"evaluation_id": "E1", "review_paths": ["/absolute/grading-pack/trial-1/review-eval1.json"]},
  {"evaluation_id": "E2", "review_paths": ["/absolute/grading-pack/trial-1/review-eval2.json"]}
]}
```

Для нескольких trial в каждой review_paths перечислите все повторы. measure_benchmark_dispersion.py вычисляет разброс числа проходов и максимальное число расходящихся факторов. Порог заранее закреплён в коде: max(максимальное наблюдаемое расхождение, две выборочные сигмы числа проходов). Это консервативная политика, не заявление о статистической значимости или равенстве моделей. Малое число оценок ограничивает надёжность.

```bash
python3 scripts/measure_benchmark_dispersion.py --input /absolute/dispersion-input.json --session-id <coordinator-session> --output /absolute/new-dispersion.json
python3 scripts/build_benchmark_summary.py --run /absolute/baseline --candidate /absolute/candidate --changed-dimension model_requested --calibration /absolute/dispersion-input.json --session-id <coordinator-session> --output /absolute/new-comparison.json
```

Все условия, кроме одного объявленного фактора, совпадают. Калибровка использует точные ответы одной из сторон и ту же рубрику/шкалу. Только превышение порога разрешает направление по проходам. Иначе текст строго «различий не обнаружено» с ценой различия в факторах/процентах. Без разброса сравнение блокируется. compare_profile_skills.py проверяет пакетные цитатные оценки, живые gains без calibration/session-id получают pending_dispersion.

## Формат, язык и завершение

Неизвестная схема требуемого входа даёт rc 2, неразобранный вход не разрешает stage_closed. captured означает пригодный захват, validated — файл после гейтов, stage_closed — полную оценку всех факторов. quality_passed отдельно требует все pass. Закрытая оценка может выявить ошибки.

Русские notes отклоняются при посторонней письменности или смешанном кириллическом/латинском слове. Дословный evidence дефекта допускается при literal_defect_quote=true и успешном цитатном гейте. Исключение не распространяется на notes. check_russian_artifact.py проверяет отдельные текстовые файлы. Исключение задаёт конкретные start/end/quote/source_path/source_sha256 и независимый primary-source-root, а не весь документ.

JSON-файлы сначала декодируются, поэтому Unicode-экранирование не обходит проверку. Для их исключений дополнительно нужен pointer к строке; start/end относятся к декодированному значению этой строки.

Сводка сканирует все response обеих сторон и сохраняет проблемы письменности по источникам. model_requested остаётся запрошенной моделью. Время, символы и токены вычисляются отдельно, неизвестный usage остаётся null. Пустой ответ получает failed с сохранением токенов и сырого envelope.

Контрольные случаи — tests/test_benchmark_evidence.py. Артефакты A0 из другого проекта не перепроверены. Фикстуры подтверждают механические отказы, не качество живой модели.
