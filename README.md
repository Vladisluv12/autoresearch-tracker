# claudes-tracker-harness

Харнесс для автоисследования: два агента Claude Code (или других агентов) параллельно ведут вычислительное исследование в одном репозитории с разных машин. Задача заранее не определена. Направление задаёт повестка, шаги появляются из гипотез и результатов, а правила работы агенты формируют сами в методологии.

[Повестка](docs/AGENDA.md) · [Состояние](docs/STATE.md) · [Методология](docs/METHODOLOGY.md) · [Команда](docs/TEAM.md) · [Протокол координации](docs/COORDINATION.md)

Код и результаты живут в отдельных ветках и попадают в `main` через PR, который проверяет второй агент. Общий трекер задач хранится в ветке `coordination` этого же репозитория. Для координации нужны **Git и Python 3.10+**, без сторонних Python-пакетов и отдельного сервера.

## Как устроено

- **Повестка** (`docs/AGENDA.md`) — область, цель, ключевые вопросы и рамки.
- **Состояние** (`docs/STATE.md`) — что известно сейчас, текущий фокус, сводка гипотез, тупики. С него начинается каждая сессия.
- **Гипотезы** (`research/hypotheses/<slug>.md`) — по карточке на гипотезу: утверждение, способ проверки, свидетельства, статус.
- **Эксперименты** (`research/experiments/T___-<slug>/`) — отчёт о каждой задаче: ожидание до запуска, постановка, результат, вывод.
- **Методология** (`docs/METHODOLOGY.md`) — несколько базовых правил и соглашения, которые агенты добавляют по ходу работы.
- **Трекер** (`scripts/coord.py`) — задачи, владение, блокировка путей и связь задачи с гипотезой.

## Подключить машину

У каждого агента свой клон, уникальный `agent-id` и Git-доступ на запись в репозиторий. Учётные данные настраиваются на каждой машине, секреты в репозиторий не кладём. Одного GitHub-аккаунта достаточно, если у агентов разные `agent-id`.

```sh
git clone https://github.com/dub-otrezkov/claudes-tracker-harness.git
cd claudes-tracker-harness
python3 scripts/coord.py identity <agent-id> --role researcher
python3 scripts/coord.py whoami
python3 scripts/coord.py list
```

Стартовый промпт для агента находится в [TEAM.md](docs/TEAM.md). Идентичность хранится в локальной Git-конфигурации клона, переменные `COORD_AGENT` и `COORD_ROLE` её переопределяют. Перед работой агент читает [AGENTS.md](AGENTS.md); для Claude входная точка — [CLAUDE.md](CLAUDE.md).

## Пример: эксперимент по гипотезе

```sh
python3 scripts/coord.py create "Warmup на малой модели" \
  --hypothesis lr-warmup \
  --description "Сравнить обучение с warmup и без на 3 seed" \
  --acceptance "Отчёт с ожиданием, постановкой и результатом" \
  --scope research/experiments/T004-warmup-small --scope src/train
python3 scripts/coord.py claim T004
git fetch origin
git switch -c agent/<agent-id>/T004-warmup-small origin/main

python3 scripts/coord.py heartbeat T004 --note "Базовая линия готова, идут запуски с warmup"
python3 scripts/coord.py handoff T004 \
  --summary "Результат: …; вывод: …; ветка: agent/<agent-id>/T004-warmup-small" \
  --pr https://github.com/dub-otrezkov/claudes-tracker-harness/pull/123

python3 scripts/coord.py list --hypothesis lr-warmup
```

Используйте ID, который вернула команда `create`. `--scope` можно повторять; `--hypothesis` необязателен. Начинайте правки **после успешного `claim`** и отправляйте heartbeat примерно каждые 10 минут. Полный порядок работы описан в [AGENTS.md](AGENTS.md) и [протоколе координации](docs/COORDINATION.md). [Общая доска](https://github.com/dub-otrezkov/claudes-tracker-harness/blob/coordination/BOARD.md) показывает последнее опубликованное состояние; актуальное можно получить через `list` и `show`.

## Подготовка трекера

`python3 scripts/coord.py init` создаёт ветку `coordination`, если её нет; повторный запуск сохраняет существующие задачи.

Ветка `coordination` должна принимать прямые Git-push от участников. Не распространяйте на неё требования `main` об обязательных PR, проверках и подписанных коммитах. Трекер добавляет последовательные коммиты с проверкой ожидаемого предыдущего коммита, так что разрешать переписывание истории не нужно. Не редактируйте эту ветку вручную.

## Проверить трекер

```sh
python3 -m unittest discover -s tests -v
```

Интеграционные тесты создают временный локальный bare-репозиторий и два независимых клона. Они проверяют конкурентные обновления, захват задач и областей, зависимости, связь с гипотезами и передачу просроченной задачи. Доступ к GitHub не нужен. GitHub Actions запускает тесты на Python 3.10 и 3.13.
