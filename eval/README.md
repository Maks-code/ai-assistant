# Eval (локальная автопроверка качества)

Цель: быстро и повторяемо проверять качество ассистента после изменений.

## Что проверяется
- доля успешных кейсов (`pass_rate`)
- доля fallback-ответов (`fallback_rate`)
- соответствие источников ожидаемому процессу/документам
- корректная обработка неоднозначных вопросов и уточнений

Примечание: для кейсов, где важна именно генерация LLM/fallback-семантика, можно указать `skip_when_stub_llm: true`, чтобы не учитывать их в `--stub-llm` прогоне.

## Структура
- `eval/cases.json` — набор тест-кейсов
- `scripts/run_eval.py` — скрипт прогона
- `eval/reports/` — отчеты прогонов (`latest.json`, baseline и т.д.)

## Запуск

### 1) Без сети/OpenAI (рекомендуется для регрессии retrieval)
```bash
.venv/bin/python scripts/run_eval.py --mode local --stub-llm --output eval/reports/latest.json --fail-on-errors
```

### 2) Через запущенный локальный сервер
```bash
.venv/bin/python scripts/run_eval.py --mode http --base-url http://127.0.0.1:8000 --output eval/reports/latest.json --fail-on-errors
```

## Baseline
Первый стабильный прогон сохраняйте отдельным файлом:
```bash
.venv/bin/python scripts/run_eval.py --mode local --stub-llm --output eval/reports/baseline-local.json
```

Дальше сравнивайте `latest.json` с baseline по полям:
- `summary.pass_rate`
- `summary.fallback_rate`
- `results[*].ok` / `results[*].reasons`
