# Double Bite

Платформа онлайн-замовлення їжі з AI-чатботом підтримки 24/7.

| Releases & Framework | Data & Infrastructure | Security & AI | CI & Project Info |
| :---: | :---: | :---: | :---: |
| [![DoubleBite Release](https://img.shields.io/badge/DoubleBite-v1.0.0-blue.svg?logo=github&logoColor=white)](https://github.com/iDeash123/DoubleBite)<br>[![Python Versions](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg?logo=python&logoColor=white)](https://www.python.org/)<br>[![Django Version](https://img.shields.io/badge/django-6.0%2B-092E20.svg?logo=django&logoColor=white)](https://www.djangoproject.com/)<br>[![Frontend Stack](https://img.shields.io/badge/frontend-HTMX%202%20%7C%20Alpine-336699.svg?logo=htmx&logoColor=white)](https://htmx.org/) | [![PostgreSQL](https://img.shields.io/badge/postgresql-16%2B_(pgvector)-336791.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org/)<br>[![Redis](https://img.shields.io/badge/redis-7--alpine-DC382D.svg?logo=redis&logoColor=white)](https://redis.io/)<br>[![Mailpit](https://img.shields.io/badge/mailpit-SMTP_dev-2E7D32.svg?logo=maildotru&logoColor=white)](https://mailpit.axllent.org/)<br>[![Stripe API](https://img.shields.io/badge/stripe-7.0%2B_(HMAC)-635BFF.svg?logo=stripe&logoColor=white)](https://stripe.com/) | [![Gemini API](https://img.shields.io/badge/Gemini_API-2.5_Flash-4285F4.svg?logo=googlegemini&logoColor=white)](https://ai.google.dev/)<br>[![IDOR Protection](https://img.shields.io/badge/IDOR-protected-brightgreen.svg?logo=auth0&logoColor=white)](#ai-чатбот-підтримки)<br>[![Ruff Security](https://img.shields.io/badge/Ruff_S%2BBLE-strict-brightgreen.svg?logo=ruff&logoColor=white)](#тестування-та-перевірки-якості)<br>[![Tailwind CSS](https://img.shields.io/badge/tailwind-standalone_cli-38B2AC.svg?logo=tailwind-css&logoColor=white)](https://tailwindcss.com/) | [![CI](https://img.shields.io/badge/CI-passing-brightgreen.svg?logo=github-actions&logoColor=white)](#тестування-та-перевірки-якості)<br>[![Pytest Suite](https://img.shields.io/badge/tests-630%20tests-brightgreen.svg?logo=pytest&logoColor=white)](#тестування-та-перевірки-якості)<br>[![uv](https://img.shields.io/badge/%E2%9A%A1-uv-261230.svg?logo=uv&logoColor=white)](https://astral.sh/uv)<br>[![Ruff](https://img.shields.io/badge/%E2%9A%A1-Ruff-000000.svg?logo=ruff&logoColor=white)](https://github.com/astral-sh/ruff)<br>[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) |

---

## Можливості

- 🛒 Каталог страв з фільтрацією за категорією, ціною, дієтою та алергенами (HTMX 2 + нативні Django 6 Template Partials)
- 💳 Оплата через Stripe Checkout (картка, Apple/Google Pay) або при доставці (готівка / термінал кур'єру) з ідемпотентними webhook'ами та поверненнями
- 🤖 AI-чатбот на Google Gemini API з multi-round tool calling (`stream_async`) — керує кошиком, перевіряє статус замовлення, шукає по меню (RAG через PostgreSQL `pgvector` HNSW) та ескалує звернення оператору
- 📦 Трекінг замовлень у реальному часі (HTMX polling, state machine `PENDING → PAID → PREPARING → ON_WAY → DELIVERED` для `ONLINE` та `PENDING → PREPARING → ON_WAY → DELIVERED` для `CASH`/`CARD`, захищені гостьові посилання `?t=`)
- 🔐 Авторизація: email/пароль з обов'язковою верифікацією email + Google та GitHub OAuth 2.0 (`django-allauth` без автоперезапису паролів), атомарний Redis rate limiting (`HTTP 429`)
- 📧 Транзакційні email через `transaction.on_commit()`: підтвердження замовлення, чеки Stripe, повернення коштів, статуси доставки, скидання пароля, ескалація тікетів (Mailpit у dev)
- 🔍 SEO: `sitemap.xml`, `robots.txt`, ASCII-слаги, canonical, Open Graph, Schema.org JSON-LD (`json_script`)
- 🎨 UI без зовнішніх CDN — Tailwind CSS (standalone CLI), WhiteNoise для статики у prod, локальні шрифти, HTMX, Alpine.js, Lenis

---

## Скріншоти

<details>
<summary>📸 Розгорнути</summary>

### Головна · Каталог · Страва
| Головна сторінка | Каталог страв | Сторінка страви |
|:---:|:---:|:---:|
| ![Hero](docs/screenshots/hero_showcase.png) | ![Menu](docs/screenshots/menu_catalog.png) | ![Dish](docs/screenshots/dish_detail_main.png) |
| Hero-секція з перемикачем страв та макросами КБЖВ | Фільтрація за категоріями, пошук, дієтичні бейджі | Опис, алергени, модифікатори, додавання в кошик |

### Кошик · Чекаут · Профіль
| Шторка кошика | Повний кошик | Чекаут | Профіль |
|:---:|:---:|:---:|:---:|
| ![Drawer](docs/screenshots/cart_view_min.png) | ![Cart](docs/screenshots/cart_view_main.png) | ![Checkout](docs/screenshots/checkout.png) | ![Profile](docs/screenshots/profile.png) |
| Бічна панель на Alpine.js | Перелік позицій та вартість | Форма доставки, вибір оплати | Адреси, історія замовлень |

### Авторизація · AI-чатбот
| Вхід / Реєстрація | AI-чатбот підтримки |
|:---:|:---:|
| ![Auth](docs/screenshots/auth_login.png) | <img src="docs/screenshots/ai_concierge_chat.png" alt="AI Chat" width="400"> |
| Email + Google/GitHub OAuth 2.0 | Стрімінг відповідей (SSE), tool calling, RAG |

</details>

---

## Стек

**Backend:** Django 6.0+, Uvicorn (ASGI) / WSGI, WhiteNoise, PostgreSQL 16 (`pgvector`), Redis 7  
**Frontend:** HTMX 2, Alpine.js, Tailwind CSS (standalone CLI), Lenis  
**AI:** Google Gemini API (`GEMINI_MODEL`, наприклад `gemini-2.5-flash`), `pgvector` RAG (1024-d `gemini-embedding-2` + `HnswIndex`)  
**Payments:** Stripe Checkout Sessions + Webhooks (HMAC) + Refunds (`idempotency_key`)  
**Auth:** `django-allauth` (Google, GitHub OAuth 2.0, обов'язкова верифікація email)  
**Infra:** Docker Compose (PostgreSQL 16 + `pgvector`, Redis 7, Mailpit), `uv`  
**Quality:** `pytest` + `pytest-django` (630 тестів), Ruff (строгі правила безпеки `S` + `BLE`)

---

## Швидкий старт

### Передумови

- Python 3.12+
- [uv](https://astral.sh/uv) (менеджер пакетів)
- Docker + Docker Compose
- Node.js (опційно — для тестів JS-санітайзера markdown `test_chat_markdown.py`)

### Встановлення

```bash
# 1. Клонувати репозиторій
git clone https://github.com/iDeash123/DoubleBite.git
cd double-bite

# 2. Встановити залежності
uv sync

# 3. Налаштувати змінні оточення
cp .env.example .env
# Заповніть обов'язкові значення: DB_PASSWORD, REDIS_PASSWORD, REDIS_URL,
# а також GEMINI_API_KEY, STRIPE_*, GOOGLE/GITHUB OAuth (за потреби)

# 4. Підняти інфраструктуру (PostgreSQL + pgvector, Redis, Mailpit)
docker compose up -d

# 5. Застосувати міграції
uv run python double-bite/manage.py migrate

# 6. Наповнити БД тестовими даними (100+ страв, FAQ, тестові акаунти)
uv run python double-bite/manage.py seed_db --force

# 7. Запустити сервер (ASGI — рекомендовано для SSE-стрімінгу AI)
uv run uvicorn config.asgi:application --app-dir double-bite --reload --port 8000

# Альтернативно (WSGI, також підтримує SSE через вбудований адаптер):
uv run python double-bite/manage.py runserver
```

Сайт: **http://localhost:8000**

### Корисні management-команди

```bash
# Перебудувати RAG-ембедінги страв через Google Gemini API (pgvector)
uv run python double-bite/manage.py rebuild_dish_embeddings --only-missing
uv run python double-bite/manage.py rebuild_dish_embeddings --force

# Очистити покинуті анонімні/порожні кошики (dry-run або реальне видалення)
uv run python double-bite/manage.py cleanup_abandoned_carts --days 7 --dry-run
uv run python double-bite/manage.py cleanup_abandoned_carts --days 7

# Очищення БД або генерація без завантаження зображень
uv run python double-bite/manage.py seed_db --force --clean-only
uv run python double-bite/manage.py seed_db --force --skip-images

# Снапшоти БД (зі скиданням PostgreSQL sequences)
uv run python double-bite/manage.py dump_snapshot
uv run python double-bite/manage.py load_snapshot
```

### Тестові акаунти (`DEBUG=True`)

| Роль | Email | Пароль |
|:---|:---|:---|
| Адміністратор | `admin@doublebite.com` | `admin12345` (або `ADMIN_PASSWORD` у `.env`) |
| Клієнт | `customer@doublebite.com` | `customer12345` (або `SEED_CUSTOMER_PASSWORD`) |
| Кур'єр | `courier@doublebite.com` | `courier12345` (або `SEED_COURIER_PASSWORD`) |

### Корисні URL

| Сервіс | URL |
|:---|:---|
| Сайт | [http://localhost:8000/](http://localhost:8000/) |
| Django Admin | [http://localhost:8000/admin/](http://localhost:8000/admin/) *(або шлях з `DJANGO_ADMIN_URL`)* |
| Mailpit (email) | [http://localhost:8025/](http://localhost:8025/) |
| Sitemap сайту | [http://localhost:8000/sitemap.xml](http://localhost:8000/sitemap.xml) |

---

## AI-чатбот підтримки

Чатбот працює на **Google Gemini API** (`GEMINI_MODEL`, наприклад `gemini-2.5-flash` у `.env.example`, OpenAI-compatible SSE endpoint). Відповіді стрімяться через SSE (ASGI `uvicorn` та WSGI-адаптер без `DJANGO_ALLOW_ASYNC_UNSAFE`) із multi-round tool calling (`max_rounds=3`), таймаутами та блокуванням AI-відповідей після ескалації на оператора (`HTTP 409`). Мова відповіді визначається автоматично за повідомленням клієнта.

```mermaid
flowchart TD
    User([Клієнт]) -->|SSE| StreamView[chat_stream_view]
    StreamView --> Agent[GeminiSupportAgent]
    Agent -->|Multi-round Stream + Tools| LLM{Google Gemini API}

    LLM --> add_to_cart[add_to_cart]
    LLM --> update_cart[update_cart_quantity]
    LLM --> remove_cart[remove_from_cart]
    LLM --> view_cart[view_cart]
    LLM --> check_status[check_order_status]
    LLM --> search[search_dishes — pgvector RAG]
    LLM --> faq[get_faq_answer]
    LLM --> escalate[escalate_to_operator]

    add_to_cart -->|SSE cart_mutation + /orders/cart/badge/| UI[Оновлення кошика]
    escalate -->|on_commit Email + ESCALATED lock| Admin[Адміністратор]
    LLM -->|Потокові токени| User
```

### Tools

| Інструмент | Опис |
|:---|:---|
| `add_to_cart` | Додавання страви за ID або назвою (з валідацією доступності) |
| `update_cart_quantity` | Зміна кількості (0 = видалення, обмеження `<= 99`, підтримка `cart_item_id`) |
| `remove_from_cart` | Видалення страви з кошика |
| `view_cart` | Поточний вміст кошика (включно з `selected_options`) та сума |
| `check_order_status` | Статус замовлення за `order_number` або ID (з перевіркою власника — захист від IDOR) |
| `search_dishes` | Семантичний пошук по меню (`pgvector` `CosineDistance`, SQL-фільтри до `limit`, поріг релевантності) |
| `get_faq_answer` | Ранжовані відповіді з бази знань (доставка, оплата, графік) |
| `escalate_to_operator` | Атомарне створення тікету + блокування AI + email адміністратору після коміту |

---

## Тестування та перевірки якості

```bash
# Швидкий прогон усіх тестів (SQLite + LocMemCache: 621 passed, 9 Postgres/Redis-only skipped)
uv run python -m pytest

# Повний E2E-прогон проти реальних PostgreSQL + pgvector + Redis (docker compose up -d: 630 passed)
TEST_WITH_POSTGRES=1 TEST_WITH_REDIS=1 uv run python -m pytest

# По модулях
uv run python -m pytest double-bite/support/tests/   # AI-агент, tools, RAG, IDOR, SSE (129)
uv run python -m pytest double-bite/orders/tests/    # Кошик, Stripe, state machine, трекінг (159)
uv run python -m pytest double-bite/accounts/tests/  # Акаунти, OAuth, адреси, 429 lockout (110)
uv run python -m pytest double-bite/menu/tests/      # Каталог, вітрина, SEO, snapshots, seed (123)
uv run python -m pytest double-bite/config/tests/    # Прод-налаштування, XFF/rate-limit, email (93)
uv run python -m pytest double-bite/tests_e2e/       # PostgreSQL/pgvector, Redis Lua, concurrency, E2E (16)

# Строгий лінтер (включно з правилами безпеки Ruff S та BLE)
uv run ruff check .

# Перевірка міграцій та прод-конфігурації (DEBUG=False)
uv run python double-bite/manage.py migrate --check
uv run python double-bite/manage.py check --deploy --fail-level WARNING
```

---

## Структура проєкту

```text
double-bite/
├── double-bite/              # Django-проєкт
│   ├── accounts/             # Користувачі, OAuth 2.0, профілі, адреси доставки
│   ├── menu/                 # Головна вітрина, каталог страв, категорії, SEO, snapshots, embeddings
│   ├── orders/               # Кошик, замовлення, Stripe Checkout/Webhooks/Refunds, трекінг
│   ├── support/              # AI-чатбот (Google Gemini API, multi-round tool calling, pgvector RAG, SSE, пульт оператора)
│   │   └── agent/            # client.py, tools.py, prompts.py
│   ├── config/               # settings.py, security.py, transactions.py, validators.py, urls.py, asgi.py
│   ├── tests_e2e/            # Інтеграційні E2E та concurrency тести (PostgreSQL, pgvector, Redis, ASGI/WSGI)
│   ├── static/               # CSS, JS, шрифти (все локально, без CDN, WhiteNoise у prod)
│   └── templates/            # Django-шаблони + Django 6 partials + email-шаблони
├── docs/screenshots/         # Скріншоти інтерфейсу
├── docker-compose.yml        # PostgreSQL 16 (pgvector), Redis 7, Mailpit
├── pyproject.toml            # Залежності (uv) та строгі правила Ruff (E, F, I, UP, B, BLE, S)
├── uv.lock                   # Lockfile
├── pytest.ini                # Конфігурація pytest
├── .env.example              # Зразок змінних оточення
└── LICENSE                   # MIT
```

---

## Ліцензія

[MIT](LICENSE)
