---
description: Установить QA-пакет в текущий проект одной командой или проверить его настройку
---

Пользователь поручил установить или настроить QA-пакет. Выполни подготовку сам
через shell-инструмент, без предложения вручную запускать Python. QA MCP не нужен.
Определи фактический абсолютный project_root текущего приложения; сохраняй cwd
проекта. package_root = <project_root>/.qa-agent/package.

Если install.json уже существует, не меняй версию пакета: выполни локальные init
и doctor ниже. При повреждении manifest, pending установки или существующей
package без install.json остановись с диагностикой по PROJECT_INSTALLATION.md;
не применяй bootstrap к уже установленному/незавершённому комплекту.

Для первой установки получи исходник из официального репозитория в уникальную
временную папку внутри проекта. Не исполняй Python из глобального extension или
снаружи проекта. До подготовки проверь, что .qa-agent и bootstrap.local не являются
симлинками и не являются обычными файлами. Не заменяй существующие файлы приложения.
Выполни из project_root:

```bash
qa_project_root="$(pwd -P)"
test ! -L .qa-agent && test ! -L .qa-agent/bootstrap.local &&
mkdir -p .qa-agent/bootstrap.local &&
qa_install_stage="$(mktemp -d "$qa_project_root/.qa-agent/bootstrap.local/install.XXXXXX")" &&
printf '*\n' > "$qa_install_stage/.gitignore" &&
git clone --depth 1 --single-branch https://github.com/dimavrb/QaAgentGigacodeCli.git "$qa_install_stage/source" &&
python3 "$qa_install_stage/source/scripts/qa_install.py" --project-root "$qa_project_root" --bootstrap --apply
```

Исполняемый установщик теперь находится внутри проекта, а целевая package —
отдельно от временного исходника. --bootstrap разрешает только первую установку;
незавершённые QA-задачи, конфликты и блокировки не обходятся. При ошибке сети или
проверки остановись и покажи причину, не создавай файлы пакета или реестр вручную.
При status=conflict установка не состоялась; сначала нужен разбор файлов.

После installed/unchanged либо для уже установленного комплекта выполни:
`python3 "<package_root>/scripts/qa_project.py" init --project-root "<project_root>"`
и `python3 "<package_root>/scripts/qa_project.py" doctor --project-root "<project_root>"`.
Покажи установленную версию и failed-проверки. Контекст и настройки сохраняются.
После первой установки предложи открыть новую сессию для загрузки проектных
ролей/skills/команд и проверить /agents manage, /skills, /help, /memory.
Не объявляй текущую сессию перезагруженной и не запускай QA-работу автоматически.
Обновление используемого пакета выполняется между сессиями; /qa:init его не заменяет.
Покажи одноразовую настройку shell-alias:
`alias qa-start='python3 .qa-agent/package/scripts/qa_start.py'`.
Дальше из корня приложения достаточно `qa-start`: он проверит обновление перед
запуском GIGACODE. Alias в shell-tool не меняет родительский терминал пользователя;
не объявляй его сохранённым и не редактируй домашний shell-профиль автоматически.
