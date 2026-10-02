# VPS-раннер каталога

Раз в 6 часов докачивает окно топиков с RuTracker, сливает с прошлым
снапшотом и публикует релиз, если данные изменились. Один контейнер на
один тик крона, внутри демонов нет.

## Требования

- Любой VPS с Docker + Docker Compose v2 и выходом в интернет.
- ~1 ГБ диска (образ ~320 МБ + state ~25 МБ + запас), 1 CPU / 512 МБ RAM.
- GitHub-токен с записью в Contents репозитория каталога (публикация).
- Куки RuTracker (скрапинг). Без них раннер просто держит прошлый
  снапшот — ничего не ломает, но и не обновляет.

## Установка

```bash
git clone https://github.com/i3sey/pipensx-catalog.git /opt/pipensx-catalog
cp /opt/pipensx-catalog/runner/env.example /opt/pipensx-catalog/runner/.env
chmod 600 /opt/pipensx-catalog/runner/.env
```

Заполни `.env`:

| Переменная | Обязательно | Что это |
|---|---|---|
| `GITHUB_REPO` | да | `i3sey/pipensx-catalog` (уже в примере) |
| `GITHUB_TOKEN` | для публикации | Classic PAT со scope `repo` или fine-grained с Contents read/write |
| `RUTRACKER_COOKIE` | для скрапинга | Полное значение Cookie из браузера (см. ниже) |
| `SCRAPER_PROXY` | нет | Прокси только для трафика скрапера, напр. `socks5h://127.0.0.1:1080` |
| `TOPICS_WINDOW` | нет | Топиков за тик, дефолт 60 |
| `SCRAPE_DELAY` | нет | Пауза между запросами, дефолт 3 (сек) |
| `TOPICS` | нет | Явный список топиков вместо окна ротации |

**Куки из браузера** (Chrome/Firefox + расширение Cookie-Editor):
1. Залогинься на rutracker.org и пройди проверку Cloudflare вручную.
2. В Cookie-Editor скопируй значения и собери одной строкой:
   `bb_guid=...; bb_t=...; bb_session=...; bb_ssl=1; cf_clearance=...`
   (`cf_clearance` — самое важное, это пропуск через проверку; без него
   почти наверняка будет challenge).
3. Вставь строкой в `RUTRACKER_COOKIE`. Никуда больше куки не копируй,
   в git она попасть не может (`.env` не коммитится — проверь `git status`).

## Проверка перед кроном (обязательно)

```bash
cd /opt/pipensx-catalog
MODE=probe docker compose -f runner/docker-compose.yml --env-file runner/.env up --build
```

Смотри матрицу `direct / proxy / cookie / proxy+cookie`:
- хотя бы одна строка без `challenge=True` и без `ERROR` — скрапинг
  с этого VPS будет работать;
- везде challenge — этот VPS для скрапинга не годится (IP дата-центра
  в блоке). Раннер всё равно можно держать: он будет честно держать
  прошлый снапшот, а обновления делать домашними прогонами.

Пробный тик без публикации (токен можно пока не вписывать):

```bash
docker compose -f runner/docker-compose.yml --env-file runner/.env up --build
```

В конце должно быть `unchanged (...)` или `blocked by anti-bot` — оба
исхода штатные. Раннер никогда не публикует битое: вето покрытия (exit 2)
и проверка sha перед релизом встроены.

## Крон

```cron
0 */6 * * * docker compose -f /opt/pipensx-catalog/runner/docker-compose.yml --env-file /opt/pipensx-catalog/runner/.env up --build --quiet-pull >/var/log/catalog-runner.log 2>&1
```

Логи тиков — в `/var/log/catalog-runner.log`. Что там искать:
- `unchanged (...)` — всё свежее, делать нечего;
- `publishing catalog-...` + `uploaded` — вышел новый релиз;
- `VETO from coverage gate` — merge просел >2%: релиз НЕ вышел,
  разбирайся (обычно — массовый бан топиков или сломанный парсер);
- `blocked by anti-bot` — тик пропущен, следующий попробует снова.

Обновление самого раннера (новый Dockerfile/скрипты):

```bash
cd /opt/pipensx-catalog && git pull
```

(`--build` в кроне сам пересоберёт образ при изменениях.)

## Кука протухла

Симптом: раньше была строка без challenge, теперь везде challenge.
Лечение: выгрузи куки из браузера заново, обнови `RUTRACKER_COOKIE`
в `.env`. Сессии живут месяцами, процедура редкая.

## Неполадки

| Симптом | Причина / действие |
|---|---|
| `snapshot download failed, ... nothing to do` | Нет сети до GitHub или пустой state при первом запуске без сети — проверь связь, перезапусти |
| `scrape failed rc=1` | Упал сам fetch всех топиков (сеть) — следующий тик повторит |
| Контейнер ест больше 512 МБ | Подними лимит в `docker-compose.yml`, парсинг 23 МБ JSON пиково хочет ~200–300 МБ |
| Релиз создался, а коммит в main нет | Смотри лог `git push` в конце тика; повторный тик докоммитит (идемпотентно) |
