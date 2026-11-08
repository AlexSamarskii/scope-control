# Происхождение кода и компонентов

Scope Control использует одну основную платформу — SpeedPy. Дополнительно
адаптированы отдельные интерфейсные и архитектурные решения других проектов.
Доноры не устанавливаются как дополнительные auth/ORM/worker-платформы.

Это публичная сводка фактических заимствований. Полные лицензионные тексты и
уведомления: [LICENSE](../app/LICENSE.md), [NOTICE](../app/NOTICE.md),
[OFL Inter](../app/static/fonts/OFL-Inter.txt). Сводка не заменяет их условия
и не назначает новую общую лицензию проекту.

## speedpy-foundation-S1

Источник: https://github.com/speedpy/speedpy

Ревизия: `2cd0043c474b145f8be395eec4b8d31483c8daa0`.

Сохранён каркас Django/ORM/auth/Celery, server templates, статические ресурсы
и подходящие тесты. Исходные каталоги: `mainapp/`, `project/`, `speedpycom/`,
`usermodel/`, `demoapp/`, `crispy_tailwind/`, `templates/`, `static/`; целевой
каталог — [app/](../app/). Добавлены продуктовые ограничения доступа,
неизменяемые версии объёма и описанные ниже расширения. MIT SpeedPy сохранена.

## boxyhq-invitation-flow-S1

Источник: https://github.com/boxyhq/saas-starter-kit

Ревизия: `abc9b686823cbfb4973c79bc36fea37a3244be6c`.

Исходные пути: `prisma/schema.prisma`, `components/auth/JoinWithInvitation.tsx`.

Целевые пути: [app/templates/account/signup.html](../app/templates/account/signup.html), [app/usermodel/adapters.py](../app/usermodel/adapters.py).

Invitation signup UX adapted to Django/allauth; read-only expected email plus independent server-side verified-address checks. React/Formik/NextAuth code not imported.

## apptension-scoping-pattern-S1

Источник: https://github.com/apptension/saas-boilerplate

Ревизия: `caa46d26e01f9cd2c29f0e67bb72ff073c755053`.

Исходные пути: `packages/backend/common/graphql/mutations.py`.

Целевые пути: [app/mainapp/services/scope_control.py](../app/mainapp/services/scope_control.py), [app/mainapp/tests/test_scope_control.py](../app/mainapp/tests/test_scope_control.py).

Tenant-scoped object lookup and negative cross-tenant scenarios adapted to TeamMembership/Django. Graphene mutation and its dependencies not copied.

## speedpy-analysis-execution-S2a

Источник: https://github.com/speedpy/speedpy

Ревизия: `2cd0043c474b145f8be395eec4b8d31483c8daa0`.

Исходные пути: `project/celeryapp.py`, `mainapp/models/jobs.py`, `mainapp/tasks/jobs.py`, `mainapp/tasks/teams.py`.

Целевые пути: [app/project/celeryapp.py](../app/project/celeryapp.py), [app/mainapp/services/analysis.py](../app/mainapp/services/analysis.py), [app/mainapp/tasks/analysis.py](../app/mainapp/tasks/analysis.py).

Retained SpeedPy Celery/ORM; adapted per-record transactional recovery principle to version-bound runs/attempts.

Kept already credited Apptension scoped-lookup principle; no Graphene/second auth/ORM.

Added native domain validation, leases, quota serialization and UUID-only task payload; generic demo job not reused.

## speedpy-yandex-execution-extension-S2b

Источник: https://github.com/speedpy/speedpy

Ревизия: `2cd0043c474b145f8be395eec4b8d31483c8daa0`.

Исходные пути: `project/celeryapp.py`, `mainapp/models/jobs.py`, `mainapp/tasks/jobs.py`, `mainapp/tasks/teams.py`.

Целевые пути: [app/project/celeryapp.py](../app/project/celeryapp.py), [app/mainapp/services/analysis.py](../app/mainapp/services/analysis.py), [app/mainapp/tasks/analysis.py](../app/mainapp/tasks/analysis.py), [app/mainapp/services/analysis_provider.py](../app/mainapp/services/analysis_provider.py), [app/mainapp/services/analysis_budget.py](../app/mainapp/services/analysis_budget.py).

Extend retained SpeedPy execution and previously credited Apptension tenant scoping with native Yandex HTTP, immutable provider profile and global reservation/settlement.

New provider/prompt/schema/budget code authored for Scope Control; no Yandex SDK or other donor source copied. API documentation is protocol evidence.

## nextjs-activity-principle-S3a

Источник: https://github.com/nextjs/saas-starter

Ревизия: `6e33e58b1e553a41fe22e6b941a7229a002de361`.

Исходные пути: `lib/db/schema.ts`, `LICENSE`.

Целевые пути: [app/mainapp/models/change_control.py](../app/mainapp/models/change_control.py), [app/mainapp/services/change_control.py](../app/mainapp/services/change_control.py), [app/mainapp/tests/test_scope_change_control.py](../app/mainapp/tests/test_scope_change_control.py).

Adapt actor/action/time within request-derived team/project to append-only human revision/evidence records.

No Drizzle/TypeScript code, Next.js runtime, IP retention or second ORM; existing SpeedPy/Apptension scoping retained.

## speedpy-project-lifecycle-S3b

Источник: https://github.com/speedpy/speedpy

Ревизия: `2cd0043c474b145f8be395eec4b8d31483c8daa0`.

Исходные пути: `mainapp/models/teams.py`, `mainapp/tasks/teams.py`.

Целевые пути: [app/mainapp/models/data_lifecycle.py](../app/mainapp/models/data_lifecycle.py), [app/mainapp/services/data_lifecycle.py](../app/mainapp/services/data_lifecycle.py), [app/speedpycom/management/commands/purge_scope_projects.py](../app/speedpycom/management/commands/purge_scope_projects.py).

Scope to one project, separate owner intent from quiesced operator purge and restore anti-resurrection inventory.

No Frappe platform or Team/account automatic deletion hooks; preserve donor eligibility for later useful components.

## speedpy-project-workflow-ui-S4a

Источник: https://github.com/speedpy/speedpy

Ревизия: `2cd0043c474b145f8be395eec4b8d31483c8daa0`.

Исходные пути: `templates/mainapp/layouts/sidebar_layout.html`, `static/mainapp/input.css`.

Целевые пути: [app/templates/mainapp/scope_control/_workflow_guide.html](../app/templates/mainapp/scope_control/_workflow_guide.html), [app/templates/mainapp/scope_control/project_detail.html](../app/templates/mainapp/scope_control/project_detail.html), [app/static/mainapp/styles.css](../app/static/mainapp/styles.css).

Four-step request-bound display and next action, explicit manual mode, readonly roles and stale-version branches.

## Поставляемые ресурсы

| Ресурс | Происхождение | Лицензия |
| --- | --- | --- |
| Inter font | rsms/inter | SIL OFL1.1, полный текст в app/static/fonts/OFL-Inter.txt |
| Alpine.js | alpinejs/alpine v3.15.3 | MIT, полный текст в NOTICE |
| Driver.js | kamranahmedse/driver.js (ныне nilbuild/driver.js) | MIT, полный текст в NOTICE |
| htmx | bigskysoftware/htmx v2.0.8 | 0BSD, полный текст в NOTICE |

Для htmx v2.0.8 файл `dist/htmx.min.js` побайтово сопоставлен с поставляемым
`app/static/vendors/htmx.min.js`. Лицензия получена из той же ревизии.

## Зависимости и эксплуатация

Python/Node зависимости зафиксированы в `app/uv.lock` и `app/package-lock.json`.
Их собственные лицензии сохраняются у соответствующих пакетов. PostgreSQL и
Redis запускаются отдельно; Redis7.4.7 использует RSALv2/SSPLv1. MIT starter
не покрывает эти runtime-условия. Коммерческое размещение требует проверки
применимых условий конкретной установки.

Из Frappe и Open SaaS код в текущую версию не заимствован. Их рассмотрение
при выборе платформы само по себе не является заимствованием.

## hallmark-terrazzo-ui-20261108

Источник: https://github.com/Nutlope/hallmark

Ревизия: `407fa689f4a276f2fd389208d271d0fef08f5058`. MIT, Copyright (c) 2026 Hallmark contributors.
Полный текст лицензии сохранён в [NOTICE](../app/NOTICE.md).

Исходные файлы: `site/themes/terrazzo/tokens.css`, `style.css`, `base.css`,
`skills/hallmark/references/themes/terrazzo.md`. Адаптированы бумажная палитра,
чернильные кнопки, терракотовый акцент, прямые углы, рамки, жёсткие тени,
типографика и оформление этапов. Целевые файлы: `app/static/mainapp/input.css`,
`styles.css`, `app/tailwind.config.js`, `app/templates/base.html`,
`app/templates/mainapp/layouts/sidebar_layout.html`, шаблоны навигации,
`project_list.html`, `_workflow_guide.html` и локальный SVG-знак.
Фотографии, JavaScript и рекламный контент Hallmark не заимствованы.
Используются существующие Django/Tailwind/Alpine; новых runtime-зависимостей нет.

Шрифт Onest из темы заменяет Syne также в заголовках ради поддержки кириллицы.
Неизменённый файл `ofl/onest/Onest[wght].ttf` из google/fonts, ревизия
`4c1db3aec83c67dd223dd82a68c039cab30917a9`, поставляется локально как
`app/static/fonts/Onest-Variable.ttf` под SIL OFL1.1. Полный текст:
[OFL Onest](../app/static/fonts/OFL-Onest.txt). Внешние сервисы шрифтов
не вызываются; уведомления Inter сохранены.

Локальная проверка на синтетических данных: сборка CSS,65 существующих тестов,
26 состояний Chrome с полным сценарием до новой версии объёма и10 дополнительных
состояний на финальной сборке CSS. Проверены навигация, формы, фото, светлая/тёмная
тема и мобильный экран. Это проверка оформления, не готовность к production.
