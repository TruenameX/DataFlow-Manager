# DataFlow Manager

Учебное приложение для управления ETL-пайплайнами.

## Запуск сервера

Требуются Python 3.12 и uv. Выполняйте команды из корня репозитория.

```bash
uv run uvicorn dataflow_manager.main:app --reload
```

Сервер доступен по адресу http://127.0.0.1:8000.
`GET /health` возвращает HTTP 200, JSON `{"status": "ok"}`
и Content-Type `application/json`, без обращения к базе данных или внешним сервисам.

## Запуск тестов

```bash
uv run pytest
```
