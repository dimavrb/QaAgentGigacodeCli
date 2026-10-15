# Несколько авторов и общий HTML-план

```bash
python3 examples/test-plan-partition/build_demo.py --output-dir dist/previews/test-plan-partition-demo
```

В новой папке находятся принятая база scope/, снимок v2/partition.json,
контексты/назначения/каталоги двух групп и проверенная интеграция.
Итог — v2/integration/result.plan.html: все три требования и два UI-кейса
сохранились, один кейс связан с двумя требованиями.

Группы и итог проходят отдельные fixture-отзывы стандартного реестра;
GIGACODE и продукт не запускаются. Это пример передачи/валидации/сборки,
не замер качества живых авторов. Старые директории не перезаписываются.
Правила — [TEST_PLAN_PARTITION.md](../../docs/TEST_PLAN_PARTITION.md).
