# VPS-раннер каталога pipensx

Автономный планировщик на базе Docker Compose:
- **`flaresolverr`**: решает Cloudflare и передает сессионные куки для доступа к спискам файлов (`viewtorrent.php`).
- **`catalog-runner`**: догружает новые и обновленные раздачи в персистентную SQLite-базу (`/state/catalog.db`), обогащает их через TitleDB (`pipensx-metadata`), валидирует по `schema.json` v2 и выпускает новый GitHub Release при наличии изменений.
- **Ветка `main` не засоряется** тяжелыми автокоммитами (все релизы выходят строго в GitHub Releases `latest`).

---

## 1. Подготовка на VPS

Склонируй репозиторий (или скопируй только папку `runner/`):
```bash
git clone https://github.com/i3sey/pipensx-catalog.git /opt/pipensx-catalog
cd /opt/pipensx-catalog/runner
cp env.example .env
chmod 600 .env
```

Заполни параметры в `.env`:
```ini
GITHUB_REPO=i3sey/pipensx-catalog
# GitHub Personal Access Token (classic со scope "repo" или fine-grained с "Contents: Read & Write")
GITHUB_TOKEN=ghp_...

# RuTracker cookies из браузера (bb_data и bb_session)
RUTRACKER_COOKIE=bb_data=...; bb_session=...; bb_ssl=1

# Сколько свежих страниц форума сканировать за раз (по умолчанию 3 страницы = ~150 тем)
PAGES=3
DELAY=1.5
```

---

## 2. Тестовый запуск вручную

Стандартный запуск (проверяет изменения, если каталог не менялся — ничего не делает):
```bash
cd /opt/pipensx-catalog/runner
docker compose up --build
```

### Принудительный перевыпуск релиза (Force Flag)
Чтобы принудительно выпустить и перезалить релиз на GitHub (для первой проверки или сброса), используй любой из способов:

**Способ 1 (через переменную окружения):**
```bash
FORCE_PUBLISH=1 docker compose up
```
*(или задай `FORCE_PUBLISH=1` в файле `.env`)*

**Способ 2 (через флаг CLI):**
```bash
docker compose run --rm catalog-runner --force
```

В логах отобразятся подробные шаги:
- Проверка связи с FlareSolverr и RuTracker.
- Количество тем и статус в базе данных.
- Список обновленных/обогащенных раздач.
- Загрузка каждого файла (`catalog.json`, `manifest.json`, `report.md`) в GitHub Releases с точным размером.
- Прямая ссылка на созданный релиз: `https://github.com/i3sey/pipensx-catalog/releases/latest`.

---

## 3. Автозапуск по ночам (Cron на хосте)

Открой crontab на VPS (`crontab -e`) и добавь запуск раз в сутки (например, в 03:30 ночи):

```cron
30 3 * * * cd /opt/pipensx-catalog/runner && docker compose pull catalog-runner && docker compose up --abort-on-container-exit >> /var/log/catalog-runner.log 2>&1
```

- `docker compose pull catalog-runner` автоматически подтянет свежий образ из GHCR, если в ветку `main` были внесены изменения.
- Контейнер отработает за 1–2 минуты и завершится. Днем 0% потребления RAM/CPU.

---

## 4. CI/CD (GitHub Actions)

В репозитории настроен `.github/workflows/docker-ci.yml`:
- При каждом `git push` в ветку `main` GitHub Actions:
  1. Запускает unit-тесты (`unittest`).
  2. Собирает Docker-образ и публикует его в GitHub Container Registry (`ghcr.io/i3sey/pipensx-catalog:latest`).
- Ваш VPS автоматически получит обновленную версию раннера при следующем тике cron (`docker compose pull`).
