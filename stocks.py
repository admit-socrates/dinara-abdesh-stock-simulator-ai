import yfinance as yf
from database import get_connection

TICKERS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'META', 'NVDA', 'NFLX',
    'JPM', 'V', 'WMT', 'DIS', 'PYPL', 'INTC', 'AMD', 'BABA', 'UBER',
    'LYFT', 'SNAP', 'ABNB', 'SPOT', 'SHOP', 'SQ', 'COIN', 'HOOD',
    'NKE', 'MCD', 'SBUX', 'KO', 'PEP', 'JNJ', 'PFE', 'MRNA', 'ABBV',
    'XOM', 'CVX', 'BA', 'GE', 'F', 'GM', 'RIVN', 'LCID', 'PLTR', 'RBLX',
    'U', 'DKNG', 'PENN', 'MGM', 'WYNN', 'LVS',
]

TICKER_NAMES = {
    'AAPL': 'Apple Inc.',
    'MSFT': 'Microsoft Corp.',
    'GOOGL': 'Alphabet Inc.',
    'AMZN': 'Amazon.com Inc.',
    'TSLA': 'Tesla Inc.',
    'META': 'Meta Platforms',
    'NVDA': 'NVIDIA Corp.',
    'NFLX': 'Netflix Inc.',
    'JPM': 'JPMorgan Chase',
    'V': 'Visa Inc.',
    'WMT': 'Walmart Inc.',
    'DIS': 'Walt Disney Co.',
    'PYPL': 'PayPal Holdings',
    'INTC': 'Intel Corp.',
    'AMD': 'Advanced Micro Devices',
    'BABA': 'Alibaba Group',
    'UBER': 'Uber Technologies',
    'LYFT': 'Lyft Inc.',
    'SNAP': 'Snap Inc.',
    'ABNB': 'Airbnb Inc.',
    'SPOT': 'Spotify Technology',
    'SHOP': 'Shopify Inc.',
    'SQ': 'Block Inc.',
    'COIN': 'Coinbase Global',
    'HOOD': 'Robinhood Markets',
    'NKE': 'Nike Inc.',
    'MCD': "McDonald's Corp.",
    'SBUX': 'Starbucks Corp.',
    'KO': 'Coca-Cola Co.',
    'PEP': 'PepsiCo Inc.',
    'JNJ': 'Johnson & Johnson',
    'PFE': 'Pfizer Inc.',
    'MRNA': 'Moderna Inc.',
    'ABBV': 'AbbVie Inc.',
    'XOM': 'Exxon Mobil Corp.',
    'CVX': 'Chevron Corp.',
    'BA': 'Boeing Co.',
    'GE': 'GE Aerospace',
    'F': 'Ford Motor Co.',
    'GM': 'General Motors',
    'RIVN': 'Rivian Automotive',
    'LCID': 'Lucid Group',
    'PLTR': 'Palantir Technologies',
    'RBLX': 'Roblox Corp.',
    'U': 'Unity Software',
    'DKNG': 'DraftKings Inc.',
    'PENN': 'PENN Entertainment',
    'MGM': 'MGM Resorts',
    'WYNN': 'Wynn Resorts',
    'LVS': 'Las Vegas Sands',
}


def update_all_prices():
    """Fetch current prices for all tickers via yfinance and save to DB."""
    print("Fetching stock prices from yfinance...")
    conn = get_connection()
    updated = 0
    try:
        # Download all tickers in one request (much faster)
        data = yf.download(
            ' '.join(TICKERS),
            period='5d',
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        if data.empty:
            print("No data returned from yfinance")
            return

        # Multi-ticker download returns MultiIndex columns: (field, ticker)
        close = data['Close'] if 'Close' in data.columns.get_level_values(0) else data

        for ticker in TICKERS:
            try:
                if ticker not in close.columns:
                    continue
                series = close[ticker].dropna()
                if len(series) == 0:
                    continue
                price = float(series.iloc[-1])
                prev_close = float(series.iloc[-2]) if len(series) >= 2 else price
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
                name = TICKER_NAMES.get(ticker, ticker)

                conn.execute('''
                    INSERT INTO stock_prices (ticker, name, price, prev_close, change_pct, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(ticker) DO UPDATE SET
                        name       = excluded.name,
                        price      = excluded.price,
                        prev_close = excluded.prev_close,
                        change_pct = excluded.change_pct,
                        updated_at = excluded.updated_at
                ''', (ticker, name, price, prev_close, change_pct))
                updated += 1
            except Exception as e:
                print(f"  Error processing {ticker}: {e}")

        conn.commit()
        print(f"Updated {updated} stock prices.")
    except Exception as e:
        print(f"Error in update_all_prices: {e}")
    finally:
        conn.close()


def ensure_prices_loaded():
    """On startup: fetch prices only if DB has fewer than 10 cached entries."""
    conn = get_connection()
    try:
        count = conn.execute('SELECT COUNT(*) AS cnt FROM stock_prices').fetchone()['cnt']
    finally:
        conn.close()

    if count < 10:
        update_all_prices()
    else:
        print(f"Using {count} cached stock prices.")


def get_cached_prices():
    """Return {ticker: {price, name, change_pct, ...}} from DB cache."""
    conn = get_connection()
    try:
        rows = conn.execute('SELECT * FROM stock_prices').fetchall()
        return {r['ticker']: dict(r) for r in rows}
    finally:
        conn.close()
