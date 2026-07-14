/* ============================================================
   StockSim — global JS
   ============================================================ */

// ── Mobile nav burger ──────────────────────────────────────
const burger = document.getElementById('navBurger');
const mobileMenu = document.getElementById('mobileMenu');
if (burger) {
  burger.addEventListener('click', function () {
    mobileMenu.classList.toggle('open');
  });
}

// ── Modal state ────────────────────────────────────────────
const overlay   = document.getElementById('modalOverlay');
const modalClose = document.getElementById('modalClose');
const modalTitle = document.getElementById('modalTitle');
const modalName  = document.getElementById('modalName');
const modalPriceEl = document.getElementById('modalPrice');
const modalAvailEl = document.getElementById('modalAvail');
const modalAvailLabel = document.getElementById('modalAvailLabel');
const modalSharesInput = document.getElementById('modalShares');
const modalTotalEl = document.getElementById('modalTotal');
const modalSubmit = document.getElementById('modalSubmit');
const modalMsg    = document.getElementById('modalMsg');

let _mode = 'buy';   // 'buy' | 'sell'
let _ticker = '';
let _price = 0;
let _maxShares = 0;

function openModal() {
  overlay.classList.add('open');
  modalSharesInput.value = '';
  modalTotalEl.textContent = '$0.00';
  setModalMsg('', '');
  modalSubmit.disabled = false;
  setTimeout(() => modalSharesInput.focus(), 80);
}

function closeModal() {
  overlay.classList.remove('open');
}

function setModalMsg(text, type) {
  modalMsg.textContent = text;
  modalMsg.className = 'modal-msg' + (type ? ' ' + type : '');
}

// Buy
function openBuyModal(btn) {
  _mode   = 'buy';
  _ticker = btn.dataset.ticker;
  _price  = parseFloat(btn.dataset.price);

  modalTitle.textContent = 'Buy Shares';
  modalSubmit.textContent = 'Buy';
  modalSubmit.style.background = 'var(--green)';

  modalName.textContent  = btn.dataset.name + ' (' + _ticker + ')';
  modalPriceEl.textContent = '$' + _price.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

  modalAvailLabel.textContent = 'Available cash';
  modalAvailEl.textContent = '...';

  // Fetch cash balance
  getCash().then(cash => {
    _maxShares = cash / _price;
    modalAvailEl.textContent = '$' + cash.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  });

  openModal();
}

// Sell
function openSellModal(btn) {
  _mode   = 'sell';
  _ticker = btn.dataset.ticker;
  _price  = parseFloat(btn.dataset.price);
  _maxShares = parseFloat(btn.dataset.shares);

  modalTitle.textContent = 'Sell Shares';
  modalSubmit.textContent = 'Sell';
  modalSubmit.style.background = 'var(--red)';

  modalName.textContent  = btn.dataset.name + ' (' + _ticker + ')';
  modalPriceEl.textContent = '$' + _price.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

  modalAvailLabel.textContent = 'Shares owned';
  modalAvailEl.textContent = _maxShares.toLocaleString('en-US', {maximumFractionDigits: 4}) + ' shares';

  openModal();
}

// ── Live total calculation ──────────────────────────────────
if (modalSharesInput) {
  modalSharesInput.addEventListener('input', function () {
    const qty = parseFloat(this.value) || 0;
    const total = qty * _price;
    modalTotalEl.textContent = '$' + total.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    setModalMsg('', '');
  });
}

// ── Close modal ────────────────────────────────────────────
if (modalClose) modalClose.addEventListener('click', closeModal);
if (overlay) {
  overlay.addEventListener('click', function (e) {
    if (e.target === overlay) closeModal();
  });
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeModal();
});

// ── Submit trade ────────────────────────────────────────────
if (modalSubmit) {
  modalSubmit.addEventListener('click', function () {
    const shares = parseFloat(modalSharesInput.value);
    if (!shares || shares <= 0) {
      setModalMsg('Please enter a valid number of shares.', 'error');
      return;
    }
    if (_mode === 'sell' && shares > _maxShares) {
      setModalMsg('Cannot sell more than you own (' + _maxShares.toLocaleString('en-US', {maximumFractionDigits: 4}) + ' shares).', 'error');
      return;
    }
    if (_mode === 'buy' && shares > _maxShares) {
      setModalMsg('Insufficient cash for that order.', 'error');
      return;
    }

    modalSubmit.disabled = true;
    setModalMsg('Processing...', '');

    const endpoint = _mode === 'buy' ? '/api/buy' : '/api/sell';

    fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: _ticker, shares: shares}),
    })
      .then(r => r.json())
      .then(data => {
        if (data.success) {
          setModalMsg(data.message, 'success');
          // Reload page after 1.2s to reflect new balances
          setTimeout(() => {
            closeModal();
            location.reload();
          }, 1200);
        } else {
          setModalMsg(data.error || 'An error occurred.', 'error');
          modalSubmit.disabled = false;
        }
      })
      .catch(() => {
        setModalMsg('Network error. Please try again.', 'error');
        modalSubmit.disabled = false;
      });
  });
}

// ── Helper: get current cash balance ───────────────────────
function getCash() {
  return fetch('/api/cash')
    .then(r => r.json())
    .then(d => d.cash || 0)
    .catch(() => 0);
}
