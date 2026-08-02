import json
import os
import sqlite3
from datetime import datetime, timedelta, date

try:
    from psycopg2.errors import UniqueViolation as _PgUniqueViolation
except ImportError:
    _PgUniqueViolation = type('_Never', (Exception,), {})


def _parse_dt(value):
    """Parse a timestamp that is either a string (SQLite) or datetime (PostgreSQL)."""
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value  # already a datetime object from psycopg2


def _fmt_dt(value):
    """Format a timestamp for chart labels — works for both backends."""
    if isinstance(value, str):
        return value[:16].replace('T', ' ')
    if value is not None:
        return value.strftime('%Y-%m-%d %H:%M')
    return ''

from flask import Flask, render_template, redirect, url_for, request, jsonify, flash, session
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user, UserMixin,
)
from werkzeug.security import generate_password_hash, check_password_hash

from database import STARTING_CASH, init_db, get_db, close_db, IS_POSTGRES
from stocks import TICKERS, TICKER_NAMES, ensure_prices_loaded
import ai

import logging
import math

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('stocksim')

app = Flask(__name__)

_DEFAULT_SECRET = 'stock-sim-dev-secret'
app.secret_key = os.environ.get('SECRET_KEY', _DEFAULT_SECRET)
_ON_VERCEL = bool(os.environ.get('VERCEL'))
if _ON_VERCEL and app.secret_key == _DEFAULT_SECRET:
    # Sessions signed with the public dev key can be forged. Loud, not fatal
    # (crashing prod would be worse), but this must never be the case live.
    logger.critical('SECRET_KEY is unset in production — set it in the Vercel env.')

# Harden the session cookie. On Vercel (HTTPS) also require Secure.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',   # blocks the cookie on cross-site POSTs (CSRF)
    SESSION_COOKIE_SECURE=_ON_VERCEL,
)

SESSION_DEMO_MODE = _ON_VERCEL and not IS_POSTGRES

# Largest order we will accept, to reject absurd/overflow share counts.
MAX_SHARES = 1_000_000.0


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp


def _is_cross_origin():
    """
    Defense-in-depth CSRF check for state-changing JSON endpoints: reject a
    request whose Origin/Referer host is not our own. SameSite=Lax already
    blocks the session cookie cross-site; this is the belt to that suspenders.
    """
    origin = request.headers.get('Origin') or request.headers.get('Referer')
    if not origin:
        return False  # non-browser client (curl/tests) — allow
    from urllib.parse import urlsplit
    return urlsplit(origin).netloc != urlsplit(request.host_url).netloc


def _valid_shares(raw):
    """Return a positive, finite, capped float or None if invalid."""
    try:
        shares = float(raw)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(shares) or shares <= 0 or shares > MAX_SHARES:
        return None
    return shares


# Max AI-coach calls per user per day. Protects the paid API from abuse/cost.
try:
    AI_DAILY_LIMIT = int(os.environ.get('AI_DAILY_LIMIT', '40'))
except ValueError:
    AI_DAILY_LIMIT = 40


def ai_quota_check_and_increment(db, user_id):
    """
    Atomically consume one unit of the user's daily AI quota.
    Returns (allowed: bool, remaining_after: int). In demo mode (no DB) the
    count lives in the session instead.
    """
    today = date.today().isoformat()

    if SESSION_DEMO_MODE:
        used = session.get('ai_used', {})
        n = int(used.get(today, 0))
        if n >= AI_DAILY_LIMIT:
            return False, 0
        used = {today: n + 1}  # keep only today
        session['ai_used'] = used
        session.modified = True
        return True, AI_DAILY_LIMIT - (n + 1)

    row = db.execute(
        'SELECT count FROM ai_usage WHERE user_id = ? AND day = ?',
        (user_id, today),
    ).fetchone()
    used = row['count'] if row else 0
    if used >= AI_DAILY_LIMIT:
        return False, 0

    if row:
        db.execute(
            'UPDATE ai_usage SET count = count + 1 WHERE user_id = ? AND day = ?',
            (user_id, today),
        )
    else:
        db.execute(
            'INSERT INTO ai_usage (user_id, day, count) VALUES (?, ?, 1)',
            (user_id, today),
        )
    db.commit()
    return True, AI_DAILY_LIMIT - (used + 1)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

app.teardown_appcontext(close_db)
_runtime_ready = False


def ensure_runtime_ready():
    global _runtime_ready
    if _runtime_ready:
        return
    init_db(app)
    with app.app_context():
        ensure_prices_loaded()
    _runtime_ready = True


@app.before_request
def bootstrap_runtime():
    ensure_runtime_ready()

# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

class User(UserMixin):
    def __init__(self, id, username, cash_balance):
        self.id = id
        self.username = username
        self.cash_balance = cash_balance

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    if SESSION_DEMO_MODE:
        demo_user = session.get('demo_user')
        if demo_user and str(demo_user.get('id')) == str(user_id):
            return User(demo_user['id'], demo_user['username'], demo_user['cash_balance'])
        return None

    row = get_db().execute(
        'SELECT id, username, cash_balance FROM users WHERE id = ?', (user_id,)
    ).fetchone()
    if row:
        return User(row['id'], row['username'], row['cash_balance'])
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def record_portfolio_value(user_id, db):
    """Snapshot the user's total portfolio value into history."""
    if SESSION_DEMO_MODE:
        cash, stocks_value, total, *_ = calc_portfolio(user_id, db)
        history = session.get('demo_history', [])
        history.append({
            'total_value': round(total, 2),
            'cash': round(cash, 2),
            'created_at': datetime.now().isoformat(timespec='minutes'),
        })
        session['demo_history'] = history[-30:]
        session.modified = True
        return

    user = db.execute(
        'SELECT cash_balance FROM users WHERE id = ?', (user_id,)
    ).fetchone()
    holdings = db.execute(
        'SELECT ticker, shares FROM holdings WHERE user_id = ? AND shares > 0',
        (user_id,),
    ).fetchall()
    prices = {
        r['ticker']: r['price']
        for r in db.execute('SELECT ticker, price FROM stock_prices').fetchall()
    }
    stocks_value = sum(h['shares'] * prices.get(h['ticker'], 0) for h in holdings)
    total = user['cash_balance'] + stocks_value

    db.execute(
        'INSERT INTO portfolio_history (user_id, total_value, cash) VALUES (?, ?, ?)',
        (user_id, total, user['cash_balance']),
    )
    db.commit()


def calc_portfolio(user_id, db):
    """Return (cash, stocks_value, total, pnl, pnl_pct, holdings_list)."""
    if SESSION_DEMO_MODE:
        demo_user = session.get('demo_user') or {
            'id': 'demo',
            'username': 'demo',
            'cash_balance': STARTING_CASH,
        }
        cash = float(demo_user.get('cash_balance', STARTING_CASH))
        demo_holdings = session.get('demo_holdings', {})
        prices = {
            r['ticker']: dict(r)
            for r in db.execute('SELECT * FROM stock_prices').fetchall()
        }

        holdings_list = []
        stocks_value = 0.0
        for ticker, h in demo_holdings.items():
            shares = float(h.get('shares', 0))
            if shares <= 0:
                continue
            avg_price = float(h.get('avg_price', 0))
            p = prices.get(ticker, {})
            cur_price = p.get('price', 0) or 0
            value = shares * cur_price
            stocks_value += value
            cost = shares * avg_price
            gain = value - cost
            gain_pct = (gain / cost * 100) if cost else 0
            holdings_list.append({
                'ticker': ticker,
                'name': TICKER_NAMES.get(ticker, ticker),
                'shares': shares,
                'avg_price': avg_price,
                'current_price': cur_price,
                'value': value,
                'gain': gain,
                'gain_pct': gain_pct,
                'change_pct': p.get('change_pct', 0) or 0,
            })

        total = cash + stocks_value
        pnl = total - STARTING_CASH
        pnl_pct = pnl / STARTING_CASH * 100
        return cash, stocks_value, total, pnl, pnl_pct, holdings_list

    user = db.execute(
        'SELECT cash_balance FROM users WHERE id = ?', (user_id,)
    ).fetchone()
    cash = user['cash_balance']

    holdings_rows = db.execute(
        'SELECT ticker, shares, avg_price FROM holdings WHERE user_id = ? AND shares > 0',
        (user_id,),
    ).fetchall()
    prices = {
        r['ticker']: dict(r)
        for r in db.execute('SELECT * FROM stock_prices').fetchall()
    }

    holdings_list = []
    stocks_value = 0.0
    for h in holdings_rows:
        t = h['ticker']
        p = prices.get(t, {})
        cur_price = p.get('price', 0) or 0
        value = h['shares'] * cur_price
        stocks_value += value
        cost = h['shares'] * h['avg_price']
        gain = value - cost
        gain_pct = (gain / cost * 100) if cost else 0
        holdings_list.append({
            'ticker': t,
            'name': TICKER_NAMES.get(t, t),
            'shares': h['shares'],
            'avg_price': h['avg_price'],
            'current_price': cur_price,
            'value': value,
            'gain': gain,
            'gain_pct': gain_pct,
            'change_pct': p.get('change_pct', 0) or 0,
        })

    total = cash + stocks_value
    pnl = total - STARTING_CASH
    pnl_pct = pnl / STARTING_CASH * 100
    return cash, stocks_value, total, pnl, pnl_pct, holdings_list


SECTOR_BY_TICKER = {
    'AAPL': 'consumer technology', 'MSFT': 'enterprise software and AI', 'GOOGL': 'search, ads, and AI',
    'AMZN': 'e-commerce and cloud', 'TSLA': 'electric vehicles', 'META': 'social media and ads',
    'NVDA': 'AI chips', 'NFLX': 'streaming', 'JPM': 'banking', 'V': 'payments', 'WMT': 'retail',
    'DIS': 'entertainment', 'PYPL': 'digital payments', 'INTC': 'semiconductors', 'AMD': 'semiconductors',
    'BABA': 'Chinese e-commerce', 'UBER': 'mobility', 'LYFT': 'mobility', 'SNAP': 'social media',
    'ABNB': 'travel', 'SPOT': 'music streaming', 'SHOP': 'e-commerce software', 'SQ': 'fintech',
    'COIN': 'crypto exchange', 'HOOD': 'retail brokerage', 'NKE': 'consumer apparel',
    'MCD': 'restaurants', 'SBUX': 'restaurants', 'KO': 'consumer staples', 'PEP': 'consumer staples',
    'JNJ': 'healthcare', 'PFE': 'pharmaceuticals', 'MRNA': 'biotech', 'ABBV': 'pharmaceuticals',
    'XOM': 'oil and gas', 'CVX': 'oil and gas', 'BA': 'aerospace', 'GE': 'industrial technology',
    'F': 'automobiles', 'GM': 'automobiles', 'RIVN': 'electric vehicles', 'LCID': 'electric vehicles',
    'PLTR': 'data analytics and AI', 'RBLX': 'gaming', 'U': 'game development software',
    'DKNG': 'sports betting', 'PENN': 'gaming and casinos', 'MGM': 'casinos and resorts',
    'WYNN': 'casinos and resorts', 'LVS': 'casinos and resorts',
}


CONTEXT_BY_SECTOR = {
    'AI chips': {
        'news': 'AI infrastructure demand, data-center spending, and semiconductor supply updates',
        'risk': 'valuation risk, export controls, and chip-cycle volatility',
    },
    'enterprise software and AI': {
        'news': 'cloud growth, AI product adoption, and enterprise IT budgets',
        'risk': 'slower corporate spending or pressure on software margins',
    },
    'semiconductors': {
        'news': 'chip demand, PC/server cycles, foundry supply, and AI-related orders',
        'risk': 'inventory cycles, export restrictions, and strong competition',
    },
    'electric vehicles': {
        'news': 'EV demand, battery costs, delivery numbers, and interest-rate expectations',
        'risk': 'price cuts, production delays, and high growth expectations',
    },
    'oil and gas': {
        'news': 'oil prices, OPEC decisions, geopolitical supply risk, and energy demand',
        'risk': 'commodity price swings and long-term energy transition pressure',
    },
    'banking': {
        'news': 'interest-rate expectations, credit quality, lending growth, and regulation',
        'risk': 'loan losses, deposit pressure, and market stress',
    },
    'retail': {
        'news': 'consumer spending, inflation, holiday demand, and inventory trends',
        'risk': 'margin pressure, weak consumer confidence, and supply-chain costs',
    },
    'fintech': {
        'news': 'payment volume, crypto sentiment, regulation, and user growth',
        'risk': 'regulatory changes, credit losses, and market sentiment',
    },
    'crypto exchange': {
        'news': 'crypto prices, ETF flows, regulation, and trading activity',
        'risk': 'regulatory risk and sharp crypto-market volatility',
    },
}


def sector_context(sector):
    return CONTEXT_BY_SECTOR.get(sector, {
        'news': 'company earnings, sector news, interest rates, and broader market sentiment',
        'risk': 'unexpected news, valuation changes, and market volatility',
    })


def build_ai_explanation(stock, holding=None):
    """Create a deterministic AI-style learning explanation from live price data."""
    ticker = stock.get('ticker')
    change = stock.get('change_pct')
    price = stock.get('price')
    sector = SECTOR_BY_TICKER.get(ticker, 'public markets')
    context = sector_context(sector)

    if change is None:
        direction = 'flat'
        move = 'does not have a fresh day-change signal yet'
        tone = 'neutral'
    elif change > 1:
        direction = 'up'
        move = f'is up {change:.2f}% today'
        tone = 'positive'
    elif change < -1:
        direction = 'down'
        move = f'is down {abs(change):.2f}% today'
        tone = 'negative'
    else:
        direction = 'mixed'
        move = f'is moving only {change:+.2f}% today'
        tone = 'neutral'

    if direction == 'up':
        why = f"The market may be reacting positively to {context['news']}."
        action = 'Ask whether the good news is temporary, or whether it changes the company long term.'
    elif direction == 'down':
        why = f"Investors may be worried about {context['risk']}."
        action = 'Ask whether the fall is a real business problem or just short-term market fear.'
    else:
        why = f"The market may be waiting for clearer signals from {context['news']}."
        action = 'Do not force a trade; compare the company story with the chart and recent news.'

    owned_note = None
    if holding:
        owned_note = (
            f"You own {holding['shares']:g} share(s). Your position is "
            f"{holding['gain_pct']:+.2f}% versus your average cost."
        )

    return {
        'ticker': ticker,
        'name': stock.get('name') or TICKER_NAMES.get(ticker, ticker),
        'price': price,
        'change_pct': change,
        'sector': sector,
        'tone': tone,
        'headline': f"{ticker} {move}.",
        'why': why,
        'news_signal': context['news'],
        'risk_signal': context['risk'],
        'student_takeaway': action,
        'owned_note': owned_note,
    }


def ai_market_snapshot(db, holdings_list=None):
    rows = [dict(r) for r in db.execute('SELECT * FROM stock_prices').fetchall()]
    holdings_by_ticker = {h['ticker']: h for h in (holdings_list or [])}
    movers = [r for r in rows if r.get('change_pct') is not None and r.get('price')]
    top_up = sorted(movers, key=lambda r: r['change_pct'], reverse=True)[:3]
    top_down = sorted(movers, key=lambda r: r['change_pct'])[:3]
    watch = []
    for row in top_up[:2] + top_down[:2]:
        row['name'] = row.get('name') or TICKER_NAMES.get(row['ticker'], row['ticker'])
        watch.append(build_ai_explanation(row, holdings_by_ticker.get(row['ticker'])))
    return watch


def admin_passwords():
    configured = os.environ.get('ADMIN_PASSWORDS') or os.environ.get('ADMIN_PASSWORD')
    if configured:
        return {p.strip() for p in configured.split(',') if p.strip()}
    if os.environ.get('VERCEL'):
        return set()
    return {'admin123', 'alua123', 'alua123!'}


def start_demo_session(username):
    session['demo_user'] = {
        'id': 'demo',
        'username': username,
        'cash_balance': STARTING_CASH,
    }
    session['demo_holdings'] = {}
    session['demo_history'] = [{
        'total_value': STARTING_CASH,
        'cash': STARTING_CASH,
        'created_at': datetime.now().isoformat(timespec='minutes'),
    }]
    session.modified = True
    login_user(User('demo', username, STARTING_CASH))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        action = request.form.get('action')
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()

        if SESSION_DEMO_MODE:
            if len(username) < 3:
                flash('Username must be at least 3 characters.', 'error')
            elif len(password) < 4:
                flash('Password must be at least 4 characters.', 'error')
            else:
                start_demo_session(username)
                if action == 'register':
                    flash(f'Welcome! You have been credited ${STARTING_CASH:,.0f} in virtual funds.', 'success')
                return redirect(url_for('dashboard'))
            return render_template('login.html', starting_cash=STARTING_CASH)

        if action == 'register':
            if len(username) < 3:
                flash('Username must be at least 3 characters.', 'error')
            elif len(password) < 4:
                flash('Password must be at least 4 characters.', 'error')
            elif db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone():
                flash('This username is already taken. Please choose another one.', 'error')
            else:
                pw_hash = generate_password_hash(password, method='pbkdf2:sha256')
                try:
                    db.execute(
                        'INSERT INTO users (username, password_hash, cash_balance) VALUES (?, ?, ?)',
                        (username, pw_hash, STARTING_CASH),
                    )
                    db.commit()
                except (sqlite3.IntegrityError, _PgUniqueViolation):
                    db.rollback()
                    flash('This username is already taken. Please choose another one.', 'error')
                else:
                    row = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
                    user = User(row['id'], row['username'], row['cash_balance'])
                    # Seed history with starting balance
                    record_portfolio_value(user.id, db)
                    login_user(user)
                    flash(f'Welcome! You have been credited ${STARTING_CASH:,.0f} in virtual funds.', 'success')
                    return redirect(url_for('dashboard'))

        elif action == 'login':
            row = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
            if row and check_password_hash(row['password_hash'], password):
                user = User(row['id'], row['username'], row['cash_balance'])
                login_user(user)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid username or password.', 'error')

    return render_template('login.html', starting_cash=STARTING_CASH)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    cash, stocks_value, total, pnl, pnl_pct, holdings_list = calc_portfolio(current_user.id, db)

    if SESSION_DEMO_MODE:
        history = session.get('demo_history', [])
        chart_labels = [_fmt_dt(h['created_at']) for h in history]
        chart_data = [round(h['total_value'], 2) for h in history]
    else:
        # Record snapshot at most once per hour
        last = db.execute(
            'SELECT created_at FROM portfolio_history WHERE user_id = ? ORDER BY id DESC LIMIT 1',
            (current_user.id,),
        ).fetchone()
        should_record = True
        if last:
            try:
                last_dt = _parse_dt(last['created_at'])
                if datetime.now() - last_dt < timedelta(hours=1):
                    should_record = False
            except Exception:
                pass
        if should_record:
            record_portfolio_value(current_user.id, db)

        # Chart data — last 30 snapshots
        history = db.execute(
            'SELECT total_value, created_at FROM portfolio_history '
            'WHERE user_id = ? ORDER BY id DESC LIMIT 30',
            (current_user.id,),
        ).fetchall()
        history = list(reversed(history))

        chart_labels = [_fmt_dt(h['created_at']) for h in history]
        chart_data = [round(h['total_value'], 2) for h in history]

    if not chart_data:
        chart_labels = ['Start']
        chart_data = [STARTING_CASH]

    top_holdings = sorted(holdings_list, key=lambda x: x['value'], reverse=True)[:5]
    ai_watch = ai_market_snapshot(db, holdings_list)

    return render_template(
        'dashboard.html',
        cash=cash,
        stocks_value=stocks_value,
        total=total,
        pnl=pnl,
        pnl_pct=pnl_pct,
        top_holdings=top_holdings,
        chart_labels=json.dumps(chart_labels),
        chart_data=json.dumps(chart_data),
        starting_cash=STARTING_CASH,
        ai_watch=ai_watch,
    )


@app.route('/market')
@login_required
def market():
    db = get_db()
    rows = db.execute('SELECT * FROM stock_prices ORDER BY ticker').fetchall()
    stocks = [dict(r) for r in rows]
    # Add any tickers missing from cache
    cached = {r['ticker'] for r in rows}
    for t in TICKERS:
        if t not in cached:
            stocks.append({
                'ticker': t, 'name': TICKER_NAMES.get(t, t),
                'price': None, 'change_pct': None,
            })
    for stock in stocks:
        stock['ai'] = build_ai_explanation(stock)
    stocks.sort(key=lambda x: x['ticker'])
    return render_template('market.html', stocks=stocks)


@app.route('/portfolio')
@login_required
def portfolio():
    db = get_db()
    cash, stocks_value, total, pnl, pnl_pct, holdings_list = calc_portfolio(current_user.id, db)
    holdings_list.sort(key=lambda x: x['value'], reverse=True)
    prices = {
        r['ticker']: dict(r)
        for r in db.execute('SELECT * FROM stock_prices').fetchall()
    }
    for holding in holdings_list:
        stock = prices.get(holding['ticker'], {'ticker': holding['ticker'], 'price': holding['current_price']})
        stock['name'] = holding['name']
        holding['ai'] = build_ai_explanation(stock, holding)
    return render_template(
        'portfolio.html',
        cash=cash,
        stocks_value=stocks_value,
        total=total,
        pnl=pnl,
        pnl_pct=pnl_pct,
        holdings=holdings_list,
        starting_cash=STARTING_CASH,
    )


@app.route('/ai-coach')
@login_required
def ai_coach():
    db = get_db()
    cash, stocks_value, total, pnl, pnl_pct, holdings_list = calc_portfolio(current_user.id, db)
    rows = [dict(r) for r in db.execute('SELECT * FROM stock_prices ORDER BY ticker').fetchall()]
    holdings_by_ticker = {h['ticker']: h for h in holdings_list}
    selected = (request.args.get('ticker') or '').upper().strip()
    if selected not in {r['ticker'] for r in rows}:
        selected = holdings_list[0]['ticker'] if holdings_list else (rows[0]['ticker'] if rows else 'AAPL')

    selected_stock = next((r for r in rows if r['ticker'] == selected), None)
    if selected_stock:
        selected_stock['name'] = selected_stock.get('name') or TICKER_NAMES.get(selected, selected)
        explanation = build_ai_explanation(selected_stock, holdings_by_ticker.get(selected))
    else:
        explanation = None

    watch = ai_market_snapshot(db, holdings_list)
    return render_template(
        'ai_coach.html',
        stocks=rows,
        selected=selected,
        explanation=explanation,
        watch=watch,
        cash=cash,
        stocks_value=stocks_value,
        total=total,
        pnl=pnl,
        pnl_pct=pnl_pct,
        ai_live=ai.ai_available(),
        ai_model=ai.model_name() if ai.ai_available() else None,
    )


@app.route('/api/ai/chat', methods=['POST'])
@login_required
def api_ai_chat():
    """Real Claude-backed coach chat, grounded in the user's live portfolio."""
    if _is_cross_origin():
        return jsonify({'error': 'Cross-origin request blocked.'}), 403
    if not ai.ai_available():
        return jsonify({
            'error': "The AI coach is not switched on yet. Ask your mentor to add the API key."
        }), 503

    data = request.get_json(silent=True) or {}
    question = (data.get('message') or '').strip()
    if not question:
        return jsonify({'error': 'Please type a question first.'}), 400

    db = get_db()
    allowed, remaining = ai_quota_check_and_increment(db, current_user.id)
    if not allowed:
        return jsonify({
            'error': f"You've reached today's limit of {AI_DAILY_LIMIT} AI questions. Try again tomorrow."
        }), 429

    cash, stocks_value, total, pnl, pnl_pct, holdings_list = calc_portfolio(current_user.id, db)
    movers = [
        {'ticker': r['ticker'], 'change_pct': r['change_pct']}
        for r in db.execute(
            'SELECT ticker, change_pct FROM stock_prices '
            'WHERE change_pct IS NOT NULL ORDER BY ABS(change_pct) DESC LIMIT 6'
        ).fetchall()
    ]
    portfolio = {'cash': cash, 'total': total, 'pnl': pnl, 'pnl_pct': pnl_pct}

    # Short conversation memory so follow-up questions ("why?") make sense.
    history = session.get('ai_history', [])
    answer, err = ai.coach_answer(question, portfolio, holdings_list, movers, history=history)
    if err:
        return jsonify({'error': err}), 502

    history = history + [
        {'role': 'user', 'content': question},
        {'role': 'assistant', 'content': answer},
    ]
    session['ai_history'] = history[-8:]  # keep the last 4 exchanges
    session.modified = True
    return jsonify({'answer': answer, 'remaining': remaining})


@app.route('/leaderboard')
@login_required
def leaderboard():
    db = get_db()
    if SESSION_DEMO_MODE:
        cash, stocks_value, total, pnl, pnl_pct, _ = calc_portfolio(current_user.id, db)
        return render_template(
            'leaderboard.html',
            board=[{
                'username': current_user.username,
                'total': total,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'is_me': True,
            }],
            starting_cash=STARTING_CASH,
        )

    users = db.execute('SELECT id, username, cash_balance FROM users').fetchall()
    prices = {
        r['ticker']: r['price']
        for r in db.execute('SELECT ticker, price FROM stock_prices').fetchall()
    }

    board = []
    for u in users:
        holdings = db.execute(
            'SELECT ticker, shares FROM holdings WHERE user_id = ? AND shares > 0',
            (u['id'],),
        ).fetchall()
        stocks_val = sum(h['shares'] * prices.get(h['ticker'], 0) for h in holdings)
        total = u['cash_balance'] + stocks_val
        pnl = total - STARTING_CASH
        pnl_pct = pnl / STARTING_CASH * 100
        board.append({
            'username': u['username'],
            'total': total,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'is_me': u['id'] == current_user.id,
        })

    board.sort(key=lambda x: x['pnl_pct'], reverse=True)
    return render_template('leaderboard.html', board=board, starting_cash=STARTING_CASH)


@app.route('/history')
@login_required
def history():
    """A student's own trade history — a core feature the app was missing."""
    db = get_db()
    if SESSION_DEMO_MODE:
        return render_template('history.html', rows=[], demo=True)

    rows = [dict(r) for r in db.execute(
        'SELECT ticker, shares, price, type, created_at FROM transactions '
        'WHERE user_id = ? ORDER BY id DESC LIMIT 200',
        (current_user.id,),
    ).fetchall()]
    for r in rows:
        r['name'] = TICKER_NAMES.get(r['ticker'], r['ticker'])
        r['total'] = (r['shares'] or 0) * (r['price'] or 0)
        r['when'] = _fmt_dt(r['created_at'])
    return render_template('history.html', rows=rows, demo=False)


@app.route('/healthz')
def healthz():
    """Unauthenticated liveness/readiness probe — also reports DB mode."""
    prices = None
    db_ok = True
    try:
        prices = get_db().execute(
            'SELECT COUNT(*) AS c FROM stock_prices'
        ).fetchone()['c']
    except Exception:
        db_ok = False
    return jsonify({
        'status': 'ok' if db_ok else 'degraded',
        'persistent_db': IS_POSTGRES,
        'demo_mode': SESSION_DEMO_MODE,
        'ai_coach': 'live' if ai.ai_available() else 'rule-based',
        'prices_cached': prices,
    }), (200 if db_ok else 503)


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    # Logout
    if request.args.get('logout'):
        session.pop('admin_ok', None)
        return redirect(url_for('admin'))

    # Login attempt
    error = None
    if request.method == 'POST':
        if request.form.get('password') in admin_passwords():
            session['admin_ok'] = True
        else:
            error = 'Incorrect password.'

    if not session.get('admin_ok'):
        return render_template('admin.html', authenticated=False, error=error)

    db = get_db()
    today     = date.today()
    week_ago  = today - timedelta(days=7)

    # User counts
    total_users = db.execute(
        'SELECT COUNT(*) AS cnt FROM users'
    ).fetchone()['cnt']

    new_today = db.execute(
        'SELECT COUNT(*) AS cnt FROM users WHERE created_at >= ?',
        (today.isoformat(),),
    ).fetchone()['cnt']

    new_week = db.execute(
        'SELECT COUNT(*) AS cnt FROM users WHERE created_at >= ?',
        (week_ago.isoformat(),),
    ).fetchone()['cnt']

    # Transaction counts by type
    buys = sells = 0
    for row in db.execute(
        'SELECT type, COUNT(*) AS cnt FROM transactions GROUP BY type'
    ).fetchall():
        if row['type'] == 'buy':
            buys = row['cnt']
        elif row['type'] == 'sell':
            sells = row['cnt']

    # Top 5 most active users
    top_users = [dict(r) for r in db.execute('''
        SELECT u.username, COUNT(t.id) AS cnt
        FROM transactions t
        JOIN users u ON t.user_id = u.id
        GROUP BY u.id, u.username
        ORDER BY cnt DESC
        LIMIT 5
    ''').fetchall()]

    # Top 5 most purchased stocks
    top_stocks = [dict(r) for r in db.execute('''
        SELECT ticker, COUNT(*) AS cnt
        FROM transactions
        WHERE type = 'buy'
        GROUP BY ticker
        ORDER BY cnt DESC
        LIMIT 5
    ''').fetchall()]

    # Total virtual money in circulation (cash + stock holdings)
    total_cash = db.execute(
        'SELECT COALESCE(SUM(cash_balance), 0) AS s FROM users'
    ).fetchone()['s'] or 0

    total_stocks_value = db.execute('''
        SELECT COALESCE(SUM(h.shares * sp.price), 0) AS s
        FROM holdings h
        JOIN stock_prices sp ON h.ticker = sp.ticker
        WHERE h.shares > 0
    ''').fetchone()['s'] or 0

    total_in_circulation = total_cash + total_stocks_value

    # All users with portfolio value
    prices = {
        r['ticker']: r['price']
        for r in db.execute('SELECT ticker, price FROM stock_prices').fetchall()
    }
    users_list = []
    for u in db.execute(
        'SELECT id, username, cash_balance, created_at FROM users ORDER BY created_at DESC'
    ).fetchall():
        holdings = db.execute(
            'SELECT ticker, shares FROM holdings WHERE user_id = ? AND shares > 0',
            (u['id'],),
        ).fetchall()
        stocks_val = sum(h['shares'] * (prices.get(h['ticker']) or 0) for h in holdings)
        total = u['cash_balance'] + stocks_val
        reg = u['created_at']
        users_list.append({
            'username':   u['username'],
            'registered': reg.strftime('%Y-%m-%d') if hasattr(reg, 'strftime') else str(reg)[:10],
            'cash':       u['cash_balance'],
            'total':      total,
            'pnl_pct':    (total - STARTING_CASH) / STARTING_CASH * 100,
        })

    return render_template('admin.html',
        authenticated       = True,
        total_users         = total_users,
        new_today           = new_today,
        new_week            = new_week,
        buys                = buys,
        sells               = sells,
        top_users           = top_users,
        top_stocks          = top_stocks,
        total_in_circulation= total_in_circulation,
        users_list          = users_list,
        starting_cash       = STARTING_CASH,
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route('/api/cash')
@login_required
def api_cash():
    if SESSION_DEMO_MODE:
        demo_user = session.get('demo_user') or {}
        return jsonify({'cash': demo_user.get('cash_balance', STARTING_CASH)})

    user = get_db().execute(
        'SELECT cash_balance FROM users WHERE id = ?', (current_user.id,)
    ).fetchone()
    return jsonify({'cash': user['cash_balance']})


@app.route('/api/prices')
@login_required
def api_prices():
    db = get_db()
    rows = db.execute('SELECT * FROM stock_prices').fetchall()
    return jsonify({r['ticker']: dict(r) for r in rows})


@app.route('/api/buy', methods=['POST'])
@login_required
def api_buy():
    if _is_cross_origin():
        return jsonify({'error': 'Cross-origin request blocked.'}), 403
    data = request.get_json(silent=True) or {}
    ticker = (data.get('ticker') or '').upper().strip()
    shares = _valid_shares(data.get('shares', 0))
    if shares is None:
        return jsonify({'error': 'Enter a valid number of shares.'}), 400
    if ticker not in TICKERS:
        return jsonify({'error': 'Ticker not found.'}), 400

    db = get_db()
    price_row = db.execute(
        'SELECT price FROM stock_prices WHERE ticker = ?', (ticker,)
    ).fetchone()
    if not price_row or not price_row['price']:
        return jsonify({'error': 'Price unavailable, please try again later.'}), 400

    price = price_row['price']
    total_cost = price * shares

    if SESSION_DEMO_MODE:
        demo_user = session.get('demo_user') or {
            'id': 'demo',
            'username': current_user.username,
            'cash_balance': STARTING_CASH,
        }
        cash_balance = float(demo_user.get('cash_balance', STARTING_CASH))
        if cash_balance < total_cost:
            return jsonify({'error': f'Insufficient funds. Need ${total_cost:,.2f}, available ${cash_balance:,.2f}.'}), 400

        demo_holdings = session.get('demo_holdings', {})
        existing = demo_holdings.get(ticker)
        if existing:
            old_shares = float(existing.get('shares', 0))
            old_avg = float(existing.get('avg_price', price))
            new_shares = old_shares + shares
            new_avg = (old_shares * old_avg + shares * price) / new_shares
        else:
            new_shares = shares
            new_avg = price

        demo_holdings[ticker] = {'shares': new_shares, 'avg_price': new_avg}
        demo_user['cash_balance'] = cash_balance - total_cost
        session['demo_user'] = demo_user
        session['demo_holdings'] = demo_holdings
        session.modified = True
        record_portfolio_value(current_user.id, db)
        return jsonify({
            'success': True,
            'message': f'Bought {shares:g} share(s) of {ticker} at ${price:,.2f}.',
            'new_cash': demo_user['cash_balance'],
        })

    # Atomic, race-safe deduction: only succeeds if the balance still covers
    # the cost. Guards against a double-submit spending the same cash twice.
    cur = db.execute(
        'UPDATE users SET cash_balance = cash_balance - ? '
        'WHERE id = ? AND cash_balance >= ?',
        (total_cost, current_user.id, total_cost),
    )
    if cur.rowcount == 0:
        db.rollback()
        avail = db.execute(
            'SELECT cash_balance FROM users WHERE id = ?', (current_user.id,)
        ).fetchone()['cash_balance']
        return jsonify({'error': f'Insufficient funds. Need ${total_cost:,.2f}, available ${avail:,.2f}.'}), 400

    # Update holdings (upsert with weighted avg price)
    existing = db.execute(
        'SELECT shares, avg_price FROM holdings WHERE user_id = ? AND ticker = ?',
        (current_user.id, ticker),
    ).fetchone()
    if existing:
        new_shares = existing['shares'] + shares
        new_avg = (existing['shares'] * existing['avg_price'] + shares * price) / new_shares
        db.execute(
            'UPDATE holdings SET shares = ?, avg_price = ? WHERE user_id = ? AND ticker = ?',
            (new_shares, new_avg, current_user.id, ticker),
        )
    else:
        db.execute(
            'INSERT INTO holdings (user_id, ticker, shares, avg_price) VALUES (?, ?, ?, ?)',
            (current_user.id, ticker, shares, price),
        )

    # Record transaction
    db.execute(
        'INSERT INTO transactions (user_id, ticker, shares, price, type) VALUES (?, ?, ?, ?, ?)',
        (current_user.id, ticker, shares, price, 'buy'),
    )
    db.commit()
    record_portfolio_value(current_user.id, db)

    return jsonify({
        'success': True,
        'message': f'Bought {shares:g} share(s) of {ticker} at ${price:,.2f}.',
        'new_cash': db.execute('SELECT cash_balance FROM users WHERE id = ?', (current_user.id,)).fetchone()['cash_balance'],
    })


@app.route('/api/sell', methods=['POST'])
@login_required
def api_sell():
    if _is_cross_origin():
        return jsonify({'error': 'Cross-origin request blocked.'}), 403
    data = request.get_json(silent=True) or {}
    ticker = (data.get('ticker') or '').upper().strip()
    shares = _valid_shares(data.get('shares', 0))
    if shares is None:
        return jsonify({'error': 'Enter a valid number of shares.'}), 400

    db = get_db()
    price_row = db.execute(
        'SELECT price FROM stock_prices WHERE ticker = ?', (ticker,)
    ).fetchone()
    if not price_row or not price_row['price']:
        return jsonify({'error': 'Price unavailable.'}), 400

    price = price_row['price']
    proceeds = price * shares

    if SESSION_DEMO_MODE:
        demo_holdings = session.get('demo_holdings', {})
        holding = demo_holdings.get(ticker)
        owned = float(holding.get('shares', 0)) if holding else 0
        if owned < shares:
            return jsonify({'error': f'Not enough shares. You own {owned:g}.'}), 400

        demo_user = session.get('demo_user') or {
            'id': 'demo',
            'username': current_user.username,
            'cash_balance': STARTING_CASH,
        }
        new_shares = owned - shares
        if new_shares < 1e-9:
            demo_holdings.pop(ticker, None)
        else:
            holding['shares'] = new_shares
            demo_holdings[ticker] = holding
        demo_user['cash_balance'] = float(demo_user.get('cash_balance', STARTING_CASH)) + proceeds
        session['demo_user'] = demo_user
        session['demo_holdings'] = demo_holdings
        session.modified = True
        record_portfolio_value(current_user.id, db)
        return jsonify({
            'success': True,
            'message': f'Sold {shares:g} share(s) of {ticker} at ${price:,.2f}.',
            'new_cash': demo_user['cash_balance'],
        })

    # Atomic, race-safe decrement: only succeeds if enough shares remain.
    cur = db.execute(
        'UPDATE holdings SET shares = shares - ? '
        'WHERE user_id = ? AND ticker = ? AND shares >= ?',
        (shares, current_user.id, ticker, shares),
    )
    if cur.rowcount == 0:
        owned_row = db.execute(
            'SELECT shares FROM holdings WHERE user_id = ? AND ticker = ?',
            (current_user.id, ticker),
        ).fetchone()
        owned = owned_row['shares'] if owned_row else 0
        db.rollback()
        return jsonify({'error': f'Not enough shares. You own {owned:g}.'}), 400

    # Drop a fully-sold position, add the proceeds.
    db.execute(
        'DELETE FROM holdings WHERE user_id = ? AND ticker = ? AND shares < ?',
        (current_user.id, ticker, 1e-9),
    )
    db.execute(
        'UPDATE users SET cash_balance = cash_balance + ? WHERE id = ?',
        (proceeds, current_user.id),
    )

    db.execute(
        'INSERT INTO transactions (user_id, ticker, shares, price, type) VALUES (?, ?, ?, ?, ?)',
        (current_user.id, ticker, shares, price, 'sell'),
    )
    db.commit()
    record_portfolio_value(current_user.id, db)

    return jsonify({
        'success': True,
        'message': f'Sold {shares:g} share(s) of {ticker} at ${price:,.2f}.',
        'new_cash': db.execute('SELECT cash_balance FROM users WHERE id = ?', (current_user.id,)).fetchone()['cash_balance'],
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    ensure_runtime_ready()
    from scheduler import start_scheduler
    start_scheduler(app)
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000)
