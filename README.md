# MailWorker

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![tests](https://github.com/olegkeatzin/mailworker/actions/workflows/tests.yml/badge.svg)](https://github.com/olegkeatzin/mailworker/actions/workflows/tests.yml)

Служба, которая автоматически разбирает входящую почту с помощью ИИ и
регистрирует документы в журнале учёта на Firebird. Без интерфейса: настройки —
в одном `config.ini`, запуск — службой, планировщиком или в Docker.

```
письмо → вложения (PDF/DOCX/XLSX/сканы) → OCR → модель
       → справочники KORRESP/THEMA → запись в CARD
```

## Возможности

- IMAP и POP3, опрос по расписанию, окно «за последние N дней».
- Разбор письма и вложений: PDF (текстовый слой и сканы), DOCX, XLSX,
  изображения, пересланные письма вложением `.eml`.
- OCR сканов отдельной моделью; основной модели — текст и список тем из справочника.
- Корреспондент и тема берутся из справочников базы (`KORRESP`, `THEMA`),
  а не придумываются моделью.
- Запись напрямую в Firebird (таблица `CARD`) или через HTTP-эндпоинт журнала.
- Защита от повторной записи, повтор ошибок, архив исходных писем.
- Пробный прогон без записи, проверка настроек и узлов одной командой.

## Как это работает

1. Служба подключается к ящику и забирает письма: флаг «непрочитано» (IMAP)
   и окно «за последние N дней» — независимо друг от друга. IMAP ищет по дате
   на сервере, POP3 фильтрует локально по заголовку `Date`.
2. Вложения готовятся к отправке: текст извлекается, страницы PDF рендерятся,
   сканы распознаются OCR.
3. Модель получает текст письма, вложений и список тем справочника и возвращает
   JSON: содержание документов, номер, дату, отправителя, листы, тему.
4. Корреспондент ищется в `KORRESP` (по закреплённому адресу или названию),
   тема — в `THEMA`; в журнал пишутся их идентификаторы и канонические названия.
5. Запись уходит в `CARD` одной транзакцией; письмо помечается обработанным,
   исходник сохраняется в архиве.

## Быстрый старт

### Docker (рекомендуется)

```bash
git clone https://github.com/olegkeatzin/mailworker.git
cd mailworker

mkdir -p config data
cp config.ini.example config/config.ini
# отредактируйте config/config.ini: почта, база, модель
```

В контейнере данные лежат в томе `/app/data`, поэтому пути в `.ini` должны быть
абсолютными:

```ini
[processing]
state_path = /app/data/state.sqlite
archive_dir = /app/data/processed

[logging]
file = /app/data/mailworker.log
```

На Linux каталог `data` должен быть доступен пользователю контейнера (UID 10001):

```bash
sudo chown -R 10001:10001 data
```

Запуск:

```bash
docker compose up -d --build                  # собрать и запустить
docker compose logs -f                        # журнал работы
docker compose run --rm mailworker --check    # проверить настройки
docker compose run --rm mailworker --once     # один проход
docker compose down                           # остановить
```

Обновление после `git pull`:

```bash
docker compose up -d --build
```

### Локально

```bash
git clone https://github.com/olegkeatzin/mailworker.git
cd mailworker

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.ini.example config.ini    # затем заполнить [mail], [ai], [database]

python main.py --check              # проверить узлы
python main.py --eml "письмо.eml"   # разобрать письмо без записи
python main.py                      # рабочий запуск
```

Для записи в Firebird нужен пакет `fdb` и клиентская библиотека Firebird
(`fbclient.dll` / `libfbclient.so`). Путь задаётся в `[database] fb_client_library`,
если библиотека не видна в системе. В Docker она уже установлена.

## Настройка

Всё задаётся в `config.ini`; полный список с пояснениями — в
[`config.ini.example`](config.ini.example).

### `[mail]` — ящик

| Ключ | Значение |
|---|---|
| `protocol` | `imap` или `pop3` |
| `host`, `port`, `tls` | сервер; `tls`: `ssl`, `starttls`, `none` |
| `username`, `password` | учётная запись ящика |
| `mailbox` | папка IMAP (обычно `INBOX`) |
| `mode` | `unseen` (непрочитанные) или `all` (все); `since_days` тоже принимается как `all` + окно по дате |
| `since_days` | окно по дате: N последних календарных дней, включая сегодня; `0` — без ограничения. Работает в любом режиме: IMAP ищет на сервере, POP3 фильтрует локально по заголовку `Date` |
| `mark_seen` | помечать письмо прочитанным после записи |
| `delete_after` | удалять письмо после записи — только POP3 |
| `poll_interval`, `max_per_cycle` | пауза между проходами и писем за проход |

### Окно `since_days` во времени

- Окно пересчитывается **при каждом опросе**, а не один раз при старте: сегодня
  это «с 09.11.2026», завтра — «с 10.11.2026». Служба может работать неделями,
  дата не «застывает».
- Письма, выпавшие из окна, не читаются, даже если остались на сервере:
  со временем письмо уходит из окна, а не появляется в нём.
- Если служба не работала дольше окна, письма за время простоя не обработаются.
  Для таких случаев увеличьте `since_days` или поставьте `0` (без ограничения).
- За один проход обрабатывается не больше `max_per_cycle` писем — выбираются
  последние по порядку поступления. Для ящика, где письма накапливаются,
  увеличьте `max_per_cycle` или включите удаление после обработки (POP3).
- Стартовая строка лога показывает окно на момент запуска; в каждом проходе
  окно печатается заново (`окно с …`).

### `[database]` — Firebird

| Ключ | Значение |
|---|---|
| `host`, `database`, `user`, `password` | подключение к базе журнала |
| `charset` | `WIN1251` для существующей базы |
| `fb_client_library` | путь к клиентской библиотеке Firebird |
| `date_format` | `string` (даты строкой `ДД.ММ.ГГГГ`) или `date` |
| `sanitize_text` | заменять символы, которых нет в кодировке базы |

### `[ai]` и `[ocr]` — модели

| Ключ | Значение |
|---|---|
| `base_url` | OpenAI-совместимый эндпоинт, обычно с `/v1` |
| `api_key`, `model` | ключ и имя модели (как в `GET /models`) |
| `disable_thinking` | отключить «размышления» reasoning-моделей |
| `multimodal` | передавать картинки на OCR |
| `ocr_max_pages`, `text_max_pages` | лимиты страниц PDF |
| `[ocr] base_url/model` | отдельная модель распознавания (пусто — основная) |
| `system_prompt_file`, `user_template_file` | свои промпты файлами |

### `[classifiers]` — справочники

| Ключ | Значение |
|---|---|
| `enabled` | брать корреспондента и тему из базы |
| `refresh_minutes` | как часто перечитывать справочники (0 — при запуске) |
| `correspondents_sql`, `themes_sql` | запросы к KORRESP и THEMA: колонки `ID, NAME, NUMBPART, SHOWS` |
| `email_map_file` | необязательный CSV «адрес → корреспондент» |
| `match_threshold` | порог похожести названий (0.5–1.0) |
| `unknown_correspondent` | `model`, `none`, `skip`, `create` |
| `unknown_thema` | `none`, `skip`, `create` |
| `themes_in_prompt` | сколько названий тем передавать модели |

### `[journal]` — запись

| Ключ | Значение |
|---|---|
| `storage` | `db` — напрямую в Firebird, `api` — POST на HTTP-эндпоинт журнала |
| `api_url`, `api_timeout` | адрес и таймаут эндпоинта |
| `card_type`, `reg_number` | `CARD_TYPE` и `INTINDEX` |
| `reg_date` | `today` или `email_date` |
| `doc_date_fallback` | `email_date`, `reg_date`, `today`, `none` |
| `doc_number_fallback` | `none`, `subject`, `regex` |
| `sheets_mode` | `pdf_pages`, `attachments`, `analysis`, `one` |
| `max_*_length` | длины колонок `CARD` для обрезки |

### `[processing]` — отбор

| Ключ | Значение |
|---|---|
| `filter_mode` | `all` (по умолчанию), `ai`, `with_attachments` |
| `skip_spam` | не регистрировать спам (работает в любом режиме) |
| `require_register_flag`, `min_confidence` | только для `ai`: решение модели и порог уверенности |
| `retry_errors` | повторять письма после ошибки |
| `state_path` | файл учёта обработанных писем |
| `archive_dir`, `save_eml` | куда складывать исходные письма |

### `[logging]`

Уровень, файл с ротацией, вывод в консоль.

## Справочники: корреспондент и тема

Данные читаются из таблиц `KORRESP` и `THEMA` той же базы, что и `CARD`:
они загружаются при старте и перечитываются каждые `refresh_minutes` минут.
В журнал пишутся `KORRESP` = `KORRESP.NAME`, `KORRESPID` = `KORRESP.ID`,
`THEMA` = `THEMA.ID`.

**Корреспондент.** Модель называет организацию из бланка или подписи. Служба
сравнивает её с названиями из `KORRESP` (без кавычек, «ё», ООО/АО/ПАО,
пунктуации) и берёт запись при похожести не ниже `match_threshold`. Если не
найдено — действует `unknown_correspondent`. В `KORRESP` нет почтовых адресов,
поэтому точное закрепление адреса за корреспондентом — необязательный файл
`classifiers/correspondent_emails.csv` (образец рядом).

**Тема.** Список названий из `THEMA` передаётся модели прямо в промпте: она
обязана выбрать одно или `null`, служба сверяет выбор с базой. Если не найдено —
`unknown_thema`.

Пополнение справочников (`create`) по умолчанию выключено: записи в справочнике
документов лучше заводить осознанно.

## Журнал: поля `CARD`

| Поле | Что попадает | Ограничение |
|---|---|---|
| `CARD_TYPE` | `[journal] card_type` | — |
| `INTINDEX` | `[journal] reg_number` («новая») | 20 |
| `INTDATE` | дата регистрации | — |
| `EXTINDEX` | номер документа от контрагента | 20 |
| `EXTDATE` | дата документа | — |
| `SH_CONTENTS` | краткое содержание **приложенных документов** (если вложений нет — письма) | 512 |
| `PAGES_DOC` | количество листов | — |
| `KORRESP` / `KORRESPID` | название и ID корреспондента из справочника | 100 / — |
| `THEMA` | ID темы из справочника (0 — не определена) | — |

Тексты обрезаются по длинам колонок: иначе Firebird откажет «string right
truncation». Запись выполняется одной транзакцией: номер из генератора `ID_CARD`,
затем `INSERT` с параметрами.

## Отбор писем

| `filter_mode` | Что регистрируется | Решение модели |
|---|---|---|
| `all` (по умолчанию) | все письма | не учитывается |
| `ai` | где модель сказала «регистрировать» и уверенность ≥ `min_confidence` | учитывается |
| `with_attachments` | только письма с вложениями | не учитывается |

Состав полей в запросе к модели зависит от режима: при `all` и
`with_attachments` полей `register`, `register_reason` и `confidence` в схеме
нет — модель их не вычисляет. Так же убирается `category`, если `skip_spam = false`.
Ответ короче, лишние числа не отвлекают модель.

## Ящик с пересылкой писем

Если в служебный ящик пересылают отобранные письма:

- **`From` — пересылающий, а не корреспондент.** Для писем с признаком пересылки
  адрес и имя из `From` не участвуют в поиске по справочнику; корреспондент
  берётся из бланка пересланного письма, и модели это прямо сказано.
- **Исходное письмо вложением `.eml`** разбирается как письмо: в промпт попадают
  его отправитель, дата, тема и текст, а его вложения (PDF, DOCX, сканы)
  обрабатываются обычно, включая OCR и подсчёт листов.
- **Отбор** по умолчанию `all`: письма уже отобраны, служба их не перепроверяет.

## Команды

| Команда | Что делает |
|---|---|
| `python main.py` | опрос ящика по расписанию |
| `python main.py --once` | один проход и выход |
| `python main.py --check` | проверка настроек, почты, модели, журнала и справочников |
| `python main.py --eml файл.eml` | разбор письма, без записи |
| `python main.py --eml файл.eml --write` | разбор с записью в журнал |
| `--json`, `--log-level`, `--config` | машинный вывод, уровень лога, другой файл настроек |

## Развёртывание

### Docker Compose

`docker-compose.yml` собирает образ из `Dockerfile`, монтирует `./config`
только для чтения и `./data` для состояния, архива и логов; контейнер
перезапускается сам (`restart: unless-stopped`). Часовой пояс задаётся
переменной `TZ`.

```bash
TZ=Asia/Novosibirsk docker compose up -d
```

### systemd (Linux)

```ini
[Unit]
Description=MailWorker — обработка ящика в журнал учёта
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/mailworker
ExecStart=/opt/mailworker/venv/bin/python main.py --config /opt/mailworker/config.ini
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### Служба Windows (NSSM)

```cmd
nssm install MailWorker "C:\Python312\python.exe" "C:\mailworker\main.py --config C:\mailworker\config.ini"
nssm set MailWorker AppDirectory C:\mailworker
nssm set MailWorker Start SERVICE_AUTO_START
nssm start MailWorker
```

## Диагностика

| Сообщение | Что проверить |
|---|---|
| «Файл настроек не найден» | путь в `--config` |
| «проверьте настройки — …» | список неверных ключей сразу |
| «Не найдена клиентская библиотека Firebird» | `[database] fb_client_library` |
| «Сервер … не принял логин или пароль» | `[mail] username/password` |
| «Папка … не найдена» | `[mail] mailbox` |
| «Модель вернула HTTP 404» | `[ai] base_url` должен заканчиваться на `/v1` |
| «Не удалось прочитать справочник KORRESP» | `[classifiers] correspondents_sql` |
| «Письмо … пропущено: корреспондент … не найден» | CSV адресов или `unknown_correspondent` |
| «Ответ модели не является JSON» | размер модели, `disable_thinking` |

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest
```

264 теста: настройки, разбор писем и вложений (PDF/DOCX/XLSX/картинки/вложенные
`.eml`), промпт и разбор ответа модели, поля журнала и обрезка по длинам колонок,
`INSERT` с `KORRESPID`/`THEMA` и HTTP-эндпоинт, справочники и политики, дедупликация,
конвейер, проход по ящику, IMAP/POP3 (включая окно `since_days`), командная строка.

CI ([workflow](.github/workflows/tests.yml)) прогоняет тесты и собирает Docker-образ
на каждый push и pull request.

## Структура

```
mailworker/
├── main.py                     точка входа
├── processor/
│   ├── cli.py                  команды и проверки
│   ├── config.py               разбор и проверка .ini
│   ├── classifiers.py          сопоставление со справочниками
│   ├── pipeline.py             конвейер обработки письма
│   ├── service.py              опрос ящика
│   ├── state.py                учёт обработанных писем
│   ├── mail/                   разбор .eml, IMAP, POP3
│   ├── attachments/            PDF, DOCX, XLSX, изображения
│   ├── ai/                     промпт, клиент модели, OCR, разбор JSON
│   └── journal/                поля CARD, Firebird, HTTP, справочники
├── classifiers/                образец файла адресов
├── tests/                      автотесты
├── config.ini.example          шаблон настроек
├── Dockerfile, docker-compose.yml
├── requirements.txt, requirements-dev.txt
├── pyproject.toml, Makefile
└── .github/workflows/tests.yml
```

## Ограничения

- Схема `CARD` не меняется: служба пишет перечисленные колонки. Если в базе
  появятся новые обязательные колонки, `INSERT` дополняется в
  `processor/journal/firebird.py`.
- Архивы (`.zip`, `.rar`) не распаковываются — вложение помечается как
  непроанализированное.
- Адресов корреспондентов в базе нет: точная связь адреса с записью `KORRESP` —
  только через необязательный CSV; сопоставление по названию работает без него.
- При `storage = api` тема в HTTP-эндпоинт не передаётся: в его контракте такого
  поля нет, `THEMA` пишется только при прямой записи в базу.

## Благодарности

Логика разбора письма и вложений основана на проекте
[MailSense](https://github.com/olegkeatzin/mailsense); здесь она переработана под
автоматическую регистрацию документов и запись в журнал.

## Лицензия

[MIT](LICENSE).