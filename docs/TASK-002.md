# TASK-002 — Декларативная спецификация data_sources

**Status: AGREED. Approval date: 2026-11-10. Approved by: Product Owner / пользователь.**
Основание: утверждённый пакет B05/B06/B07, CR-015–017 и отдельное принятие итоговой спецификации владельцем.
**B05/B06/B07: APPROVED** владельцем продукта; это не подтверждение реализации.
DDL, миграции, ORM и production API в рамках этой задачи не создаются.

## 1. Scope и источники

Одна таблица `public.data_sources` служебной PostgreSQL: конфигурации PostgreSQL-источников,
без plaintext password и истории проверок. Документ задаёт поля, инварианты и границы API/application/DB.

Иерархия: прямое утверждение пользователя B05/B06/B07 и CR-015–017 → [PRD](PRD.md)
→ [ADR-002/003/005](ARCHITECTURE.md#10-architecture-decision-records) → факты исследований
→ технические предложения. API-контракт — [ARCHITECTURE §4](ARCHITECTURE.md#4-api-design),
карточка — [BACKLOG TASK-002](BACKLOG.md#task-002--согласование-схемы-data_sources).

`result(9).md` физически недоступен в проверенном репозитории. Основание утверждения —
прямое решение пользователя в сессии и перечисленные им правила, а не прочитанный внешний файл.
Отчёты SPIKE-004/006 доступны в истории диалога; их безопасная сводка сохранена в
[реестре доказательств](BACKLOG.md#15-approved-decisions-and-evidence).

**NORMATIVE:** утверждённые правила ниже. **VERIFIED:** только наблюдения R004/R006.
**DEFERRED:** будущие доказательства и работы. Утверждённое ограничение не становится VERIFIED.
Новые CHECK, пределы, 400/415 и timestamp-механизм ещё не проверены экспериментально.

## 2. Колонки, генерация и API → DTO → DB

Все колонки NOT NULL. Create принимает только шесть пользовательских полей; PATCH — их непустое подмножество.
Все входные строки имеют строгий JSON string type, без coercion, surrogate и U+0000.

| API / DTO | PostgreSQL column / type | Генерация/default и ограничения | PATCH | SourceRead |
|---|---|---|---|---|
| `id`, read-only string | `id uuid` | UUIDv4 приложения; PK `pk_data_sources`; DB default отсутствует | Запрещён | Canonical lowercase hyphenated UUIDv4 |
| `name`, required Create | `name text` | Без default; 1–255 scalars, original; §3 | Переданное значение заменяет original | Да, без нормализации |
| Не принимается | `name_key text COLLATE "C"` | STORED generated, §3; ≤1024 UTF-8 bytes; UNIQUE | Только автоматически | Нет |
| `host`, required Create | `host text` | Без default; canonical ASCII hostname/IP; §5 | Валидация/канонизация | Да, canonical |
| `port`, required Create | `port integer` | Без default; 1–65535 | Strict integer | Да, JSON integer |
| `database`, required Create | `database_name text` | Без default; 1–63 UTF-8 bytes; literal name | Без trim/NFC/casefold | Да, как `database` |
| `username`, required Create | `username text` | Без default; 1–63 UTF-8 bytes | Без trim/NFC/casefold | Да |
| `password`, required Create | `password_encrypted text` | Только непустой Fernet-токен; plaintext 1–4096 UTF-8 bytes проверяется до encrypt | Missing сохраняет токен, новый encrypt | Никогда |
| `created_at`, read-only | `created_at timestamptz(6)` | PostgreSQL `transaction_timestamp()` | Не меняется | RFC3339 UTC, 6 дробных знаков, `Z` |
| `updated_at`, read-only | `updated_at timestamptz(6)` | Тот же INSERT-default; UPDATE по §4 | Только фактическое изменение | Тот же формат |

UUID — идентификатор, не средство авторизации. DB `uuid` не доказывает UUIDv4 сам по себе:
version/variant контролируются генератором и валидатором path/read, metadata и интеграционными тестами.
Альтернативные UUID-записи, uppercase, braces и UUID другой версии отклоняются в API (422).

Лимиты 255 scalars / 1024 bytes / 253 ASCII chars / 63 UTF-8 bytes / 4096 UTF-8 bytes
приняты продуктово. Это ограниченный профиль v0.1, не физические максимумы всех сборок PostgreSQL.
63 bytes для database/username соответствует стандартному identifier-профилю; нестандартный NAMEDATALEN вне поддержки.

## 3. Original name, ключ и collation — B06

`name` сохраняется буквально. Алгоритм `NFC-casefold-NFC-v1`:
`K(name) = NFC(casefold(NFC(name)))`. Только ключ нормализуется.

Канонический декларативный фрагмент (не исполняемый DDL/не миграция):

```sql
name_key text COLLATE "C"
GENERATED ALWAYS AS (
    normalize(
        casefold(
            normalize(name, NFC)
            COLLATE pg_catalog.pg_unicode_fast
        ),
        NFC
    )
) STORED NOT NULL
```

Именованный `uq_data_sources_name_key`: UNIQUE (`name_key`), **NOT DEFERRABLE**.
Готовый ключ сравнивается побайтовой семантикой `C`; преобразование — явным builtin provider.
Не полагаться на default collation служебной БД, не заменять алгоритм на `lower`/`citext`.
Writer не владеет таблицей, не меняет expression/ограничения, не записывает generated key.
INSERT/UPDATE меняют только original; серверный инвариант обязателен для независимых writers.

Исходный поддерживаемый профиль: **PostgreSQL 18.6, UTF8, `pg_unicode_fast`, builtin provider,
collation version `1`**. Этот server profile определяет производственную семантику;
Python-эталон не является вторым источником записываемого ключа. R004 использовал Python 3.12.6/UCD 15.0.0;
совпадение всех Unicode-таблиц Python/PostgreSQL не доказано. Отсутствие профиля — gate развёртывания,
не повод молча перейти на другую collation или `lower`.

Политика `name`:

- null/пустое значение запрещены; 1–255 Unicode scalar values исходной строки;
- U+0020 на первом/последнем месте запрещён без trim; строка только из U+0020 отклоняется;
- U+0000, U+0001–U+001F и U+007F запрещены;
- другие Unicode whitespace сохраняются буквально, если не нарушают эти ограничения;
- внутренние пробелы не схлопываются; NBSP не превращается в U+0020;
- не применять NFKC, удаление диакритики или confusable mapping;
- key ≤1024 UTF-8 bytes независимо от размера original; ничего не обрезать.

### CR-016 — единицы длины

Unicode scalar исключает surrogate U+D800–DFFF. В Python `len(str)` считает code points, а не grapheme clusters
и не bytes; отдельные surrogate необходимо отклонить до измерения/UTF-8 encoding.
На корректной строке PostgreSQL UTF8 `char_length(name)` соответствует числу code points/scalars;
`octet_length(name_key)` считает UTF-8 bytes. Для database/username/password в приложении используется
`len(value.encode("utf-8", "strict"))`. Supplementary character — один scalar и обычно четыре UTF-8 bytes.
Combining sequence может быть несколькими scalars при одном видимом символе.

Имя измеряется **до** NFC/casefold; ключ — **после** серверного вычисления. Python prevalidation
не заменяет серверный key-size CHECK: version-sensitive расхождение обрабатывается безопасно по §8.

## 4. PostgreSQL time и CR-015

Источник обоих времён — PostgreSQL, не часы Python.

**INSERT:** приложение не передаёт timestamps. Оба DB default — `transaction_timestamp()`;
это одно согласованное значение на транзакцию, поэтому created_at = updated_at.
Значение отражает transaction time, а не обещание времени физического commit.

**PATCH, механизм без trigger:** SourceService открывает одну единицу работы; repository получает текущую
строку с row lock (семантика SELECT FOR UPDATE). После валидации и канонизации он сравнивает
переданные несекретные поля с сохранёнными значениями буквально (для text — без case-insensitive collation).
`Name → NAME` — фактическое изменение original, даже если key прежний. Одинаковый canonical host — no-op.

- Если password отсутствует и все переданные поля равны, UPDATE не выполняется: 200, timestamps прежние.
- Переданный допустимый password шифруется заново и считается изменением; decrypt старого ради сравнения не нужен.
- При изменении repository выполняет один UPDATE значений и `updated_at = clock_timestamp()` в той же транзакции.
  Read формируется из результата операции, успешный ответ отправляется только после commit.
- created_at/id не обновляются; failed PATCH откатывает также updated_at; check ничего не меняет.

Это **явный контракт DB/application boundary**, не скрытая generated/trigger-гарантия времени.
БД обеспечивает время/точность/атомарность, repository — определение no-op и выбор UPDATE.
Обычный writer не владеет объектами; INSERT-права не включают generated key/timestamps;
отдельные прямые writers должны использовать тот же repository-контракт. Одни defaults/NOT NULL
не запрещают привилегированному SQL-writer нарушить timestamp-политику.
Trigger не выбран и не добавляется автоматически. Если понадобятся независимые прямые writers
с полной DB-гарантией timestamps, это отдельное архитектурное изменение.

Не вводить `updated_at >= created_at` или обещание строгой монотонности: серверные часы могут корректироваться.
UTC — формат API; `timestamptz` хранит момент, а не буквальный суффикс Z. Application сериализует UTC с шестью знаками.
До реализации обязательны тесты defaults, row locking, literal comparison, clock expression,
no-op и rollback; механизм не VERIFIED. [B08](B08-SESSION-UOW.md) APPROVED WITH CONDITIONS: выбрана sync Session; реализация и проверки не завершены.

## 5. Остальные входные поля

**Host:** ASCII hostname ≤253 символов; labels 1–63 LDH (буквы ASCII/цифры/дефис), без начального/конечного `-`.
Single-label допускается. DNS имя canonical lowercase. IPv4/IPv6 — одиночный адрес,
canonical representation. Нет whitespace, trailing dot, IPv6 brackets, zone ID, URL/DSN,
host list, порта внутри host, Unix/@socket path. Unicode U-label не принимается; для IDNA допускается
ASCII A-label в этом ограниченном профиле. Unicode hostname input DEFERRED.
Уточнение B07: четыре непустые полностью числовые метки через точки допускаются только как валидный canonical IPv4; `999.1.1.1`, `256.1.1.1` и неканонический `001.2.3.4` не получают DNS fallback. Будущий API-validator должен применять то же правило; он ещё не реализован.
Уточнение B07 принято после применения initial revision к изолированной тестовой БД; для сохранения идентичности истории миграций используется отдельная корректирующая revision `0002_host_policy`.
CRUD не делает DNS/connect и не требует разрешённости host в allowlist; check отдельно проверяет назначение/TLS.

**Port:** strict JSON integer 1–65535. `true`, `5432.0`, `"5432"` недопустимы; DB integer покрывает весь диапазон.

**Database:** literal string 1–63 UTF-8 bytes; Unicode/регистр/пробелы сохраняются, без trim/NFC/casefold.
Control set U+0000–U+001F/U+007F запрещён. `=` и URI-префиксы `postgres://`, `postgresql://` запрещены.
Это намеренное сужение допустимых реальных имён в v0.1, не замена безопасному connector.
Значение передаётся отдельным параметром, без интерполяции и dbname expansion; реальный connector-путь проверяется T-14.

**Username:** literal string 1–63 UTF-8 bytes; тот же control set запрещён.
Без trim/NFC/casefold; пробелы и регистр сохраняются, SQL quoting не применяется к самому значению.

**Password:** strict string 1–4096 UTF-8 bytes. Missing Create/null/empty запрещены;
whitespace-only допустим. Без trim/NFC/casefold, U+0000/surrogate запрещены.
Только CryptoService получает plaintext для encrypt; хранится Fernet-токен `password_encrypted`.
Ciphertext limit не равен plaintext limit: колонка text, без лимита 4096 для токена.
DB не может проверить plaintext-length по токену — это ответственность application до encrypt.
Read/list/DELETE/no-op PATCH без нового password не требуют decrypt; доступность канала ключа остаётся B04.

## 6. Именованные DB-инварианты

Это спецификация для TASK-003/004, **не выполненные ограничения**. Тип text выбран с CHECK, не `char(n)` и не truncation.

| Объект | Инвариант / декларативный предикат | Application complement |
|---|---|---|
| `pk_data_sources` | PRIMARY KEY(id) | UUIDv4 generator/path format |
| NOT NULL всех колонок | Включая name_key/timestamps | Explicit null отклонён до записи |
| `ck_data_sources_name_nonempty` | `char_length(name) > 0` | Не считать пустоту конфликтом |
| `ck_data_sources_name_length` | `char_length(name) <= 255` | Scalar validation, без surrogate |
| `ck_data_sources_name_edges` | `left(name,1) <> ' ' AND right(name,1) <> ' '` | U+0020 без trim |
| `ck_data_sources_name_controls` | Нет U+0001–U+001F/U+007F; NUL отвергает text/protocol | Явный codepoint predicate, не locale-dependent whitespace regex |
| `ck_data_sources_name_key_bytes` | `octet_length(name_key) <= 1024` | Проверка эталона; DB key окончательный |
| Generated name_key | Точный фрагмент §3 | Никогда не принимать от клиента |
| `uq_data_sources_name_key` | UNIQUE(name_key), NOT DEFERRABLE | Правильный rollback/error mapping |
| `ck_data_sources_host` | Непустой ASCII/canonical hostname либо canonical IP, ≤253; правила §5 | Канонизация/разбор до записи; без DNS |
| `ck_data_sources_port` | `port BETWEEN 1 AND 65535` | Reject bool/float/string до DB coercion |
| `ck_data_sources_database_bytes` | `octet_length(database_name) BETWEEN 1 AND 63` | Измерять UTF-8, не Python len |
| `ck_data_sources_database_literal` | Нет control set/`=`/URI-префиксов | Отдельный connector parameter без expansion |
| `ck_data_sources_username_bytes` | `octet_length(username) BETWEEN 1 AND 63` | Строгая строка |
| `ck_data_sources_username_controls` | Нет control set | Без trim/casefold |
| `ck_data_sources_password_token_nonempty` | `char_length(password_encrypted) > 0` | CryptoService проверяет Fernet; CHECK не доказывает шифрование |
| Timestamp defaults | §4, без trigger | Literal no-op сравнение/UPDATE только при изменении |

Control CHECK должен перечислять запрещённые codepoint ranges явно; не расширять набор по locale или Python isspace().
Host CHECK задаёт полный semantic predicate §5; точная PostgreSQL-форма regex/IP-validation должна быть
проверена до приёмки TASK-004 и не может использовать небезопасный cast, выбрасывающий неожиданный SQLSTATE.
Разные допустимые реализации одного предиката не меняют утверждённую политику.
Имена объектов фиксированы, физическая реализация/миграция сейчас отсутствует.

## 7. Create, PATCH и транзакционная граница

Create: envelope/DTO validation → канонизация → UUIDv4 → encrypt → INSERT с DB timestamps/key → commit → SourceRead + Location.
При отказе нет частичной строки. Политика внешнего check при CRUD не вызывается.

PATCH: только непустой JSON object; явный null любого поля, extra/read-only (`id`, timestamps,
name_key и секретные внутренние поля) → 422. Missing сохраняет значение.
Lookup/row lock → no-op detection → при новом password encrypt → UPDATE по §4 → commit → Read.
Не выполнять частичный commit в repository. Ошибка на любом этапе записи вызывает rollback всей единицы работы.
Логическая транзакционная граница утверждена; [B08](B08-SESSION-UOW.md) APPROVED WITH CONDITIONS, реализация Session/timeouts и unit of work остаётся TASK-005.
Чтение/снимок служебной БД закрывается до target connection. Не держать транзакцию ради внешней сети.

DELETE физический, 204 без тела; отсутствующий/повторный ID →404. List без строк →200 `[]`,
но DB failure никогда не превращается в `[]`/404. Неоднозначный commit failure не доказывает отсутствие записи:
не обещать безопасный автоматический retry Create и не повторять его молча.

## 8. Error mapping и безопасность

Публичный контракт — [ARCHITECTURE §4](ARCHITECTURE.md#4-api-design).

| API / DTO outcome | DB/application signal | Публичный результат |
|---|---|---|
| Invalid JSON | Parser, до DTO | 400 malformed_json |
| Wrong Content-Type | Envelope | 415 unsupported_media_type |
| Invalid ID/field/null/extra/body/query | Safe validation projection | 422 validation_error |
| Недопустимое имя по DB CHECK | 23514 + точное известное имя CHECK допустимости | 422, fields=[name] |
| Name conflict | 23505 + uq_data_sources_name_key + ожидаемые public/data_sources, если предоставлены | 409 source_name_conflict |
| Нет записи | Явный результат repository | 404 source_not_found |
| Контролируемая временная DB/crypto/executor недоступность | Типизированный надёжный сигнал | 503 operation_unavailable |
| Чужой UNIQUE/CHECK, повреждённые данные/токен, неизвестная ошибка | Нет надёжного бизнес-mapping | 500 internal_error |
| Отрицательный target check | Надёжная категория стадии | 200 CheckResult failed |

Проверять структурированные sqlstate/constraint_name и schema/table, если имеются. Не разбирать raw message.
Общий IntegrityError не равен конфликту имени; отсутствие нужной диагностики → безопасный internal fallback.
Технические SQLSTATE/имена ограничений не публикуются. После ошибки записи требуется rollback.
Другие известные validation CHECK можно отображать в 422 только по явной reviewed allowlist, не по всему классу ошибок.

Выход: только id/name/host/port/database/username/created_at/updated_at. Никогда password,
password_encrypted/encrypted_password, token/key/name_key, ORM diagnostics.
Не сериализовать ORM целиком; не журналировать body, DSN, SQL/bind parameters,
exception args/cause/traceback/input/ctx/msg/произвольный loc. Обычные логи — фиксированные stage/category и безопасный ID.
Синтетические маркеры и проверки headers/stdout/stderr/structured logs обязательны; production logging ещё DEFERRED.

## 9. Миграции и DEFERRED

- TASK-003: metadata/Computed(persisted)/constraint names по этому документу, безопасный repr, без I/O/create_all.
  Не начата; TASK-002 принята, до Execute требуется обоснование версии SQLAlchemy и отдельное разрешение реализации.
- TASK-004: reviewed Alembic migration, проверка профиля PostgreSQL и реальных CHECK/defaults/generated/UNIQUE.
  Не начата; не выполнять миграции workers автоматически; разрушающий downgrade не является безопасным rollback.
- При существующих данных: validation → расчёт новых ключей → поиск коллизий → решение владельца → контролируемый переход.
  Не обрезать/переименовывать/сливать/удалять записи автоматически.
- **E6 DEFERRED**, обязателен **до** изменения алгоритма, Unicode/provider/версии поддерживаемого профиля:
  расширенная матрица, новые коллизии, пересчёт stored generated values и перестроение UNIQUE.
  Один REINDEX не заменяет пересчёт stored values. Простого refresh collation metadata недостаточно.
- Реальные driver/crypto/network признаки, TLS/DNS/5 секунд, production server/access logs DEFERRED
  до соответствующих integration/security gates TASK-011/013/014/022/023/024; safe fallback остаётся обязательным.
- B08 утверждён с условиями, но не реализован и не VERIFIED; B09/B10/B11 и процедуры ключа/allowlist/TLS вне scope схемы остаются открытыми.

### D01–D05 — сохранённые технические проверки

Все записи **DEFERRED**, не выполнены. Они не препятствуют документальному закрытию TASK-002,
но обязательны на назначенных этапах. Это реестр уже предусмотренных проверок, не новые задачи-дубликаты.

| ID | Статус | Проверка | Ответственный этап / условие |
|---|---|---|---|
| D01 | DEFERRED | Точные SQL-формы host/control CHECK | TASK-003 metadata; TASK-004 реальная PostgreSQL-проверка |
| D02 | DEFERRED | Server defaults, row locking, ORM refresh, no-op PATCH, concurrent UPDATE и rollback | TASK-004/006/007/021 до приёмки соответствующей реализации |
| D03 | DEFERRED | Boundary новых CHECK/пределов, включая name_key 1024/1025 UTF-8 bytes | TASK-004/015/021; fixtures сначала измеряются, неизвестный край не считается PASS |
| D04 | DEFERRED | Unicode E6: provider/Unicode-version/algorithm, коллизии, recompute/UNIQUE | Обязателен до изменения закреплённого PostgreSQL 18.6/UTF8/pg_unicode_fast-профиля; scope SPIKE-004 и migration review |
| D05 | DEFERRED | Реальные driver/crypto/network и production logging/access logs | TASK-008/011/013/014/015–020/022/023/024: integration/security-gates до приёмки соответствующей функциональности |

## 10. CR-017 — обязательная матрица будущих проверок

Все строки ниже **NOT RUN**. Исторические R004/R006 не подменяют проверку новых ограничений.

| ID | Вход / сценарий | Требуемое доказательство | Владелец будущей проверки |
|---|---|---|---|
| V01 | name 0/1/255/256 scalars | Reject/accept/accept/reject при key в лимите; DTO и direct DB | TASK-004/015/021 |
| V02 | Supplementary Unicode, combining sequences, surrogate/NUL | Один supplementary = один scalar; строгий UTF-8; original неизменен | TASK-003/004/015 |
| V03 | name_key 1024/1025 UTF-8 bytes при name ≤255 scalars | Accept/reject, известный CHECK →422, без truncation | TASK-004/015/021 |
| V04 | NFC/casefold M01/M04–M11 | Эталон и серверное выражение; нет ложного объединения | TASK-004/021 |
| V05 | `Name`, ` Name`, `Name `, только U+0020 | DTO reject до DML; direct DB CHECK reject; пустая/занятая таблица | TASK-004/015/016/018 |
| V06 | U+0001–001F/U+007F, NBSP, внутренние пробелы | Controls reject; другие whitespace сохраняются по §3 | TASK-004/015 |
| V07 | Malformed nonempty JSON | 400, без input/секретов | TASK-015/016/018 |
| V08 | Неверный Content-Type | 415; priority до parser/DTO | TASK-015/016/018 |
| V09 | port 1/65535, 0/65536, bool/float/string | Strict DTO и DB range; типовая DB coercion не заменяет DTO | TASK-004/015 |
| V10 | host 253/254, label 63/64, case, IP, brackets/zone/trailing dot/URL/list/U-label | Точная канонизация/отказ; без DNS | TASK-004/015; сеть TASK-023 |
| V11 | database/username 63/64 UTF-8 bytes, non-ASCII, controls, `=`/URI | Boundary и literal передачи без conninfo | TASK-004/015; connector T-14 |
| V12 | password 4096/4097 UTF-8 bytes, whitespace, null/empty/missing | Accept/reject, без нормализации/утечек; ciphertext отдельно | TASK-008/015/018/022 |
| V13 | INSERT timestamps | PostgreSQL источник, оба равны; UTC API 6 знаков; readonly | TASK-004/006/016 |
| V14 | UPDATE/no-op/case-only rename/check | updated_at только при изменении, created_at прежний; check не меняет | TASK-007/014/018/021 |
| V15 | Concurrent INSERT/UPDATE, первый commit и rollback | Независимые connections, barrier/observer, commit/final rows | TASK-006/007/021 |
| V16 | 23505 нужного/чужого UNIQUE, missing/malformed diagnostics | Только точный name conflict; rollback, безопасный fallback | TASK-006/007/015/021 |
| V17 | Все ошибки/ответы/headers/logs/stdout/stderr | Нет password/token/key/DSN; positive detector control | TASK-015–020/022 |
| V18 | DB/crypto failure после части подготовки/записи | Нет частичных значений/timestamps; 503 против 500 по признаку | TASK-005/008/016/018/021/022 |
| V19 | Пустой PATCH/null/extra/read-only, same password | 422 либо принятый PATCH; missing secret сохранён; новый encrypt | TASK-015/018 |
| V20 | UUIDv4 canonical/другие формы, health, 404/405/query/body | Точный DTO/API порядок, Allow; health прежний | TASK-003/015–020/024 |

V03 требует настоящих fixtures: сначала измерить server key и original scalars, затем оценивать результат.
Кандидаты, **не проверенные сейчас**: U+1F88 ×205 для 1025-byte key; U+1F88 ×204 + U+1F600 для 1024-byte key.
Предполагается folding U+1F88 → U+1F00 + U+03B9 (5 bytes); это гипотеза для построения fixture, не факт нового эксперимента.
Если выбранный профиль даёт иной размер или край недостижим при ≤255 scalars, зафиксировать UNKNOWN/ограничение
и вынести отдельный fixture-план. Нельзя выдавать фиктивный PASS или обходить generated column произвольным ключом.

## 11. Acceptance criteria и статус согласования

- [x] Каждое поле PRD сопоставлено DTO/DB/Read; нет plaintext-колонки.
- [x] B05/B06/B07 и CR-015–017 отражены; expression/collation/имена объектов определены.
- [x] Указаны responsibilities, no-op/time-механизм, rollback/error mapping, ограничения доказательств и DEFERRED.
- [x] Определена полная будущая boundary/security/concurrency матрица, не заявлено её выполнение.
- [x] Владелец принял итоговый документ TASK-002 и проверочные примеры как спецификацию схемы (2026-11-10).

**TASK-002: AGREED AND DOCUMENTED.** Acceptance review: **ACCEPTANCE RECOMMENDED**,
BLOCKER / MAJOR / MINOR — 0; владелец подтвердил принятие в текущей сессии 2026-11-10.
Основание review — прочитанные документы/diff, проверка схемы/CR-015–017/ссылок, без SQL и DB-тестов.
Запись приёмки — [BACKLOG TASK-002](BACKLOG.md#task-002--согласование-схемы-data_sources).
Реализация схемы не выполнялась. D01–D05 сохранены DEFERRED; согласование не разрешает Execute TASK-003/004.
Решения пользователя B05/B06/B07 остаются APPROVED.
Новые CHECK/пределы/defaults и production-гарантии реализуются и проверяются в последующих задачах.
