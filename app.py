#!/usr/bin/env python3
"""
Единая точка входа для деплоя на Railway (или любой другой 24/7-хостинг):
  1) веб-страница с калькулятором funding (эта же страница защищена Basic Auth);
  2) фоново — Telegram-бот (long polling, тот же код, что в bot_worker.py);
  3) фоново — почасовая проверка прогнозного funding по открытым позициям
     и алерты в Telegram при уходе в минус (funding_alerts.py);
  4) фоново, ОПЦИОНАЛЬНО (только если заданы GOOGLE_SHEET_ID и
     GOOGLE_SERVICE_ACCOUNT_JSON) — периодическая запись funding по
     открытым позициям в столбец P Google Sheet взамен ручного ввода
     (sheets_sync.py).

Раньше (см. bot_worker.py) сервис на Railway был "worker" — процесс без
входящего HTTP и без публичного адреса. Теперь нужен и публичный веб —
поэтому Procfile указывает на этот файл как на "web"-процесс, слушающий
$PORT. bot_worker.py и bot_poll.py при этом не удалены и по-прежнему
рабочие сами по себе — на случай, если веб-калькулятор понадобится
отключить, не трогая сам Telegram-бот.

Обязательные переменные окружения — как и раньше (ASTER_*, TELEGRAM_*,
опционально BYBIT_*/LIGHTER_*/MEXC_*/GATE_*, см. funding_report.load_secrets),
плюс новые для веб-калькулятора:
  WEB_APP_USERNAME              — логин для Basic Auth (по умолчанию "admin")
  WEB_APP_PASSWORD              — пароль для Basic Auth. Если не задан —
                                   страница калькулятора НЕ отдаётся вообще
                                   (500 с понятным текстом), чтобы случайно
                                   не выложить в интернет без защиты финансовые
                                   данные позиций.
  ALERT_CHECK_INTERVAL_MINUTES  — период проверки алертов, по умолчанию 60
  FUNDING_ALERT_THRESHOLD       — порог ставки (доля, не %) для алерта,
                                   по умолчанию 0.0 (алерт при любой ставке < 0)

Опционально — синхронизация с Google Sheet (см. sheets_sync.py, поток #4
выше). Если GOOGLE_SHEET_ID не задан, поток просто не запускается —
остальная часть приложения (веб-калькулятор, бот, алерты) работает как
обычно:
  GOOGLE_SHEET_ID               — id таблицы
  GOOGLE_SERVICE_ACCOUNT_JSON   — JSON-ключ сервисного аккаунта Google целиком
  GOOGLE_SHEET_TAB              — опционально, имя вкладки (по умолчанию —
                                   первая в файле)
  SHEET_SYNC_INTERVAL_MINUTES   — период синхронизации, по умолчанию 60

Запуск локально для проверки: PORT=8080 python app.py
"""

import base64
import os
import threading

from flask import Flask, Response, jsonify, request

import balances
import calculator
import entry_price
from bot_worker import poll_forever
from funding_alerts import alert_loop
from funding_report import load_secrets
from sheets_sync import sheet_sync_loop
from sltp_alerts import sltp_alert_loop

app = Flask(__name__)

WEB_APP_USERNAME = os.environ.get("WEB_APP_USERNAME", "admin")
WEB_APP_PASSWORD = os.environ.get("WEB_APP_PASSWORD")

_secrets_cache: dict | None = None


def get_secrets() -> dict:
    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = load_secrets()
    return _secrets_cache


# ── Basic Auth ────────────────────────────────────────────────────────────────

def _unauthorized() -> Response:
    return Response(
        "Требуется авторизация", 401,
        {"WWW-Authenticate": 'Basic realm="Funding Calculator"'},
    )


@app.before_request
def require_auth():
    if request.path == "/healthz":
        return None

    if not WEB_APP_PASSWORD:
        return Response(
            "WEB_APP_PASSWORD не задан в переменных окружения — веб-калькулятор "
            "отключён, чтобы не оказаться доступным без пароля. Задайте "
            "WEB_APP_USERNAME/WEB_APP_PASSWORD в настройках Railway и передеплойте.",
            500,
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return _unauthorized()
    try:
        decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return _unauthorized()
    if username != WEB_APP_USERNAME or password != WEB_APP_PASSWORD:
        return _unauthorized()
    return None


# ── Страница ─────────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/")
def index():
    return Response(CALCULATOR_HTML, mimetype="text/html")


@app.route("/balances")
def balances_page():
    return Response(BALANCES_HTML, mimetype="text/html")


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/exchanges")
def api_exchanges():
    secrets = get_secrets()
    connected = calculator.list_connected_exchanges(secrets)
    return jsonify([
        {
            "key": key,
            "label": calculator.EXCHANGE_LABELS.get(key, key),
            "default_futures_fee_pct": calculator.DEFAULT_FUTURES_TAKER_FEE_PCT.get(key, 0.0),
        }
        for key in connected
    ])


@app.route("/api/symbols")
def api_symbols():
    exchange = request.args.get("exchange", "")
    secrets = get_secrets()
    try:
        all_open = calculator.list_open_symbols(secrets)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"symbols": all_open.get(exchange, [])})


@app.route("/api/entry-price")
def api_entry_price():
    """
    Авто-подбор цен открытия для конкретной ОТКРЫТОЙ СЕЙЧАС позиции:
    цена фьючерса — из данных позиции на бирже, цена спота — из истории
    сделок на подключённых спот-биржах и (если настроен UNISWAP_WALLET_ADDRESS)
    из истории переводов кошелька на всех EVM-сетях вокруг момента открытия.
    Может занять до ~30 секунд — Uniswap-поиск опрашивает десятки сетей.
    """
    exchange = request.args.get("exchange", "")
    symbol = request.args.get("symbol", "")
    if not exchange or not symbol:
        return jsonify({"error": "Не указаны exchange/symbol"}), 400

    secrets = get_secrets()
    try:
        result = entry_price.get_entry_price_suggestion(secrets, exchange, symbol)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Ошибка при обращении к бирже: {e}"}), 502

    return jsonify(result)


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    body = request.get_json(force=True, silent=True) or {}
    secrets = get_secrets()

    try:
        exchange = str(body["exchange"])
        symbol = body.get("symbol") or None
        start_ms = int(body["start_ms"])
        end_ms = int(body["end_ms"])
        qty = float(body["qty"])
        spot_entry_price = float(body["spot_entry_price"])
        futures_entry_price = float(body["futures_entry_price"])
        spot_fee_pct = float(body.get("spot_fee_pct", calculator.DEFAULT_SPOT_TAKER_FEE_PCT))
        futures_fee_pct = float(
            body.get("futures_fee_pct", calculator.DEFAULT_FUTURES_TAKER_FEE_PCT.get(exchange, 0.05))
        )
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"Некорректные параметры запроса: {e}"}), 400

    try:
        result = calculator.calculate(
            secrets, exchange, symbol, start_ms, end_ms,
            qty, spot_entry_price, futures_entry_price,
            spot_fee_pct, futures_fee_pct,
        )
    except calculator.CalculatorError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Ошибка при обращении к бирже: {e}"}), 502

    return jsonify(result)


@app.route("/api/balances")
def api_balances():
    """
    Сводный баланс по всем источникам (см. balances.fetch_all_balances).
    Страница /balances показывает состояние загрузки, пока запрос летит, а
    не блокирует интерфейс молча.
    """
    secrets = get_secrets()
    try:
        result = balances.fetch_all_balances(secrets)
    except Exception as e:
        return jsonify({"error": f"Ошибка при сборе балансов: {e}"}), 502
    return jsonify(result)


# ── HTML (без внешних зависимостей — CSS/JS инлайн) ───────────────────────────

CALCULATOR_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Калькулятор funding</title>
<!-- Фавикон — инлайн SVG data-URI прямо в HTML, без отдельного файла и без
     настройки Flask static-папки (страница и так отдаётся одной строкой). -->
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>💰</text></svg>">
<style>
  :root {
    --bg: #0f1420; --panel: #161d2e; --border: #2a3348; --text: #e6ebf5;
    --muted: #8b96ab; --accent: #4f8cff; --pos: #3ecf8e; --neg: #ff6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px; line-height: 1.5;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
  .layout { display: grid; grid-template-columns: minmax(280px, 360px) 1fr; gap: 20px; align-items: start; }
  @media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 18px; }
  .panel h2 { font-size: 14px; margin: 0 0 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 12px 0 4px; }
  label:first-child { margin-top: 0; }
  input, select {
    width: 100%; padding: 8px 10px; background: #0d1220; border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 13px;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  button {
    margin-top: 16px; width: 100%; padding: 10px; background: var(--accent); border: none;
    border-radius: 6px; color: white; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  button:hover { opacity: .9; }
  button:disabled { opacity: .5; cursor: default; }
  .error { color: var(--neg); font-size: 13px; margin-top: 10px; }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .stat .label { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .stat .value { font-size: 20px; font-weight: 700; }
  .pos { color: var(--pos); } .neg { color: var(--neg); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .empty { color: var(--muted); font-size: 13px; padding: 12px 0; }
  .hint { color: var(--muted); font-size: 12px; margin-top: 10px; }
</style>
</head>
<body>

<h1>Калькулятор комиссий за funding</h1>
<div class="sub">Шорт на фьючерсах + лонг на споте — доход считается только с фьючерсной ноги. Период — по UTC.
  &nbsp;·&nbsp;<a href="/balances" style="color:var(--accent);">Сводный баланс →</a>
</div>

<div class="layout">
  <div class="panel">
    <h2>Параметры</h2>

    <label for="exchange">Биржа (фьючерс)</label>
    <select id="exchange"></select>

    <label for="symbol">Монета / контракт</label>
    <select id="symbol"><option value="">Все открытые позиции биржи</option></select>

    <div class="row2">
      <div>
        <label for="start">Начало периода</label>
        <input type="date" id="start">
      </div>
      <div>
        <label for="end">Конец периода</label>
        <input type="date" id="end">
      </div>
    </div>

    <label for="qty">Объём позиции (в монете)</label>
    <input type="number" id="qty" step="any" min="0" placeholder="напр. 0.5">

    <div class="row2">
      <div>
        <label for="spotPrice">Цена входа на споте</label>
        <input type="number" id="spotPrice" step="any" min="0" placeholder="покупка">
      </div>
      <div>
        <label for="futPrice">Цена входа на фьючерсе</label>
        <input type="number" id="futPrice" step="any" min="0" placeholder="шорт">
      </div>
    </div>
    <div class="hint" id="entryPriceStatus"></div>

    <div class="row2">
      <div>
        <label for="spotFee">Комиссия спота, % (taker)</label>
        <input type="number" id="spotFee" step="any" min="0">
      </div>
      <div>
        <label for="futFee">Комиссия фьючерса, % (taker)</label>
        <input type="number" id="futFee" step="any" min="0">
      </div>
    </div>
    <div class="hint">Для выбранной конкретной монеты (не «все позиции») цены входа подбираются автоматически из истории — можно поправить вручную перед расчётом.</div>

    <button id="calcBtn">Рассчитать</button>
    <div class="error" id="errorBox" style="display:none;"></div>
  </div>

  <div>
    <div class="stat-grid" id="statGrid" style="display:none;"></div>
    <div class="panel" id="resultsPanel" style="display:none;">
      <h2>Funding по дням</h2>
      <div id="byDayTable"></div>
      <div id="bySymbolWrap" style="margin-top:20px;">
        <h2>Funding по символам</h2>
        <div id="bySymbolTable"></div>
      </div>
    </div>
  </div>
</div>

<script>
const exchangeSelect = document.getElementById('exchange');
const symbolSelect = document.getElementById('symbol');
const spotFeeInput = document.getElementById('spotFee');
const futFeeInput = document.getElementById('futFee');
const errorBox = document.getElementById('errorBox');
const statGrid = document.getElementById('statGrid');
const resultsPanel = document.getElementById('resultsPanel');

let exchangesData = [];

function fmt(n, digits) {
  digits = digits === undefined ? 4 : digits;
  return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function setDefaultDates() {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - 30);
  document.getElementById('end').value = end.toISOString().slice(0, 10);
  document.getElementById('start').value = start.toISOString().slice(0, 10);
}

async function loadExchanges() {
  const resp = await fetch('/api/exchanges');
  exchangesData = await resp.json();
  exchangeSelect.innerHTML = exchangesData.map(e => `<option value="${e.key}">${e.label}</option>`).join('');
  if (exchangesData.length === 0) {
    exchangeSelect.innerHTML = '<option value="">Нет подключённых бирж</option>';
    document.getElementById('calcBtn').disabled = true;
    return;
  }
  onExchangeChange();
}

async function loadSymbols(exchange) {
  symbolSelect.innerHTML = '<option value="">Все открытые позиции биржи</option>';
  if (!exchange) return;
  try {
    const resp = await fetch('/api/symbols?exchange=' + encodeURIComponent(exchange));
    const data = await resp.json();
    (data.symbols || []).forEach(s => {
      const opt = document.createElement('option');
      opt.value = s; opt.textContent = s;
      symbolSelect.appendChild(opt);
    });
  } catch (e) { /* не критично — можно оставить пустой список */ }
}

function onExchangeChange() {
  const key = exchangeSelect.value;
  const meta = exchangesData.find(e => e.key === key);
  futFeeInput.value = meta ? meta.default_futures_fee_pct : '';
  spotFeeInput.value = spotFeeInput.value || 0.10;
  loadSymbols(key);
  clearEntryPriceStatus();
}

exchangeSelect.addEventListener('change', onExchangeChange);

const entryPriceStatus = document.getElementById('entryPriceStatus');
const qtyInput = document.getElementById('qty');
const spotPriceInput = document.getElementById('spotPrice');
const futPriceInput = document.getElementById('futPrice');

function clearEntryPriceStatus() {
  entryPriceStatus.textContent = '';
}

async function onSymbolChange() {
  const exchange = exchangeSelect.value;
  const symbol = symbolSelect.value;
  if (!exchange || !symbol) { clearEntryPriceStatus(); return; }

  entryPriceStatus.textContent = 'Ищу цены открытия по истории (может занять до ~30 сек)…';
  try {
    const resp = await fetch(`/api/entry-price?exchange=${encodeURIComponent(exchange)}&symbol=${encodeURIComponent(symbol)}`);
    const data = await resp.json();
    if (!resp.ok) {
      entryPriceStatus.textContent = data.error || 'Не удалось подобрать цены автоматически — введите вручную.';
      return;
    }

    if (data.qty != null) qtyInput.value = data.qty;
    if (data.futures_entry_price != null) futPriceInput.value = data.futures_entry_price;
    if (data.spot_entry_price != null) spotPriceInput.value = data.spot_entry_price;

    const parts = [];
    if (data.futures_entry_price != null) {
      parts.push(`Цена фьючерса — из открытой позиции на бирже${data.entry_time_is_exact ? '' : ' (время открытия оценено приблизительно)'}.`);
    }
    if (data.spot_entry_price != null) {
      parts.push(`Цена спота — из истории сделок на ${data.spot_price_exchanges.join(', ')} (${data.spot_price_trade_count} сделок в окне).`);
    }
    if (data.notes && data.notes.length) parts.push(...data.notes);
    entryPriceStatus.textContent = parts.join(' ');
  } catch (e) {
    entryPriceStatus.textContent = 'Не удалось связаться с сервером для подбора цен: ' + e;
  }
}

symbolSelect.addEventListener('change', onSymbolChange);

function renderTable(container, rows, labelHeader) {
  if (!rows || Object.keys(rows).length === 0) {
    container.innerHTML = '<div class="empty">Нет данных за период</div>';
    return;
  }
  let html = `<table><thead><tr><th>${labelHeader}</th><th>Funding</th></tr></thead><tbody>`;
  for (const [k, v] of Object.entries(rows)) {
    const cls = v >= 0 ? 'pos' : 'neg';
    html += `<tr><td>${k}</td><td class="${cls}">${v >= 0 ? '+' : ''}${fmt(v)}</td></tr>`;
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

async function calculate() {
  errorBox.style.display = 'none';
  const exchange = exchangeSelect.value;
  if (!exchange) return;

  const startDate = document.getElementById('start').value;
  const endDate = document.getElementById('end').value;
  const qty = parseFloat(qtyInput.value);
  const spotPrice = parseFloat(spotPriceInput.value);
  const futPrice = parseFloat(futPriceInput.value);
  const spotFee = parseFloat(spotFeeInput.value);
  const futFee = parseFloat(futFeeInput.value);

  if (!startDate || !endDate || !qty || !spotPrice || !futPrice) {
    errorBox.textContent = 'Заполните период, объём и обе цены входа.';
    errorBox.style.display = 'block';
    return;
  }

  const body = {
    exchange, symbol: symbolSelect.value || null,
    start_ms: new Date(startDate + 'T00:00:00Z').getTime(),
    end_ms: new Date(endDate + 'T23:59:59Z').getTime(),
    qty, spot_entry_price: spotPrice, futures_entry_price: futPrice,
    spot_fee_pct: spotFee, futures_fee_pct: futFee,
  };

  document.getElementById('calcBtn').disabled = true;
  try {
    const resp = await fetch('/api/calculate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) {
      errorBox.textContent = data.error || 'Ошибка расчёта';
      errorBox.style.display = 'block';
      statGrid.style.display = 'none';
      resultsPanel.style.display = 'none';
      return;
    }
    renderResults(data);
  } catch (e) {
    errorBox.textContent = 'Не удалось связаться с сервером: ' + e;
    errorBox.style.display = 'block';
  } finally {
    document.getElementById('calcBtn').disabled = false;
  }
}

function renderResults(data) {
  const netCls = data.net_total >= 0 ? 'pos' : 'neg';
  const spreadCls = data.spread_effect >= 0 ? 'pos' : 'neg';
  statGrid.innerHTML = `
    <div class="stat"><div class="label">Funding доход (${data.records_count} начислений)</div><div class="value pos">+${fmt(data.funding_income_total)}</div></div>
    <div class="stat"><div class="label">Комиссии открытия</div><div class="value neg">-${fmt(data.opening_fees)}</div></div>
    <div class="stat"><div class="label">Спред спот/фьючерс на входе</div><div class="value ${spreadCls}">${data.spread_effect >= 0 ? '+' : ''}${fmt(data.spread_effect)}</div></div>
    <div class="stat"><div class="label">Итого (net)</div><div class="value ${netCls}">${data.net_total >= 0 ? '+' : ''}${fmt(data.net_total)}</div></div>
  `;
  statGrid.style.display = 'grid';

  renderTable(document.getElementById('byDayTable'), data.by_day, 'Дата (UTC)');
  const bySymbolWrap = document.getElementById('bySymbolWrap');
  if (data.by_symbol) {
    bySymbolWrap.style.display = 'block';
    renderTable(document.getElementById('bySymbolTable'), data.by_symbol, 'Символ');
  } else {
    bySymbolWrap.style.display = 'none';
  }
  resultsPanel.style.display = 'block';
}

document.getElementById('calcBtn').addEventListener('click', calculate);

setDefaultDates();
loadExchanges();
</script>
</body>
</html>
"""


BALANCES_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сводный баланс</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>💰</text></svg>">
<style>
  :root {
    --bg: #0f1420; --panel: #161d2e; --border: #2a3348; --text: #e6ebf5;
    --muted: #8b96ab; --accent: #4f8cff; --pos: #3ecf8e; --neg: #ff6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px; line-height: 1.5;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 14px; margin: 0 0 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
  .sub a { color: var(--accent); }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .stat .label { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .stat .value { font-size: 20px; font-weight: 700; }
  .pos { color: var(--pos); } .neg { color: var(--neg); } .muted { color: var(--muted); }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .empty { color: var(--muted); font-size: 13px; padding: 12px 0; }
  .err { color: var(--neg); }
  .loading { color: var(--muted); font-size: 13px; }
</style>
</head>
<body>

<h1>Сводный баланс</h1>
<div class="sub">Биржи + Rabby wallet + DeFi-протоколы (DeBank), суммарно в USD. &nbsp;·&nbsp; <a href="/">← Калькулятор funding</a></div>

<div id="loading" class="loading">⏳ Собираю данные…</div>
<div id="errorBox" class="err" style="display:none;"></div>
<div id="content" style="display:none;">
  <div class="stat-grid" id="statGrid"></div>

  <div class="panel">
    <h2>Биржи</h2>
    <div id="exchangesTable"></div>
  </div>

  <div class="panel">
    <h2>Rabby wallet (DeBank)</h2>
    <div id="walletTable"></div>
  </div>

  <div class="panel">
    <h2>DeFi-протоколы (DeBank)</h2>
    <div id="protocolsTable"></div>
  </div>
</div>

<script>
function fmt(n, digits) {
  digits = digits === undefined ? 2 : digits;
  return Number(n).toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

const EXCHANGE_LABELS = { aster: 'Aster', bybit: 'Bybit', lighter: 'Lighter', mexc: 'MEXC', gate: 'Gate' };

function renderExchanges(exchanges) {
  const rows = Object.entries(exchanges);
  if (rows.length === 0) { return '<div class="empty">Нет подключённых бирж</div>'; }
  let html = '<table><thead><tr><th>Биржа</th><th>Баланс, $</th></tr></thead><tbody>';
  for (const [key, v] of rows) {
    const label = EXCHANGE_LABELS[key] || key;
    if (v.error) {
      html += `<tr><td>${label}</td><td class="err">Ошибка: ${v.error}</td></tr>`;
    } else {
      html += `<tr><td>${label}</td><td>${fmt(v.value)}</td></tr>`;
    }
  }
  html += '</tbody></table>';
  return html;
}

function renderWallet(wallet) {
  if (!wallet) return '<div class="empty">Не настроено — задайте RABBY_WALLET_ADDRESS/DEBANK_ACCESS_KEY.</div>';
  if (wallet.error) return `<div class="err">Ошибка: ${wallet.error}</div>`;
  const chainKeys = Object.keys(wallet.chains || {});
  if (chainKeys.length === 0) return '<div class="empty">Ненулевых балансов не найдено ни на одной сети.</div>';

  let html = '<table><thead><tr><th>Сеть</th><th>Актив</th><th>Кол-во</th><th>Оценка, $</th></tr></thead><tbody>';
  for (const chain of chainKeys) {
    wallet.chains[chain].forEach((r, i) => {
      const chainCell = i === 0 ? chain : '';
      const usd = r.price_usd == null ? '<span class="muted">нет цены</span>' : fmt(r.amount * r.price_usd);
      html += `<tr><td>${chainCell}</td><td>${r.symbol}</td><td>${fmt(r.amount, 6)}</td><td>${usd}</td></tr>`;
    });
  }
  html += '</tbody></table>';
  return html;
}

function renderProtocols(protocols) {
  if (!protocols) return '<div class="empty">Не настроено — нужны те же RABBY_WALLET_ADDRESS/DEBANK_ACCESS_KEY.</div>';
  if (protocols.error) return `<div class="err">Ошибка: ${protocols.error}</div>`;
  const entries = Object.values(protocols);
  if (entries.length === 0) return '<div class="empty">Открытых DeFi-позиций не найдено.</div>';

  let html = '<table><thead><tr><th>Протокол</th><th>Сеть</th><th>Чистая позиция, $</th></tr></thead><tbody>';
  for (const p of entries) {
    const cls = p.net_usd >= 0 ? 'pos' : 'neg';
    html += `<tr><td>${p.name}</td><td>${p.chain}</td><td class="${cls}">${fmt(p.net_usd)}</td></tr>`;
  }
  html += '</tbody></table>';
  return html;
}

async function load() {
  try {
    const resp = await fetch('/api/balances');
    const data = await resp.json();
    document.getElementById('loading').style.display = 'none';
    if (!resp.ok) {
      const errorBox = document.getElementById('errorBox');
      errorBox.textContent = data.error || 'Ошибка загрузки';
      errorBox.style.display = 'block';
      return;
    }

    let protocolsNet = 0;
    if (data.protocols && !data.protocols.error) {
      protocolsNet = Object.values(data.protocols).reduce((s, p) => s + p.net_usd, 0);
    }
    document.getElementById('statGrid').innerHTML = `
      <div class="stat"><div class="label">Итого по всем источникам</div><div class="value pos">$${fmt(data.total_usd)}</div></div>
      <div class="stat"><div class="label">Из них DeFi-протоколы (net)</div><div class="value">$${fmt(protocolsNet)}</div></div>
      <div class="stat"><div class="label">Из них Rabby wallet</div><div class="value">$${fmt(data.wallet && !data.wallet.error ? data.wallet.total_usd : 0)}</div></div>
    `;

    document.getElementById('exchangesTable').innerHTML = renderExchanges(data.exchanges || {});
    document.getElementById('walletTable').innerHTML = renderWallet(data.wallet);
    document.getElementById('protocolsTable').innerHTML = renderProtocols(data.protocols);
    document.getElementById('content').style.display = 'block';
  } catch (e) {
    document.getElementById('loading').style.display = 'none';
    const errorBox = document.getElementById('errorBox');
    errorBox.textContent = 'Не удалось связаться с сервером: ' + e;
    errorBox.style.display = 'block';
  }
}

load();
</script>
</body>
</html>
"""


# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    secrets = get_secrets()

    threading.Thread(target=poll_forever, daemon=True, name="telegram-poll").start()
    threading.Thread(target=alert_loop, args=(secrets,), daemon=True, name="funding-alerts").start()
    threading.Thread(target=sltp_alert_loop, args=(secrets,), daemon=True, name="sltp-alerts").start()

    # Опционально: пользователь может не подключать Google Sheet вовсе —
    # тогда просто не запускаем поток, а не падаем и не блокируем всё
    # остальное (веб-калькулятор/бот/алерты по-прежнему работают).
    if os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        threading.Thread(target=sheet_sync_loop, args=(secrets,), daemon=True, name="sheet-sync").start()
    else:
        print("[app] GOOGLE_SHEET_ID/GOOGLE_SERVICE_ACCOUNT_JSON не заданы — "
              "синхронизация с Google Sheet отключена.", flush=True)

    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
