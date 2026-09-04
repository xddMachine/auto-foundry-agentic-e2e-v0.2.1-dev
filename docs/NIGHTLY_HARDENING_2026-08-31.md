# Ночное усиление Auto Foundry — 2026-08-31

## Итоговый вердикт

Текущий checkout имеет высокую степень готовности к следующему прогону: критические контракты, границы владения процессом, typed recovery и проверенные offline-пути теперь согласованы. Это вывод с высокой уверенностью по результатам повторных графовых и кодовых аудитов, focused-регрессий, свежих независимых review и offline-gate-проверок. Это не математическая гарантия: live model/Auto Foundry run в эту ночь не разрешался, поэтому поведение настоящего внешнего транспорта и модели ещё не наблюдалось.

Ночное усиление следует отличать от состояния дерева в целом. Checkout изначально был существенно грязным и содержал пользовательские и параллельные изменения. В этот отчёт включены только подтверждённые причины, исправления и результаты проверок, относящиеся к hardening-поверхности; нельзя считать каждый diff в репозитории результатом этой ночной работы. Несвязанные пользовательские изменения сохранены и не откатывались.

## Операционные границы

- Не выполнялся live model call, live Auto Foundry run, публикация или внешний product transport.
- Пользовательский run `RUN-371803ac3af54e92` оставлен в состоянии `paused`; его данные и артефакты не изменялись.
- По финальной проверке run `RUN-371803ac3af54e92` был `paused`, Coordinator — `waiting`, `active_dispatches=[]`; порты `8768`, `8777` и `8876` не имели listener, Auto Foundry и Control Center процессов не было. Остановленность подтверждена наблюдением, а не предположена по границе.
- Production/user Control Center и live Auto Foundry services не запускались; для browser smoke использовался только изолированный временный UI fixture server, после проверки он был остановлен и удалён.
- Не выполнялись reset, stash, revert, commit, service mutation или публикация release-артефактов.
- Все проверки ниже — offline, на текущем дереве и изолированных временных фикстурах.

## Как проверялась уверенность

Работа проходила несколькими волнами: сначала поиск контрактов через граф кода и трассировку вызовов, затем focused-кодовые проверки и изолированные воспроизведения, после чего — свежие review-пакеты по lifecycle/integration, routing/specialists, reporting/publication и process ownership. После каждого существенного изменения прогонялись узкие тесты и затем расширенный набор. Отдельно проверялись границы, где бизнес-acceptance уже достигнут, но техническая интеграция, публикация или процессный контроль ещё не завершены.

## Матрица первопричин и исправлений

### Routing, prompts и action-role typing

- Зафиксирован один latest-version action→role контракт. Удалены executable legacy aliases и fallback-роли вроде `portfolio_planner`, `report_agent`, `fidelity_reviewer` и `bounded_specialist`.
- Planner/action metadata теперь выводит только канонические роли; planner/rethink/control действия, не являющиеся model-backed dispatch, не получают модельный маршрут.
- Проверки роли и типа действия выполняются до dispatch. Несовместимая роль теперь даёт typed repair/contract defect, а не поздний или молчащий dispatch.
- Prompt wording использует coordinator-owned public request workflow; прямое «delegate specialists» не является runtime-инструкцией. Ограничение no-subagent сохраняется.
- Capacity остаётся bounded и поддерживает максимум 64, включая отдельный specialist subcap.

### Durable retry, shared discovery, specialists и MissionContext

- Retry привязан к durable fingerprint и typed state, а не к повторному model guess. Идемпотентная запись не создаёт вторую попытку при повторном чтении того же состояния.
- Shared `RequirementExecutionGroup.shared_analysis_intent` и `suggested_specialists` передаются как advisory/evidence reuse. Item-local выводы остаются исключительной ответственностью Analytical Owner.
- Specialist workflow стал coordinator-owned: AO сохраняет bounded `SpecialistTask` через public AnalystWorkspace, Planner предлагает одну свежую `specialist` action на unresolved task, specialist сохраняет typed `SpecialistMemo`, а memo разблокирует тот же task. AO не создаёт subprocess/delegate напрямую.
- Dynamic identity discovery дедуплицирует один домен, даже если он нужен нескольким требованиям или уже есть in-progress request. AO возобновляется только когда готовы все текущие домены; общий домен не вызывает лишний model call.
- Immutable MissionContext references/hashes проходят в metadata и prompts без дополнительного model call; stale или неподтверждённый hash не выдаётся за текущий контекст.

### Accepted business result и integration recovery

- Принятый бизнес-артефакт не теряется из-за последующего `technical_failure` Integration Agent. Для того же Integration Agent/session создаётся bounded typed repair/continuation path; повторный business review не запускается.
- Пустой или частичный `IntegrationSession` теперь даёт точный `staging_incomplete` handoff и reoffer того же session. Пустые checked records не отправляются в fidelity review.
- Настоящий no-op обязан иметь явную typed `no_change`/limitation integration record. Отсутствие записей остаётся incomplete, а не success.
- Контроль duplicate `artifact_id` выполняется до terminal business acceptance intent, даже когда совпадают bytes; дефект представления остаётся ремонтируемым.
- Lifecycle reconciliation опирается на тот же strong committed-manifest/publication boundary, что и Planner validation, а не на слабую item label. Законные recovery edges сохранены, незаконные edges закрыты.

### Deterministic identity commit

Identity commit, если все immutable inputs уже авторизованы и hash-bound, выполняется механически и не вызывает ненужную вторую model review. Бизнес-семантика не переносится в deterministic code: механика проверяет только typed identity, lineage, hashes и границу публикации. Если источник не даёт достаточной авторизации, путь остаётся review-bound и не делает вид, что commit детерминирован.

### Reporting, product и publication boundaries

- Reporting preflight/finalization и publication authorization стали механическими там, где durable inputs достаточны; accepted product не объявляется опубликованным без canonical policy и hash-bound authorization.
- При disabled publication policy planner/coordinator не выдают невозможный `publish_final_product`: терминальное состояние честно сообщает `reviewed product awaiting publication authorization`, без model retry.
- При enabled policy authorization привязана к exact product/manifest hash. Product и report paths не подменяют authoritative business/integration evidence слабой проекцией.
- Final-report recovery сначала детерминированно восстанавливает journal/backup через публичный `RunReportFinalizer.recover()`, затем собирает report. `transaction_pending` без durable подтверждения fail-closed и требует operator repair, а не повторного model action.

### UI launch visibility, idempotency и source formats

- UI dry-run и launch status используют private durable receipts, public redaction и CAS/idempotency-bound preparation/execute. Reload discovery читает authoritative status, а не stale label.
- Для архивов и внешних артефактов сохранены path, symlink, XML и ZIP safety boundaries; rejected/opaque members не превращаются в аналитическое evidence.
- Parquet и SQLite обрабатываются как реально проверенные форматы с provenance, а не как декларация «архив скачан, значит доступен». Analytics reproducibility сохраняет hashes, source bindings и bounded resource measurements.

## Process-start closure: найденные разрывы и финальные fixes

### `SubprocessRunner.start` и `LaunchManager.execute`

Первый разрыв был в том, что `Popen` уже создавал child, а затем `uuid.uuid4()` и извлечение `pid/pgid` происходили до cleanup-protected блока. Исключение в этом окне оставляло Supervisor без владельца. Исправления:

1. `monitorRunId` и другие независимые fallible значения формируются до `Popen`.
2. Сам `Popen` context exit, извлечение identity, построение result и readiness wait находятся в одном post-spawn try.
3. Для обычного `start_new_session=True` сохраняется безопасный fallback `pid → processGroupId`, если fake/обёртка не раскрывает `pgid`.
4. Любая ошибка после spawn сначала проверяет именно token-owned process group и вызывает только точный group termination. Broad kill по PID/группе не используется.
5. Если прямое завершение не подтверждено, `_SupervisorStartCleanupError` переносит полный private identity (`pid`, `processGroupId`, `processGroupToken`, startup metadata) в `LaunchManager.execute`; менеджер делает ещё одну bounded exact cleanup попытку и сохраняет failed/recoverable state вместо успешного запуска или потери child.

Readiness identity mismatch и искусственная ошибка `pgid` после Popen теперь воспроизводятся изолированными тестами: проверяется exact PGID/token cleanup и отсутствие orphan ownership gap.

### Различие `startupToken` и `processGroupToken`

Это два разных production token и они намеренно не взаимозаменяемы:

- `startupToken` используется только для проверки readiness/exit receipt.
- `processGroupToken` является private proof владения process group и используется для `ps` liveness probes и termination.
- При reload каждый token валидируется из своего поля. Public status и run-control response редактируют оба токена; private durable status сохраняет их только для recovery.
- Running и timed-out status с разными token теперь остаются live/starting при reload; malformed receipt при live или неизвестной liveness остаётся recoverable, а не ложно failed.

### `RunControlManager._resume` и второй caller

Второй production caller раньше ловил `_SupervisorStartCleanupError` как обычный `LaunchConflictError`, паузил lifecycle и терял `exc.started`. Это позволяло повторному resume создать второй child. Теперь `_resume`:

- распознаёт и валидирует transferred complete identity;
- повторяет exact token-owned termination через process controller;
- при подтверждённой остановке паузит lifecycle и записывает truthful private launch status `failed` (recoverable, без live ownership);
- при неподтверждённой остановке паузит lifecycle, сохраняет private identity в `starting` и оставляет run recoverable;
- после reload видит token-owned orphan в `_active_process` и блокирует второй resume до его безопасного завершения;
- не раскрывает `startupToken` или `processGroupToken` в public response.

Оба направления — confirmed cleanup и cleanup failure — покрыты regression tests. Это не глобальная блокировка: блокируется только повторное действие для конкретного run и конкретного token-owned group.

### Run-wide admission и reload closure

- Один и тот же per-run POSIX `flock` используется `LaunchManager.execute/cancel` и `RunControlManager.pause/resume`; lock приобретается до instance-local mutex, после acquire выполняется authoritative reload draft/status/lifecycle/process state.
- Cancellation сериализована с execute/pause/resume, terminal и idempotent для уже `cancelled` draft; проигравшая операция не может перезаписать truthful cancelled state.
- Same-draft retry с complete private identity возвращает текущий recoverable status без нового `runner.start`; private `pid`, PGID и оба token сохраняются только в durable status, public projection их редактирует.
- Ownership проверяется на уровне run, а не одного draft ID: разные continuation drafts не порождают второго Supervisor. Private status records дедуплицируются по exact `(processGroupId, processGroupToken)`; каждая complete identity проверяется на reload и отдельно отбрасывается только при положительно подтверждённом `gone`.
- Любая distinct live или unknown identity остаётся владельцем: older-live/newer-gone блокирует duplicate spawn/resume, как и неизвестная liveness. Более новая identity-less queued запись не скрывает старую complete identity; all-gone identities освобождают admission.
- Different-draft alias получает durable `queued` и non-cancellable semantics, даже если общий Supervisor `starting`; cancel alias не вызывает termination shared group. После подтверждённого gone обычный continuation/retry остаётся возможным.

## Каноническая role/model карта

| Route | Canonical role | Model/reasoning |
|---|---|---|
| Intake | `intake_planner` | Sol / high |
| Coordination | `foundry_supervisor` | Sol / high |
| Item analysis | `analytical_owner` | Sol / high |
| Business review | `business_reviewer` | Sol / high |
| Identity review | `identity_reviewer` | Sol / high |
| Integration fidelity | `integration_fidelity_reviewer` | Sol / high |
| Product review | `product_reviewer` | Sol / high |
| Entity resolution | `entity_resolution_owner` | Luna / max |
| Integration execution | `integration_agent` | Luna / max |
| Bounded specialist | `specialist` | Luna / max |
| Product execution | `product_agent` | Luna / max |
| Reporting | `reporting_agent` | Luna / max |

Deterministic planner/control actions имеют typed route без model: они только проверяют durable inputs, authorization, hashes, lifecycle edges и создают bounded continuation. Intake/supervisor control routes остаются отдельными typed control paths, но не получают legacy aliases.

## Реальное UberJugaad evidence (offline)

Проверенный локальный archive имеет SHA-256 `f28412f6d1469212e71f2dfa1697967b930390611524f6c2c5ca67899d6b48a6` и размер `72,224,915` bytes. Из 15 физических members построено 18 catalog entries: 10 tables, 6 documents и 2 opaque entries. Это именно catalog/provenance результат; он не означает, что сеть или внешний SQL transport был разрешён.

Шесть Parquet-таблиц дали следующие размеры:

- `all_communications`: `151673 × 19`;
- `business_documents`: `32 × 6`;
- `erp_transactions`: `1916685 × 29`;
- `sales_documents`: `411966 × 15`;
- `sales_items`: `1916685 × 12`;
- `supporting_documents`: `3467 × 34`.

SQLite sidecar содержит `contacts`, `emails`, `folders` и `metadata`. Полная offline-проверка заняла `0.808s` и использовала примерно `127MB` RSS. Размеры, hashes и source bindings сохранены для воспроизводимости; никаких live database calls не выполнялось.

## UI dry-run evidence

Изолированный UI dry-run преобразовал 13 intake blocks в 13 requirements. `prepare` и `execute` были идемпотентны, runner start произошёл ровно один раз, а reload discovery увидел все 13 requirements. Isolated browser smoke прошёл, временный сервер после проверки был очищен. Это подтверждает launch/visibility/idempotency boundary, но не является live model acceptance.

## Последние проверенные gates

- Python: `1332 passed, 2 skipped, 26 warnings` за `90.37s`.
- JavaScript: `4/4`.
- Strict Ruff curated gate: `E9,F63,F7,F82,B023`.
- `git diff --check`: чистый.
- App-only тестовые вызовы используют `PYTHONPATH=src:.`; это служебная изоляция import path и не меняет runtime policy.
- Official ZIP: SHA-256 `dd3780d979b21208af752429ddd00ac744e078f385af2a9f977b3cee32d21049`, 28 files, `282814` bytes.
- Wheel: SHA-256 `d8dad122b92be5cfa2817c997568719deec420c7641a07cadf1a38dd77eb43fa`, `632816` bytes.
- `validate_release`: offline install/import/CLI прошли.
- Production line coverage: `77.8998%` (`46796` statements, `10342` missing). Coverage run отдельно повторил те же `1332 passed, 2 skipped, 26 warnings` под instrumentation за `169.72s`.

Эти gates были выполнены без live run и без публикации текущего пользовательского run.

## Почему следующий run вероятно пройдёт по стадиям

1. **Source/archive admission.** ZIP/XML/path/symlink checks, archive hash и catalog boundaries уже проверены на реальном архиве; Parquet/SQLite evidence имеет конкретные counts и provenance. Поэтому следующий offline ingest не должен повторно спотыкаться о неявный формат или неподтверждённый member.
2. **Planning и routing.** Каноническая action→role карта, role validation и bounded repair исключают старые aliases и невозможные model routes. Planner retry теперь fingerprint-bound, поэтому повторное чтение не создаёт расходящийся plan.
3. **Entity discovery.** Domain dedupe и all-current-domains readiness подтверждены focused call-count сценариями; общий домен не должен вызывать лишний lookup, а reopen выбирает latest compatible lineage.
4. **Analytical/integration.** Accepted business result отделён от технической интеграции: partial/empty staging возвращает `staging_incomplete`, no-op требует typed record, а `technical_failure` получает same-session repair. Это убирает прежние terminal/skipped false positives и не заставляет повторять business review.
5. **Reporting/product/publication.** Deterministic preflight/finalization и canonical publication policy предотвращают невозможный publish action. Disabled policy завершает честным awaiting-authorization состоянием; enabled policy требует hash-bound authorization.
6. **Lifecycle/process control.** Post-Popen ownership, startup/process token separation, exact termination и reload orphan detection закрывают окно, в котором второй resume мог породить duplicate Supervisor. Confirmed и failed cleanup paths теперь имеют durable state и regression coverage.
7. **UI/reload.** 13→13 dry-run, one runner start, idempotent prepare/execute и reload discovery всех 13 элементов показывают, что UI не должен терять requirements или запускать повторно только из-за reload.

На каждой стадии это прогноз по локальным deterministic/offline доказательствам, а не обещание результата модели. Реальная модель может вернуть новое содержание, которое потребует typed repair, specialist evidence или operator authorization.

## Остаточные non-blockers и честная неопределённость

- Остались 2 fixture skips.
- Есть test-only macOS fork warnings; production document parsing использует `spawn`.
- Coverage не 100% (`77.8998%`, `46796` statements, `10342` missing), поэтому непокрытые ветви не следует считать доказанными.
- За curated Ruff gate остаётся широкий nonfatal lint/type debt вне выбранного набора; он не был смешан с этой ночной контрактной работой.
- Historical implementation IDs на отдельных слоях используют SHA-1, поверх них добавлены SHA-256 authoritative bindings; миграция старых идентификаторов не объявляется выполненной.
- Stale Playwright wrapper ссылается на отсутствующий `playwright-cli`; прямой `npx` smoke-путь работал, но wrapper debt остаётся.
- Release artifact regression требует, чтобы `dist` был предварительно собран и присутствовал; отсутствие dist не следует интерпретировать как regression production runtime.
- Самое существенное ограничение остаётся прежним: live model/Auto Foundry run не был разрешён. Поэтому нельзя утверждать математическую гарантию прохождения semantic/model-dependent стадий.

## Независимое завершение

Fresh independent Sol closure review: PASS. Review повторно подтвердил process-start ownership в `execute` и `_resume`, разделение `startupToken`/`processGroupToken`, exact token-bound termination, per-run admission locking, reload duplicate prevention и отсутствие broad kill. Reviewer также принял run-wide distinct-draft ownership: complete identities дедуплицируются, gone отбрасываются индивидуально, live/unknown older owners не скрываются более новыми draft records, а different-draft aliases остаются queued и non-cancellable. Это подтверждает высокую уверенность в локальных deterministic/offline gates, но не является математической гарантией и не заменяет неразрешённый live model/Auto Foundry прогон.

Final independent closure review: PASS
