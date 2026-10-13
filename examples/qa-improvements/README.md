# Учебный полный маршрут улучшений

```sh
python3 examples/qa-improvements/build_demo.py --output-dir /tmp/qa-improvements-demo
python3 scripts/qa_improvements.py verify --report /tmp/qa-improvements-demo/shadow.json
python3 scripts/qa_improvements.py verify --report /tmp/qa-improvements-demo/schedule.json
```

Новый каталог содержит frozen sources, coverage.json, обновлённый impact-input,
selection, execution-policy/schedule, отдельные full/selected results с планами и
полными trace, shadow и слепую public-копию контроля оценщика. Это fixture, не запуск
GIGACODE/продукта; даты, длительности и результаты учебные. Пилот не разрешает выпуск.
Полный формат и команды — docs/QA_IMPROVEMENTS.md.

Контроль оценщика запускайте отдельно через controls-run. Предварительные gold
хранятся в examples/grader-controls/suite.json; сначала нужны экспертное ревью и
версионирование. Оценщик получает только controls-public.json.

Локальный исполнитель требует собственный commands.json с точными argv/cwd,
timeout, политикой безопасных повторов, run/build/environment/data и хешем input.
Тесты tests/test_qa_improvements.py исполняют реальные локальные fixture-процессы:
два теста используют общий ресурс, один сначала падает, затем проходит. Отдельно
проверяется timeout. Исполнитель сохраняет обе попытки и не объявляет QA принятым.
