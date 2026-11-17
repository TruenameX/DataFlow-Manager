# DataFlow Manager v0.1 — передача контекста

Актуализировано 17.11.2026. Это навигационная сводка, а не замена первичным источникам: [AGENTS.md](AGENTS.md), [PRD](docs/PRD.md), [архитектура и ADR](docs/ARCHITECTURE.md), [backlog](docs/BACKLOG.md) и [принятая схема TASK-002](docs/TASK-002.md). Фактическое состояние Git и стенда всегда проверять заново.

## Продукт и архитектура

Учебный сервис управления конфигурациями PostgreSQL-источников для ETL. Целевая архитектура — модульный монолит FastAPI с отдельными слоями API, сервисов, repository и криптографии, служебной PostgreSQL и миграциями Alembic. Сетевые проверки цели ограничиваются allowlist, TLS verify-full и согласованным бюджетом; подробности и открытые решения — в PRD и архитектуре. Это целевая архитектура: сейчас production API содержит только `GET /health`, возвращающий `200`, JSON `{"status":"ok"}`; Session/UoW и lifespan приняты; CRUD, repository, CryptoService и connector ещё не реализованы.

Python проекта 3.12 (проверен 3.12.6), uv; PostgreSQL-целевой профиль 18.6, UTF8, builtin `pg_unicode_fast` version 1. Проверенные версии: SQLAlchemy 2.0.57, Alembic 1.20.1, psycopg 3.3.8, greenlet 3.5.6, FastAPI 0.143.0, Pydantic 2.14.1, Starlette 1.7.0, HTTPX 0.28.1, pytest 9.1.1. Точные зависимости и граф — [pyproject.toml](pyproject.toml) и [uv.lock](uv.lock); переустановка в новом чате не предполагается.

## Репозиторий и статусы

- `docs/` — PRD, архитектура/ADR, backlog и декларативная спецификация TASK-002.
- `src/dataflow_manager/main.py` — существующий health endpoint; `src/dataflow_manager/db/models.py` — SQLAlchemy metadata модели.
- `migrations/` и `alembic.ini` — Alembic; `docker/` — локальный PostgreSQL-стенд; `tests/` — offline-модель, миграции и health.
- TASK-001 — **Merged**; TASK-002 — **AGREED**; TASK-003 и TASK-004 — **ACCEPTED**. Приёмка TASK-004 владельцем — 11.11.2026. Статусы и зависимости сверять по [backlog](docs/BACKLOG.md#6-task-summary).

TASK-005 и её дочерние задачи ACCEPTED; B08 v1.6 APPROVED. Следующий блок — TASK-006/007 repository; реальные PostgreSQL-тесты требуют отдельно разрешённого изолированного стенда. TASK-008 зависит от открытого SPIKE-005/B04, API от соответствующих зависимостей.

## Принятые решения и схема

B05 — целевой [API и безопасные ошибки](docs/ARCHITECTURE.md#4-api-design): `/health`, CRUD `/api/v1/sources`, check только для сохранённого источника; ошибки и check-result отделены, секреты не выводятся. B05 — утверждённый контракт, а не реализованные routes.

B06 — original `name` сохраняется; PostgreSQL вычисляет STORED `name_key` как `NFC(casefold(NFC(name)))` с `pg_catalog.pg_unicode_fast`, готовый ключ сравнивается `COLLATE "C"`, UNIQUE — `uq_data_sources_name_key`. API не принимает/возвращает ключ. Диагностика конфликта требует SQLSTATE 23505 и точного имени ограничения с проверкой schema/table, когда доступны; неизвестные ошибки не маскируются. Ограниченная матрица не доказывает все Unicode-случаи.

B07 — UUIDv4 приложения, PostgreSQL timestamps и поля/валидация/PATCH по [TASK-002](docs/TASK-002.md). `public.data_sources` содержит десять NOT NULL колонок: `id`, `name`, `name_key`, `host`, `port`, `database_name`, `username`, `password_encrypted`, `created_at`, `updated_at`. Пароль в plaintext не хранится; read DTO и crypto ещё не реализованы. Server defaults времени — `transaction_timestamp()`; будущий repository обеспечивает row locking, no-op и обновление времени по CR-015.

Host: canonical lowercase ASCII LDH hostname или canonical IPv4/IPv6; запреты URL/DSN, socket path, brackets/zone и правила длины — в [TASK-002 §5](docs/TASK-002.md#5-остальные-входные-поля). Четыре непустые полностью числовые метки через точки допустимы только как валидный canonical IPv4: невалидный кандидат не получает DNS fallback. Будущий API-validator обязан реализовать ту же политику.

## Миграции и доказательства

[`0001_data_sources`](migrations/versions/0001_data_sources.py) — initial revision; [`0002_host_policy`](migrations/versions/0002_host_policy.py) — корректирующий host CHECK с `down_revision` 0001. Уже применённую 0001 нельзя переписывать; изменения схемы оформляются новой reviewed revision. Миграции применены только в указанной ниже изолированной тестовой БД; production-развёртывание не выполнялось. Автоматический разрушительный downgrade запрещён; восстановление — только по отдельно утверждённой процедуре из проверенной резервной копии. Функции downgrade намеренно завершаются исключением.

Последний запуск offline после host-коррекции: 23 metadata/security, 6 migration и 3 health tests — **32 passed**. Были предупреждения Alembic `path_separator` и Starlette TestClient/HTTPX. Реальные PostgreSQL-проверки включали каталог, именованные constraints, host policy, часть Unicode, boundaries ключа 1024/1025 bytes, конкурентные INSERT/UPDATE, rollback и повторный upgrade. 11.11.2026 один restart подтвердил прежние revision, схему и четыре строки: ID, name, host, generated key, timestamps и контрольные суммы ciphertext совпали. Это ограниченное доказательство; отдельные прошлые сценарии и последующие gates остаются открытыми.

## DEFERRED и среда

- **D01:** остальная матрица host/control CHECK и согласование поведения будущего validator — TASK-004/соответствующие integration gates.
- **D02:** repository locking, refresh, no-op PATCH, concurrency и rollback — TASK-006/007/021; defaults и отдельные rollback проверены в PostgreSQL.
- **D03:** оставшиеся boundaries, включая application-only пароль, — TASK-004/015/021; key 1024/1025 проверен.
- **D04:** E6, поиск коллизий и пересчёт stored values до смены PostgreSQL/Unicode/provider/алгоритма.
- **D05:** реальные driver/crypto/network и production/access logging — до приёмки соответствующей функциональности.

Последнее наблюдение 11.11.2026: Docker context `desktop-linux`, `dataflow-postgres` PostgreSQL 18.6, volume `dataflow_pgdata`, порт loopback 5432. Текущее состояние не гарантируется. В изолированной БД `task004_4ad4a493c281` revision `0002_host_policy` и четыре committed синтетические строки; не удалять их и БД автоматически.

Физический backup кластера хранится вне репозитория в защищённом временном каталоге `/private/tmp/dataflow-task004-backup-5k9dax49`; его проверяли восстановлением в отдельном изолированном контейнере и проверкой checksum страниц. Внешние Docker-конфигурация/secret mounts и полное disaster recovery не проверены; владелец принял этот остаточный риск для конкретного restart. Временное расположение не равно долговременному backup. Не читать и не публиковать содержимое архива или секреты. Сохранность, перенос и удаление backup требуют отдельного согласования.

## Git и продолжение

Фактические branch, HEAD и незакоммиченные изменения проверять перед каждой работой; untracked-файлы не видны в обычном `git diff`. Не выполнять Git-операции изменения без разрешения.

Продолжение: сверить TASK-006/007 и DoR, а также согласовать конкретный scope и реальные PostgreSQL-проверки. SQL, DDL, миграции, создание и очистка ресурсов требуют отдельного разрешения.

## Обновление TASK-005

Offline: 362 passed, 1 warning; отдельная live auth/pool регрессия: 5 passed. Изолированная БД и две роли запуска 51a5eb7a3d824992b7d35002df1fec0e и их Keychain-записи удалены; реестр и ledger сохранены. Test_User и её CONNECT к postgres сохранены; TASK-004 сохранена с утверждённым ACL без PUBLIC CONNECT. См. backlog и реестр.
