# B08 — Session, Unit of Work и служебный I/O

**Статус: APPROVED WITH CONDITIONS.** B08 v1.6, Q01–Q15, G04–G06,
Test Profile v1.0 и детализация M1/M2 приняты прямым решением владельца в текущем
согласовании. Утверждение контракта не означает реализации или прохождения тестов.
Фактическая версия libpq, совместимость capability check и изоляция стенда — NOT VERIFIED.

Источники и трассировка: [ADR-002](ARCHITECTURE.md#adr-002--служебная-postgresql-и-доступ-к-данным),
[TASK-002](TASK-002.md), [TASK-005 и дети](BACKLOG.md#task-005--session-и-транзакции).
B05–B07 и требования к внешним ETL-источникам не изменяются.

## 1. Исполнение и lifecycle

- Sync SQLAlchemy 2.x и psycopg 3, Engine на процесс, централизованный sessionmaker;
  независимая Session на UoW, без разделения между запросами и потоками.
- Сервис определяет транзакционную границу; блокирующий DB I/O не выполняется
  непосредственно в event loop. AsyncSession и repository не входят в TASK-005.
- Engine не создаётся при import; его создание не требует подключения. Без автоматических
  retry бизнес-транзакций; результат ошибочного commit не повторяется автоматически.
- FastAPI lifespan валидирует конфигурацию, создаёт ресурсы и освобождает Engine.
  При ошибке конфигурации Engine не создаётся, HTTP продолжает работать; DB-dependent
  операции отклоняются до SQL. Конфигурация перечитывается после рестарта.
- `GET /health`: HTTP 200, `{"status":"ok"}`, `application/json`, без PostgreSQL,
  DNS, ключей и внешних сервисов; это не readiness. Служебная транзакция закрывается
  до подключения к внешнему источнику. Pool pre-ping может выполнять I/O при checkout.

## 2. M1 — конфигурация и libpq

Все 12 переменных обязательны для валидной DB-конфигурации; скрытых defaults для них нет.
Числа — ASCII decimal без знака, пробелов и ведущих нулей, кроме `0`; boolean и уровень
изоляции регистронезависимы, дополнительные пробелы запрещены.

| Переменная `DFM_DB_` | Контракт |
|---|---|
| `HOST` | Только `127.0.0.1` |
| `PORT` | 1–65535 |
| `NAME`, `USER` | Каждое соответствует `^[a-z_][a-z0-9_]{0,62}$` |
| `PASSWORD` | Непустой, без NUL, кодируется драйвером; без trim и нормализации |
| `POOL_SIZE` | 1–20 |
| `MAX_OVERFLOW` | 0–20; сумма с `POOL_SIZE` ≤20 на Engine |
| `POOL_TIMEOUT` | 1–60 секунд |
| `CONNECT_TIMEOUT` | 1–30 секунд |
| `POOL_PRE_PING` | `true` или `false` |
| `POOL_RECYCLE` | 0–86400 секунд; `0` не отключает recycle |
| `ISOLATION_LEVEL` | `READ COMMITTED` |

URL собирается через SQLAlchemy `URL.create()` с `postgresql+psycopg`, без ручной
сборки DSN и пользовательских query-параметров. Закрытый профиль libpq: явные host,
port, dbname, user, password, connect_timeout, `sslmode=disable`, `gssencmode=disable`,
`require_auth=scram-sha-256`. Service, password-file fallback, Unix socket, альтернативные
адреса и fallback к более слабой auth запрещены. Без TLS профиль допустим только в
подтверждённой изолированной loopback-среде; SCRAM не шифрует весь трафик и сам по себе
не гарантирует защиту от подмены локального сервера. ETL-источники сохраняют verify-full.

Любое имя переменной окружения с префиксом `PG`, включая пустое и неизвестное, приводит
к fail-closed на startup и перед каждым новым физическим подключением. Снимок `DFM_DB_*`
получают один раз на startup. Приложение не меняет окружение после startup; повторная
проверка не устраняет гонку при изменении окружения другим потоком и не переоценивает
уже существующие соединения. Изоляция от произвольного кода и всех defaults ОС/libpq
не обещается.

Документированные PostgreSQL 18 переменные включают: `PGHOST`, `PGHOSTADDR`, `PGPORT`,
`PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGPASSFILE`, `PGSERVICE`, `PGSERVICEFILE`,
`PGSYSCONFDIR`, `PGREQUIREAUTH`, `PGCHANNELBINDING`, `PGSSLNEGOTIATION`, `PGSSLMODE`,
`PGREQUIRESSL`, `PGSSLCOMPRESSION`, `PGSSLCERT`, `PGSSLKEY`, `PGSSLCERTMODE`,
`PGSSLROOTCERT`, `PGSSLCRL`, `PGSSLCRLDIR`, `PGSSLSNI`, `PGSSLMINPROTOCOLVERSION`,
`PGSSLMAXPROTOCOLVERSION`, `PGGSSENCMODE`, `PGKRBSRVNAME`, `PGGSSLIB`,
`PGGSSDELEGATION`, `PGCONNECT_TIMEOUT`, `PGTARGETSESSIONATTRS`, `PGLOADBALANCEHOSTS`,
`PGMINPROTOCOLVERSION`, `PGMAXPROTOCOLVERSION`, `PGREQUIREPEER`, `PGOPTIONS`,
`PGAPPNAME`, `PGCLIENTENCODING`, `PGDATESTYLE`, `PGTZ`, `PGGEQO`, `PGLOCALEDIR`.
Запрет префикса распространяется также на имена, не перечисленные здесь. В частности,
`PGHOSTADDR` может изменить фактический адрес даже при указанном host.

Capability gate определяет фактически загруженные реализацию и версию libpq и поддержку
`require_auth`; при неизвестной/отсутствующей поддержке — fail-closed без fallback.
Точные API и передачу закрытых параметров требуется проверить на установленном стеке
до DB-dependent использования; конфигурационные структуры и секреты не журналируются.

Закрытые коды: `DB_CONFIG_MISSING`, `DB_CONFIG_INVALID`, `DB_ENV_FORBIDDEN`,
`DB_CLIENT_UNSUPPORTED`, `DB_ENGINE_INIT_FAILED`, `DB_CONNECTION_FAILED`,
`DB_POOL_TIMEOUT`, `DB_DISPOSE_FAILED`. Обычная диагностика не содержит raw exception,
DSN, SQL, bind, пароль, traceback или значения окружения. `hide_parameters` не заменяет
проверку логирования. Pool timeout и connect timeout не являются общим SQL deadline.

## 3. M2 — Unit of Work

UoW одноразовая; Session скрыта от публичного сервисного интерфейса, универсального
публичного SQL нет. Используются `autobegin=False`, явный begin, `close_resets_only=False`;
автоматический commit на выходе запрещён. Commit вызывается максимум один раз и только
явно. После commit или FAILED DB-операции запрещены; повторный вход и использование
после CLOSED запрещены. Контролируемая DB-ошибка помечает FAILED до передачи сервису;
для подавленной бизнес-ошибки сервис вызывает `mark_failed` с закрытым кодом.
Ограниченный интерфейс — не sandbox от намеренного обхода в доверенном Python-процессе.

| Состояние | Событие | Следующее состояние / действие |
|---|---|---|
| NEW | Session и begin успешны | ACTIVE |
| NEW | Session не создана | CLOSED, `OPEN_FAILED`; rollback/close не выполнялись |
| NEW | begin завершился ошибкой | FAILED; попытки rollback и close, затем CLOSED |
| ACTIVE | Успешная контролируемая операция | ACTIVE |
| ACTIVE | Ошибка операции или `mark_failed` | FAILED |
| ACTIVE | Успешный явный commit | COMMITTED |
| ACTIVE | Ошибка commit | FAILED; commit UNKNOWN; retry запрещён |
| ACTIVE | Выход без commit / ошибка бизнеса | Попытка rollback, затем close; CLOSED |
| FAILED | Выход | Попытка rollback, затем close; CLOSED |
| COMMITTED | Выход, включая последующую бизнес-ошибку | Close; CLOSED; успешный commit не отменяется |
| ROLLED_BACK | Cleanup | Попытка close; CLOSED |
| CLOSED | Любая операция или вход | Запрещено; состояние не меняется |

Immutable outcome формируется после попыток cleanup и сохраняется после CLOSED:
`commit_status` = NOT_ATTEMPTED/SUCCEEDED/UNKNOWN; `rollback_status` и `close_status` =
NOT_ATTEMPTED/SUCCEEDED/FAILED/UNKNOWN; `primary_error_code` — закрытый enum или null;
`additional_errors` — упорядоченный immutable tuple закрытых enum; `operation_id` — UUIDv4.
Успешный возврат rollback не доказывает отсутствие commit при UNKNOWN. CLOSED не доказывает
успешного физического освобождения соединения.

Приоритет: ошибка операции/commit, затем rollback, затем close; вторичные ошибки не
заменяют первичную. При ошибке commit результат UNKNOWN и retry запрещён. При успешном
commit и ошибке close outcome содержит SUCCEEDED/FAILED и передаётся через типизированный
`UOW_COMMITTED_CLEANUP_FAILED`; повторять бизнес-операцию запрещено. При бизнес-ошибке
после успешного commit сохраняются SUCCEEDED и приоритет этой ошибки; close failure —
вторичная. Ошибки rollback и close диагностируются даже при одновременном возникновении.

Вызывающая сторона получает безопасное типизированное исключение с outcome и фиксированными
кодами/фазами. Пользовательский текст в `mark_failed` запрещён. Raw исключения и признаки
допустимы только во внутренней контролируемой области; raw exception/cause/context/traceback
не попадают в обычные журналы. Сохраняется возможность последующей B06-классификации по
SQLSTATE 23505, точному constraint и доступным schema/table; repository и mapping вне TASK-005.

### M2.1–M2.7 — коды, фазы и уточнение lifecycle

Для outcome используется отдельный закрытый UoW enum; каталог DB_* M1 не
изменяется. Код однозначно определяет фазу:

| Код | Фаза | Условие |
|---|---|---|
| OPEN_FAILED | OPEN | Фабрика не вернула Session из-за Exception |
| UOW_BEGIN_FAILED | BEGIN | begin завершился Exception |
| UOW_EXECUTION_FAILED | EXECUTION | Неизвестная Exception тела или контролируемой DB-операции |
| UOW_BUSINESS_ERROR | EXECUTION | Сервис вызвал mark_failed с этим кодом |
| UOW_COMMIT_FAILED | COMMIT | commit завершился Exception |
| UOW_ROLLBACK_FAILED | ROLLBACK | rollback завершился Exception |
| UOW_CLOSE_FAILED | CLOSE | close завершился Exception |
| UOW_INVALID_STATE | LIFECYCLE | Операция запрещена в текущем состоянии |

Фазы OPEN, BEGIN, EXECUTION, COMMIT, ROLLBACK, CLOSE и LIFECYCLE образуют
закрытый enum. Поле фазы в outcome не добавляется. additional_errors — tuple
только UOW_ROLLBACK_FAILED и UOW_CLOSE_FAILED в порядке фактических попыток;
primary не дублируется, повтор одинакового кода запрещён.

При отсутствии предыдущей ошибки первый cleanup failure является primary.
При успешном commit и единственной ошибке close outcome содержит commit
SUCCEEDED, rollback NOT_ATTEMPTED, close FAILED, primary UOW_CLOSE_FAILED
и пустой additional_errors. Внешний код безопасного исключения —
UOW_COMMITTED_CLEANUP_FAILED, фаза CLOSE; этот код не входит в enum outcome.

До финализации безопасное исключение может содержать outcome=null. До входа
недопустимая операция оставляет UoW в NEW. Ошибка в ACTIVE регистрируется до
передачи сервису и переводит UoW в FAILED; FAILED не возвращается в ACTIVE.
После перехвата ошибки повторный commit запрещён; при выходе выполняется
cleanup и выдаётся новое безопасное исключение с финальным outcome. Ранее
выданный объект исключения не изменяется. При операции после CLOSED
выдаётся UOW_INVALID_STATE с прежним outcome без его изменения.

mark_failed(UOW_BUSINESS_ERROR) допустим в ACTIVE, регистрирует первую
ошибку и переводит UoW в FAILED. Повторный mark_failed в FAILED запрещён
и не заменяет первичную ошибку. В COMMITTED повторные commit, enter и
mark_failed запрещены; их перехваченные ошибки учитываются при завершении.
Ошибка тела после commit учитывается без rollback. При выходе из COMMITTED
выполняется только close, а commit остаётся SUCCEEDED.

Если фабрика не вернула Session, UoW не вызывает rollback или close. При
ошибке begin, операции или commit до успешного commit UoW пытается выполнить
rollback, затем close. При ошибке commit commit_status=UNKNOWN; retry запрещён.
Обычная ошибка тела отображается в UOW_EXECUTION_FAILED. Первая ошибка
операции, begin, commit или тела сохраняет приоритет над cleanup.

BaseException вне Exception, включая KeyboardInterrupt, SystemExit и
GeneratorExit, не преобразуется в UoW-код и не подавляется. При доступном
управлении выполняются предусмотренные оставшиеся попытки cleanup; первое
BaseException распространяется даже при наличии обычной primary error.
Прерванный commit, rollback или close получает статус UNKNOWN; UNKNOWN не
означает отсутствие эффекта. Если Session не была возвращена фабрикой,
cleanup не вызывается. Обычные ошибки сохраняются в outcome при успешной
финализации; само прерывание не добавляется в enum ошибок. Повторное
прерывание или завершение процесса может помешать cleanup и финализации.

### Q15 — перехваченное прерывание commit

Если собственный вызов commit прерван BaseException вне Exception, commit_status
становится UNKNOWN, попытка commit исчерпана и UoW переходит в FAILED. Первое
прерывание немедленно распространяется; если сервис перехватил его в теле,
UoW сохраняет его в контролируемой области до выхода. Повторный commit запрещён.
При выходе UoW однократно пытается выполнить rollback и close, пока управление
доступно, затем повторно распространяет первое прерывание. Оно не становится
UoW-кодом; первая возникшая обычная ошибка сохраняется primary, последующие
ошибки cleanup — additional по M2. При отсутствии обычных ошибок primary может
оставаться null. Внутренняя ссылка на прерывание освобождается перед повторным
распространением; raw exception и traceback не передаются в обычный журнал.
Правило относится к прерыванию собственного commit, непосредственно наблюдённому
UoW; обнаружение исключений, полностью перехваченных внутри чужого кода, не
требуется. Повторное прерывание или завершение процесса ограничивает гарантии Q13.

Безопасное исключение содержит в str, repr и args только фиксированный код;
outcome содержит только утверждённые enum, UUIDv4 и immutable tuple. Raw
exception, его cause/context и traceback не являются безопасными данными
для журнала или transport. Подавление отображения цепочки через from None
не гарантирует удаления __context__; traceback не считается очищенным.
Обычная диагностика создаётся заново из фиксированных значений без exc_info
и stack_info; её ошибка не заменяет первичную и не создаёт fallback с raw
exception. Признаки B06 допустимы только в контролируемой внутренней области.

## 4. Test Profile v1.0 и G04

PostgreSQL 18.6, `127.0.0.1:5432`, отдельная `task005_<unique_id>`, отдельные роли
подготовки и тестирования, синтетические данные. `pool_size=2`, `max_overflow=1`,
`pool_timeout=5`, `connect_timeout=3`, `pool_pre_ping=True`, `pool_recycle=1800`,
`READ COMMITTED`, `echo=False`, `hide_parameters=True`. Для проверки изоляции нужны
независимые физические соединения. БД и ресурсы TASK-004 не используются.

Без автоматических DROP, TRUNCATE или разрушительного downgrade; ресурсам ведётся учёт.
Создание БД/ролей/схемы, SQL, миграции, integration-тесты и очистка требуют отдельных
разрешений; очистка не подразумевается разрешением на подготовку.

## 5. Проверки и условия реализации

Все проверки B08 ниже **NOT RUN**. Offline: границы конфигурации и `PG*`, capability
gate с подменой, отсутствие I/O при import/создании Engine, Q08, fault injection каждой
фазы UoW, составные ошибки, безопасные логи и маркеры с положительным контролем.
Integration: реальные версия/возможности libpq, изолированный loopback, pg_hba и SCRAM,
запрет fallback, отдельные роли/соединения, rollback/изоляция/освобождение и pool timeout.

До начала соответствующих работ проверить точный API capability gate, таблицу переходов
и готовность fixtures; согласовать стенд и операции. DoD TASK-005 требует реальных
PostgreSQL-проверок, mock их не заменяет. Следующие операции и приёмка — в
[backlog](BACKLOG.md#task-005--session-и-транзакции).
