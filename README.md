<!-- ADMIT case: cases/dinara-abdesh -->
# StockSim AI — Симулятор фондового рынка с AI Coach

This is a separate AI prototype copy of the original StockSim app. It adds an AI Coach layer that explains why stocks may move by connecting live price changes to market signals, news categories, sector trends, and risk.

The original `/Users/yerassyl/stock-simulator` project is not edited by this copy.

Учебный симулятор для школьников и студентов. Торгуй 50 реальными акциями с виртуальными $10,000.

## Быстрый старт

### 1. Установить зависимости

```bash
pip install -r requirements.txt
```

### 2. Настроить базу данных

По умолчанию приложение использует локальный SQLite-файл `stock_simulator.db`.
Для Supabase/Postgres создай `.env` на основе `.env.example` и укажи:

```bash
DATABASE_URL=postgresql://postgres:YOUR-PASSWORD@db.gmqomudxrooioipkuzlj.supabase.co:5432/postgres?sslmode=require
SECRET_KEY=replace-with-a-long-random-secret
ADMIN_PASSWORDS=replace-with-admin-password
```

Для Vercel/serverless лучше использовать Supabase pooler connection string, если он доступен.
Пароль базы данных не коммить в репозиторий.
На Vercel `/admin` не принимает локальные demo-пароли; нужно задать `ADMIN_PASSWORDS`.

### 3. Запустить приложение

```bash
python app.py
```

При первом запуске:
- автоматически создаются таблицы в SQLite или Postgres/Supabase
- загружаются актуальные котировки через yfinance (занимает ~20-30 секунд)

### 4. Открыть в браузере

```
http://localhost:5000
```

---

## Что умеет приложение

| Страница | Описание |
|----------|----------|
| `/login` | Регистрация и вход. При регистрации выдаётся **$10,000** |
| `/dashboard` | Баланс, стоимость портфеля, график, топ-5 позиций |
| `/market` | Список 50 акций с ценами. Поиск, кнопка «Купить» |
| `/portfolio` | Все открытые позиции с прибылью/убытком |
| `/leaderboard` | Рейтинг всех участников по доходности |
| `/admin` | Админ-страница со статистикой пользователей и транзакций |

## Доступные акции (50 штук)

`AAPL` `MSFT` `GOOGL` `AMZN` `TSLA` `META` `NVDA` `NFLX`
`JPM` `V` `WMT` `DIS` `PYPL` `INTC` `AMD` `BABA` `UBER`
`LYFT` `SNAP` `ABNB` `SPOT` `SHOP` `SQ` `COIN` `HOOD`
`NKE` `MCD` `SBUX` `KO` `PEP` `JNJ` `PFE` `MRNA` `ABBV`
`XOM` `CVX` `BA` `GE` `F` `GM` `RIVN` `LCID` `PLTR` `RBLX`
`U` `DKNG` `PENN` `MGM` `WYNN` `LVS`

## Требования

- Python 3.9+
- Интернет-соединение (для загрузки котировок)

## Структура проекта

```
stock-simulator/
├── app.py           # Flask-приложение, все маршруты
├── database.py      # Инициализация SQLite
├── stocks.py        # Работа с yfinance, список тикеров
├── scheduler.py     # Обновление цен каждый день в 09:00
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── dashboard.html
│   ├── market.html
│   ├── portfolio.html
│   └── leaderboard.html
├── static/
│   ├── style.css
│   └── script.js
├── requirements.txt
└── README.md
```

## Технологии

- **Backend**: Python + Flask + Flask-Login
- **База данных**: SQLite locally, Postgres/Supabase when `DATABASE_URL` is set
- **Котировки**: yfinance (обновляются ежедневно в 09:00)
- **Frontend**: HTML + CSS + JavaScript (без фреймворков)
- **Графики**: Chart.js
