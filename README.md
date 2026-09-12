<div align="center">

# 999md-mcp

[English](#english) · [Русский](#русский)

**An MCP server for [999.md](https://999.md), Moldova's main classifieds board.**<br>
**MCP-сервер для [999.md](https://999.md) — главной доски объявлений Молдовы.**

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![fastmcp](https://img.shields.io/badge/fastmcp-4.x-6E56CF)
![transport](https://img.shields.io/badge/transport-stdio%20%7C%20http-555)
![read--only](https://img.shields.io/badge/mode-read--only-2EA043)
[![tests](https://github.com/ilodezis/999md-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/ilodezis/999md-mcp/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

</div>

---

## English

Search flats, cars and phones straight from Claude, with filters, price statistics and the seller's phone number.

### What it is

A read-only MCP server on top of the GraphQL API that 999.md uses for its own website
(`https://999.md/graphql`). No account, API key or cookies needed. The official
[Partners API](https://partners-api.999.md/api/documentation) does not fit this job: it is paid
and only manages *your own* ads, with no search at all.

### Tools

| Tool | What it does |
|---|---|
| 🔎 `search` | Search by text, category (id / path / 999.md link), filters, price in EUR/USD/MDL, sorting, paging |
| 📄 `get_ad` | Full ad: description, characteristics, amenities, address with coordinates, **phone**, seller, photo/video links |
| 🖼 `photos` | The ad's photos as images the model can actually see. Costly in context: ~550 tokens per compact photo (768 px, whole frame), ~1500 with `full_resolution`. 4 photos by default, page with `offset` |
| 🎛 `get_filters` | A subcategory's filters with feature and option ids, exactly what `search` accepts |
| 🔗 `get_options` | Dependent options: cities of a region, sectors of a city, models of a brand |
| 🗂 `categories` | Category tree with live ad counts; with `query`, which subcategories the query lives in |
| 👤 `seller_ads` | Seller profile (member since, verification, business plan) and their ads |
| 📊 `price_stats` | 999.md's own price statistics: median, average, min/max, sample size |

**Typical flow:** `categories` → `get_filters` → `search` → `get_ad`.

### Example

> "A two-room flat for monthly rent in Chișinău, up to €600"

```jsonc
{
  "category": "real-estate/apartments-and-rooms",
  "price_max": 600,
  "currency": "EUR",
  "filters": [
    {"feature_id": 1,   "option_ids": [912]},    // offer type: monthly rent
    {"feature_id": 7,   "option_ids": [12900]},  // region: Chișinău mun.
    {"feature_id": 241, "option_ids": [894]}     // 2 rooms
  ]
}
```

999.md converts currencies server-side, so a price filter also catches ads priced in lei and dollars.

### Installation

Requires [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ilodezis/999md-mcp.git
cd 999md-mcp
uv sync
```

**Claude Code:**

```bash
claude mcp add 999md --scope user -- uv run --directory /path/to/999md-mcp python server.py
```

**Claude Desktop, Cursor and other clients** (`claude_desktop_config.json` or equivalent):

```json
{
  "mcpServers": {
    "999md": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/999md-mcp", "python", "server.py"]
    }
  }
}
```

| Variable | Default | What it does |
|---|---|---|
| `SITE_LANG` | `ru` | Language of titles, options and descriptions: `ru` or `ro` |

### Remote mode (Claude.ai)

`remote.py` serves the same server over Streamable HTTP at `/mcp`, behind a single-user OAuth gate
(authorization code + PKCE S256). Claude.ai opens a consent page, the owner types the password once,
and from then on Claude calls the server with a signed Bearer token valid for a year. Nothing is stored:
authorization codes live in memory for 5 minutes, tokens are HMAC-signed. Rotating `TOKEN_SECRET` revokes every token.

```bash
cp .env.example .env            # password, signing secret, client secret
docker compose up -d --build    # listens on 127.0.0.1:8013; terminate TLS on a reverse proxy
```

| Variable | Required | What it does |
|---|---|---|
| `PASSWORD` | yes | Password typed on the consent page |
| `TOKEN_SECRET` | yes | Token signing key |
| `CLIENT_SECRET` | yes | OAuth client secret |
| `CLIENT_ID` | no | OAuth client id, `999md-claude-connector` by default |
| `REDIRECT_URIS` | no | Comma-separated allowed callbacks, Claude.ai's by default |

In Claude.ai: **Settings → Connectors → Add custom connector**, URL `https://<your-domain>/mcp`,
then put `CLIENT_ID` and `CLIENT_SECRET` under **Advanced settings**.

### How the site's API works

Reverse-engineered from the 999.md frontend (Next.js), not from any documentation:

- `POST /graphql`, no authentication; schema introspection is open.
- The `lang: ru|ro` header sets the language of translated fields.
- `searchAds.filters`: the server ignores `filterId` and only looks at `featureId`. Features inside one
  group are combined with **OR**, groups with each other with **AND**, so every feature goes into its own group.
- An invalid category path silently turns into the root category (`id: 0`); the server catches this and returns a clear error.
- Feature ids that are the same in every category: `1` offer type, `2` price, `7/8/9` region/city/sector,
  `13` text, `14` photos, `16` contacts.

### Limitations

- **Read-only.** Posting, favourites and chat are left out on purpose.
- **Throttled** to 5 requests per second. No hard rate limits turned up during testing.
- **Personal use.** 999.md's terms forbid extracting, collecting or systematising its content without consent.
  This server is not a scraper for bulk downloads.
- The API is unofficial and can change without notice. The live tests catch that right away, and CI runs them weekly.

### Tests

```bash
uv run python -m pytest -q                   # offline + live tests against the real 999.md
OFFLINE=1 uv run python -m pytest -q   # offline only
```

Offline tests cover parsing and formatting; live tests check the contract with 999.md through a real MCP client.

### Disclaimer

An independent project, not affiliated with or endorsed by 999.md or Simpals. All ads and their content
belong to their authors and to 999.md.

---

## Русский

Поиск квартир, машин, телефонов прямо из Claude: с фильтрами, ценовой статистикой и телефоном продавца.

### Что это

Read-only MCP поверх собственного GraphQL, которым 999.md кормит свой же сайт
(`https://999.md/graphql`): без аккаунта, API-ключа и куков. Официальный
[Partners API](https://partners-api.999.md/api/documentation) сюда не подходит — он платный и
только для управления *своими* объявлениями, поиска в нём нет.

### Инструменты

| Tool | Что делает |
|---|---|
| 🔎 `search` | Поиск: текст, категория (id / путь / ссылка 999.md), фильтры, цена в EUR/USD/MDL, сортировка, пагинация |
| 📄 `get_ad` | Карточка целиком: описание, характеристики, удобства, адрес с координатами, **телефон**, продавец, фото/видео |
| 🖼 `photos` | Сами фото объявления картинками, чтобы модель их видела. Дорого по контексту: ~550 токенов на сжатое фото (768 px, кадр целиком), ~1500 с `full_resolution`. По умолчанию 4 штуки, дальше `offset` |
| 🎛 `get_filters` | Фильтры подкатегории с id фич и опций — ровно то, что принимает `search` |
| 🔗 `get_options` | Зависимые опции: города региона, секторы города, модели марки |
| 🗂 `categories` | Дерево категорий со счётчиками; с `query` — в каких подкатегориях живёт запрос |
| 👤 `seller_ads` | Профиль продавца (с какого года, верификация, бизнес-план) и его объявления |
| 📊 `price_stats` | Ценовая статистика самого 999.md: медиана, среднее, min/max, размер выборки |

**Типичный путь:** `categories` → `get_filters` → `search` → `get_ad`.

### Пример

> «Двушка в аренду помесячно, Кишинёв, до 600 €»

```jsonc
{
  "category": "real-estate/apartments-and-rooms",
  "price_max": 600,
  "currency": "EUR",
  "filters": [
    {"feature_id": 1,   "option_ids": [912]},    // тип предложения: сдаю помесячно
    {"feature_id": 7,   "option_ids": [12900]},  // регион: Кишинёв мун.
    {"feature_id": 241, "option_ids": [894]}     // 2-комнатная
  ]
}
```

Валюту сервер 999.md конвертирует сам, так что фильтр по цене ловит и объявления в леях и долларах.

### Установка

Нужен [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ilodezis/999md-mcp.git
cd 999md-mcp
uv sync
```

**Claude Code:**

```bash
claude mcp add 999md --scope user -- uv run --directory /path/to/999md-mcp python server.py
```

**Claude Desktop, Cursor и другие клиенты** (`claude_desktop_config.json` или аналог):

```json
{
  "mcpServers": {
    "999md": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/999md-mcp", "python", "server.py"]
    }
  }
}
```

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `SITE_LANG` | `ru` | Язык названий, опций и описаний: `ru` или `ro` |

### Удалённый режим (Claude.ai)

`remote.py` отдаёт тот же сервер по Streamable HTTP на `/mcp` за однопользовательским OAuth
(authorization code + PKCE S256). Claude.ai открывает страницу согласия, владелец один раз вводит пароль,
дальше Claude ходит с подписанным Bearer-токеном на год. Хранилища нет: коды авторизации живут в памяти
5 минут, токены подписаны HMAC. Смена `TOKEN_SECRET` отзывает все токены.

```bash
cp .env.example .env            # пароль, секрет подписи, client secret
docker compose up -d --build    # слушает 127.0.0.1:8013, TLS — на reverse proxy
```

| Переменная | Обязательна | Что делает |
|---|---|---|
| `PASSWORD` | да | Пароль на странице согласия |
| `TOKEN_SECRET` | да | Ключ подписи токенов |
| `CLIENT_SECRET` | да | OAuth client secret |
| `CLIENT_ID` | нет | OAuth client id, по умолчанию `999md-claude-connector` |
| `REDIRECT_URIS` | нет | Разрешённые callback через запятую, по умолчанию — Claude.ai |

В Claude.ai: **Settings → Connectors → Add custom connector**, URL `https://<домен>/mcp`,
в **Advanced settings** — `CLIENT_ID` и `CLIENT_SECRET`.

### Как устроено API сайта

Разведано по фронтенду 999.md (Next.js), не по документации:

- `POST /graphql`, авторизация не нужна; интроспекция схемы открыта.
- Язык переводимых полей задаёт заголовок `lang: ru|ro`.
- `searchAds.filters`: сервер игнорирует `filterId`, смотрит только `featureId`. Фичи внутри одной
  группы объединяются по **OR**, группы между собой — по **AND**. Поэтому каждая фича кладётся в свою группу.
- Невалидный путь категории API молча превращает в корневую (`id: 0`) — сервер это ловит и отдаёт понятную ошибку.
- Фичи с постоянными id во всех категориях: `1` — тип предложения, `2` — цена, `7/8/9` — регион/город/сектор,
  `13` — текст, `14` — фото, `16` — контакты.

### Ограничения

- **Только чтение.** Постинг, избранное, чат не реализованы намеренно.
- **Троттлинг** 5 запросов/с. Жёстких лимитов у сайта при проверке не нашлось.
- **Личное использование.** Правила 999.md запрещают без согласия «извлекать из базы, собирать,
  систематизировать» контент — сервер не для массовой выгрузки.
- API неофициальное и может поменяться без предупреждения. Живые тесты это сразу покажут, CI гоняет их раз в неделю.

### Тесты

```bash
uv run python -m pytest -q                   # офлайн + живые против настоящего 999.md
OFFLINE=1 uv run python -m pytest -q   # только офлайн
```

Офлайн-тесты проверяют разбор и форматирование, живые — контракт с 999.md через настоящий MCP-клиент.

### Дисклеймер

Независимый проект, не связан с 999.md и Simpals и не одобрен ими. Объявления и их содержимое принадлежат
авторам и 999.md.

---

## License / Лицензия

[MIT](LICENSE)
