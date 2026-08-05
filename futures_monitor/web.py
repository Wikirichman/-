from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import json
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from urllib.parse import parse_qs, urlparse

from .config import load_config
from .options import analyze_options_for_contracts
from .providers import fetch_universe, provider_from_name, provider_source_manifest
from .scoring import selections_to_frame, select_leaders


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "universe.json"
OUTPUT_DIR = ROOT / "outputs"
ASSET_DIR = ROOT / "assets"
COOKIE_NAME = "futures_monitor_session"
SESSION_SECONDS = int(os.getenv("FUTURES_MONITOR_SESSION_SECONDS", "43200"))


def run_selection(provider_name: str, config_path: Path = DEFAULT_CONFIG) -> dict[str, object]:
    config = load_config(config_path)
    provider = provider_from_name(provider_name)
    source_manifest = provider_source_manifest(provider_name, config.contracts)
    bars, inventories, hourly, external_inventories, external_bars = fetch_universe(
        provider,
        config.contracts,
        config.lookback_days,
        int(config.intraday.get("lookback_bars", 80)),
    )
    inventory_ready = {
        symbol: (not frame.empty and len(frame) >= 31)
        for symbol, frame in inventories.items()
    }
    external_inventory_ready = {
        symbol: (not frame.empty and len(frame) >= 31)
        for symbol, frame in external_inventories.items()
    }
    external_price_ready = {
        symbol: (not frame.empty and len(frame) >= 21)
        for symbol, frame in external_bars.items()
    }
    hourly_ready = {symbol: (not frame.empty) for symbol, frame in hourly.items()}
    selections = select_leaders(
        config, bars, inventories, hourly, external_inventories, external_bars
    )
    selected_contracts = [
        next(contract for contract in config.contracts if contract.symbol == selection.symbol)
        for selection in selections
    ]
    option_structures = analyze_options_for_contracts(provider_name, selected_contracts)
    frame = selections_to_frame(selections)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"{provider_name}_selection.csv"
    if not frame.empty:
        frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    else:
        csv_path.write_text("", encoding="utf-8-sig")
    return {
        "provider": provider_name,
        "csv": str(csv_path),
        "count": len(selections),
        "explanation": _explanation(config),
        "sources": source_manifest,
        "coverage": {
            "contracts": len(config.contracts),
            "inventory_ready": sum(1 for ready in inventory_ready.values() if ready),
            "hourly_ready": sum(1 for ready in hourly_ready.values() if ready),
            "external_inventory_ready": sum(1 for ready in external_inventory_ready.values() if ready),
            "external_price_ready": sum(1 for ready in external_price_ready.values() if ready),
            "option_ready": sum(1 for item in option_structures.values() if item.get("available")),
            "option_checked": len(option_structures),
            "inventory_mode": "inventory_30d",
            "positioning_threshold": float(config.positioning.get("min_oi_change_20d", 0.05)),
            "intraday_required": bool(config.intraday.get("required_for_selection", False)),
            "supply_required": bool(config.supply_demand.get("required_for_selection", True)),
        },
        "rows": [
            selection.__dict__ | {"option_structure": option_structures.get(selection.symbol)}
            for selection in selections
        ],
    }


def _explanation(config) -> dict[str, object]:
    return {
        "supply_demand_title": "供需矛盾怎么判断",
        "supply_demand_body": (
            "当前版本优先看国内库存30日变化，多头要求库存下降，空头要求库存上升。"
            "对沪铜、沪铝、沪锌、沪金、沪银这类有外盘映射的品种，再叠加 LME 或 COMEX 库存与外盘价格做对比。"
            "库存接口抓不到时会明确留空，不把缺失数据硬解释成供需结论。"
        ),
        "positioning_title": "技术面怎么确认",
        "positioning_body": (
            f"持仓20日变化必须明显增加，当前默认门槛是 {float(config.positioning.get('min_oi_change_20d', 0.05)):.0%}。"
            "多头重点看上涨增仓，空头重点看下跌增仓。"
        ),
        "intraday_title": "小时级别怎么触发",
        "intraday_body": (
            "小时线只认两类结构：N字反转破摆动高低点，或突破震荡中枢。"
            "并且必须和日线方向一致，才计入最终确认。"
        ),
        "option_title": "期权结构怎么判断",
        "option_body": (
            "第一版只接入国内交易所公开商品期权日频数据。"
            "能拿到真实期权链时，计算 Call Wall、Put Wall、Call/Put 净增仓、ATM IV、25D 与 10D Skew；"
            "缺少隐波或 Delta 时，对应指标留空，不做推断。"
        ),
    }


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json({"ok": True})
            return
        if parsed.path == "/login":
            self._send_login()
            return
        if parsed.path == "/logout":
            self._clear_session("/login")
            return
        if not self._is_authenticated():
            if parsed.path == "/api/run":
                self._send_json({"error": "Authentication required"}, HTTPStatus.UNAUTHORIZED)
                return
            if parsed.path.startswith("/download/"):
                self._redirect("/login")
                return
            if parsed.path != "/" and not parsed.path.startswith("/assets/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/":
                self._redirect("/login")
                return
        if parsed.path == "/":
            self._send_html(_html())
            return
        if parsed.path == "/api/run":
            provider = parse_qs(parsed.query).get("provider", ["sample"])[0]
            if provider not in {"sample", "akshare"}:
                self._send_json({"error": f"Unsupported provider: {provider}"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                self._send_json(run_selection(provider))
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path.startswith("/download/"):
            name = parsed.path.removeprefix("/download/")
            if name not in {"sample_selection.csv", "akshare_selection.csv"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_file(OUTPUT_DIR / name, "text/csv; charset=utf-8")
            return
        if parsed.path.startswith("/assets/"):
            name = parsed.path.removeprefix("/assets/")
            if name != "zemira-capital-logo.jpg":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_file(ASSET_DIR / name, "image/jpeg")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/login":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = parse_qs(body)
        username = fields.get("username", [""])[0].strip()
        password = fields.get("password", [""])[0]
        if self._valid_credentials(username, password):
            self._set_session(username)
            return
        self._send_login("账号或密码不正确。")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_login(self, error: str = "") -> None:
        self._send_html(_login_html(error))

    def _send_json(self, data: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _is_authenticated(self) -> bool:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return False
        cookies = SimpleCookie(cookie_header)
        morsel = cookies.get(COOKIE_NAME)
        if morsel is None:
            return False
        parts = morsel.value.split(".")
        if len(parts) != 3:
            return False
        username, expires_text, signature = parts
        if not expires_text.isdigit() or int(expires_text) < int(time.time()):
            return False
        expected = _sign_session(username, expires_text)
        if not expected:
            return False
        return hmac.compare_digest(signature, expected)

    def _valid_credentials(self, username: str, password: str) -> bool:
        expected_user = os.getenv("FUTURES_MONITOR_USERNAME", "admin")
        expected_password = os.getenv("FUTURES_MONITOR_PASSWORD", "")
        if not expected_password and not _is_deployed():
            expected_password = "local-dev"
        if not expected_password:
            return False
        return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)

    def _set_session(self, username: str) -> None:
        expires = str(int(time.time()) + SESSION_SECONDS)
        value = f"{username}.{expires}.{_sign_session(username, expires)}"
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", _cookie_header(value))
        self.end_headers()

    def _clear_session(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", _cookie_header("", max_age=0))
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the futures monitor web app.")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8787")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Futures monitor web app: http://{args.host}:{args.port}")
    server.serve_forever()


def _secret_key() -> str:
    configured = os.getenv("FUTURES_MONITOR_SECRET_KEY", "")
    if configured:
        return configured
    if _is_deployed():
        return ""
    return "local-development-secret"


def _is_deployed() -> bool:
    return os.getenv("FUTURES_MONITOR_DEPLOYED", "").lower() in {"1", "true", "yes"}


def _sign_session(username: str, expires: str) -> str:
    secret = _secret_key()
    if not secret:
        return ""
    payload = f"{username}.{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _cookie_header(value: str, max_age: int | None = None) -> str:
    cookie = SimpleCookie()
    cookie[COOKIE_NAME] = value
    cookie[COOKIE_NAME]["path"] = "/"
    cookie[COOKIE_NAME]["httponly"] = True
    cookie[COOKIE_NAME]["samesite"] = "Lax"
    if os.getenv("FUTURES_MONITOR_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}:
        cookie[COOKIE_NAME]["secure"] = True
    if max_age is not None:
        cookie[COOKIE_NAME]["max-age"] = str(max_age)
    return cookie.output(header="").strip()


def _login_html(error: str = "") -> str:
    error_markup = f'<p class="error">{error}</p>' if error else ""
    dev_hint = ""
    if not os.getenv("FUTURES_MONITOR_PASSWORD", "") and not _is_deployed():
        dev_hint = '<p class="hint">本地开发默认账号 admin，密码 local-dev。公网部署请设置环境变量。</p>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>登录 - 商品期货全品种监控</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f1f1f1;
      color: #111111;
      font-family: "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
    }}
    main {{
      width: min(420px, calc(100vw - 32px));
      border: 1px solid rgba(17,17,17,.1);
      border-radius: 10px;
      background: rgba(255,255,255,.95);
      box-shadow: 0 16px 36px rgba(0,0,0,.06);
      padding: 28px;
    }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    p {{ margin: 0 0 20px; color: #666666; line-height: 1.6; }}
    label {{ display: block; margin-top: 14px; font-size: 13px; font-weight: 700; }}
    input {{
      width: 100%;
      height: 42px;
      margin-top: 7px;
      border: 1px solid rgba(17,17,17,.14);
      border-radius: 8px;
      padding: 0 12px;
      font: inherit;
    }}
    button {{
      width: 100%;
      height: 42px;
      margin-top: 20px;
      border: 0;
      border-radius: 8px;
      background: #3b5fe6;
      color: #ffffff;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    .error {{ margin: 12px 0 0; color: #b42318; }}
    .hint {{ margin: 14px 0 0; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>商品期货全品种监控</h1>
    <p>登录后查看供需、盘面、小时结构和期权结构判断。</p>
    <form method="post" action="/login">
      <label for="username">账号</label>
      <input id="username" name="username" autocomplete="username" required>
      <label for="password">密码</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">登录</button>
      {error_markup}
      {dev_hint}
    </form>
  </main>
</body>
</html>"""


def _html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>商品期货全品种监控</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f1f1f1;
      --panel: rgba(255, 255, 255, 0.95);
      --panel-strong: #ffffff;
      --line: rgba(18, 18, 18, 0.1);
      --ink: #111111;
      --muted: #666666;
      --green: #1f6a46;
      --red: #b42318;
      --blue: #3b5fe6;
      --blue-soft: rgba(59, 95, 230, 0.12);
      --hero: linear-gradient(180deg, #ffffff 0%, #f6f7fb 100%);
      --shadow: 0 16px 36px rgba(0, 0, 0, 0.06);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
      font-size: 14px;
      background-image:
        linear-gradient(180deg, rgba(255,255,255,0.9) 0%, rgba(255,255,255,0.24) 32%),
        radial-gradient(circle at top right, rgba(59, 95, 230, 0.08), transparent 24%);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 24px;
      margin: 18px auto 0;
      max-width: 1440px;
      background: rgba(255, 255, 255, 0.78);
      border: 1px solid rgba(17, 17, 17, 0.06);
      border-radius: 10px;
      backdrop-filter: blur(16px);
      box-shadow: var(--shadow);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }
    .brand-logo-wrap {
      padding: 10px 14px;
      border-radius: 10px;
      background: #ffffff;
      border: 1px solid rgba(17, 17, 17, 0.08);
      box-shadow: 0 6px 16px rgba(0,0,0,0.04);
      flex: 0 0 auto;
    }
    .zemira-lockup {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .zemira-lockup.compact { gap: 12px; }
    .zemira-mark {
      position: relative;
      width: 62px;
      height: 62px;
      flex: 0 0 auto;
    }
    .zemira-mark .base {
      position: absolute;
      left: 0;
      bottom: 0;
      width: 44px;
      height: 44px;
      background: #111111;
    }
    .zemira-mark .cut {
      position: absolute;
      right: 9px;
      top: 18px;
      width: 18px;
      height: 18px;
      background: #f1f1f1;
    }
    .zemira-mark .blue {
      position: absolute;
      right: 0;
      top: 12px;
      width: 24px;
      height: 24px;
      background: var(--blue);
    }
    .zemira-mark .dot-top {
      position: absolute;
      right: 0;
      top: 0;
      width: 8px;
      height: 8px;
      background: #111111;
    }
    .zemira-mark .dot-right {
      position: absolute;
      right: -9px;
      top: 14px;
      width: 14px;
      height: 14px;
      background: #111111;
    }
    .zemira-wordmark {
      min-width: 0;
      color: #111111;
    }
    .zemira-wordmark strong {
      display: block;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.12em;
      white-space: nowrap;
    }
    .zemira-wordmark em {
      display: block;
      margin-top: 4px;
      color: #555555;
      font-size: 11px;
      font-style: normal;
      letter-spacing: 0.06em;
      white-space: nowrap;
    }
    .brand-copy { min-width: 0; }
    .brand-copy strong {
      display: block;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    .brand-copy span {
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
    }
    .clock-wrap {
      text-align: right;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .clock-wrap strong {
      display: block;
      font-size: 14px;
      color: var(--blue);
    }
    .logout-link {
      display: inline-block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      text-decoration: none;
    }
    .logout-link:hover { color: var(--blue); }
    h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: 0; }
    main { padding: 18px 24px 28px; max-width: 1440px; margin: 0 auto; }
    .hero {
      position: relative;
      overflow: hidden;
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.9fr);
      gap: 18px;
      padding: 24px;
      margin-bottom: 16px;
      border-radius: 10px;
      background: var(--hero);
      color: var(--ink);
      border: 1px solid rgba(17, 17, 17, 0.06);
      box-shadow: var(--shadow);
    }
    .hero::after {
      content: "";
      position: absolute;
      inset: 0 0 auto auto;
      width: 38%;
      height: 100%;
      background:
        linear-gradient(135deg, transparent 0 35%, rgba(59, 95, 230, 0.07) 35% 55%, transparent 55%),
        linear-gradient(180deg, rgba(59, 95, 230, 0.08), transparent 70%);
      pointer-events: none;
    }
    .hero-copy h2 {
      margin: 0;
      max-width: 12ch;
      font-size: clamp(28px, 4vw, 42px);
      line-height: 1.02;
      font-weight: 750;
    }
    .hero-copy p {
      margin: 14px 0 0;
      max-width: 64ch;
      color: var(--muted);
      line-height: 1.7;
      font-size: 14px;
    }
    .hero-points {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 20px;
    }
    .hero-point {
      padding: 12px 14px;
      border: 1px solid rgba(17, 17, 17, 0.08);
      border-radius: 8px;
      background: rgba(255,255,255,0.84);
    }
    .hero-point strong {
      display: block;
      font-size: 12px;
      color: var(--blue);
      font-weight: 600;
    }
    .hero-point span {
      display: block;
      margin-top: 8px;
      font-size: 13px;
      line-height: 1.45;
      font-weight: 650;
    }
    .hero-side {
      display: grid;
      gap: 12px;
      align-content: start;
    }
    .hero-side-card {
      padding: 16px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.92);
      color: var(--ink);
      border: 1px solid rgba(17, 17, 17, 0.08);
      min-height: 112px;
    }
    .hero-side-card.logo-card {
      padding: 18px;
      min-height: 168px;
      display: grid;
      align-content: start;
      gap: 12px;
    }
    .hero-side-card.logo-card .zemira-lockup {
      justify-content: center;
      padding: 10px 0 2px;
    }
    .hero-side-card.logo-card .zemira-mark {
      width: 96px;
      height: 96px;
    }
    .hero-side-card.logo-card .zemira-mark .base {
      width: 68px;
      height: 68px;
    }
    .hero-side-card.logo-card .zemira-mark .cut {
      right: 14px;
      top: 28px;
      width: 28px;
      height: 28px;
      background: rgba(255, 255, 255, 0.92);
    }
    .hero-side-card.logo-card .zemira-mark .blue {
      width: 36px;
      height: 36px;
      top: 18px;
    }
    .hero-side-card.logo-card .zemira-mark .dot-top {
      width: 11px;
      height: 11px;
    }
    .hero-side-card.logo-card .zemira-mark .dot-right {
      right: -12px;
      top: 21px;
      width: 19px;
      height: 19px;
    }
    .hero-side-card.logo-card .zemira-wordmark {
      text-align: center;
    }
    .hero-side-card.logo-card .zemira-wordmark strong {
      font-size: 18px;
      letter-spacing: 0.16em;
    }
    .hero-side-card.logo-card .zemira-wordmark em {
      font-size: 12px;
    }
    .hero-side-card span {
      display: block;
      color: var(--muted);
      font-size: 12px;
    }
    .hero-side-card strong {
      display: block;
      margin-top: 10px;
      font-size: 20px;
      line-height: 1.2;
    }
    .hero-side-card p {
      margin: 10px 0 0;
      color: var(--muted);
      line-height: 1.55;
    }
    .toolbar {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 16px;
      padding: 16px 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: var(--shadow);
    }
    button, a.download {
      border: 1px solid var(--line);
      background: var(--panel-strong);
      color: var(--ink);
      height: 38px;
      padding: 0 16px;
      border-radius: 8px;
      font-weight: 650;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }
    button.primary {
      background: linear-gradient(135deg, #4066f0 0%, #2f4fd0 100%);
      color: #fff;
      border-color: rgba(59, 95, 230, 0.9);
      box-shadow: 0 8px 18px rgba(59, 95, 230, 0.2);
    }
    button:disabled { opacity: .55; cursor: wait; }
    .status { color: var(--muted); min-height: 20px; font-size: 13px; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 16px;
      min-height: 78px;
      box-shadow: var(--shadow);
    }
    .metric span { display: block; color: var(--muted); font-size: 12px; }
    .metric strong {
      display: block;
      margin-top: 10px;
      font-size: 24px;
      font-weight: 730;
      color: var(--ink);
    }
    .notes {
      display: grid;
      grid-template-columns: 1.35fr 1fr;
      gap: 12px;
      margin-bottom: 16px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 18px;
      box-shadow: var(--shadow);
    }
    .panel h2 {
      margin: 0 0 12px;
      font-size: 15px;
      line-height: 1.3;
      color: var(--ink);
    }
    .rule {
      padding: 12px 0;
      border-top: 1px solid var(--line);
    }
    .rule:first-of-type { border-top: 0; padding-top: 0; }
    .rule strong {
      display: block;
      margin-bottom: 5px;
      font-size: 13px;
    }
    .rule p, .coverage p {
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }
    .coverage p + p { margin-top: 10px; }
    .coverage strong { color: var(--ink); }
    .source-panel {
      margin-bottom: 16px;
    }
    .source-headline {
      margin: 0;
      color: var(--muted);
      line-height: 1.7;
    }
    .source-policy {
      margin: 12px 0 0;
      padding: 12px 14px;
      border-radius: 8px;
      background: rgba(59, 95, 230, 0.06);
      color: #31415d;
      line-height: 1.65;
    }
    .source-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 14px;
    }
    .source-item {
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.88);
    }
    .source-item strong {
      display: block;
      margin-bottom: 8px;
      font-size: 13px;
    }
    .source-item p {
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }
    .source-footnote {
      margin: 14px 0 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 12px;
    }
    .table-wrap {
      overflow-x: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: var(--shadow);
    }
    table { width: 100%; min-width: 1300px; border-collapse: collapse; }
    th, td { padding: 12px 12px; border-bottom: 1px solid var(--line); vertical-align: top; text-align: left; }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #f7f7f8;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    tr:last-child td { border-bottom: 0; }
    tbody tr:hover { background: rgba(59, 95, 230, 0.05); }
    .dir { font-weight: 800; }
    .long { color: var(--green); }
    .short { color: var(--red); }
    .reason { max-width: 460px; line-height: 1.6; color: #39424d; }
    .option-cell {
      min-width: 260px;
      max-width: 360px;
      line-height: 1.55;
      color: #39424d;
    }
    .option-meta {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .option-missing {
      color: var(--muted);
    }
    .option-panel {
      display: none;
      margin-bottom: 16px;
    }
    .option-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .option-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255,255,255,0.88);
      padding: 14px;
      min-height: 150px;
    }
    .option-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }
    .option-card-title strong {
      display: block;
      font-size: 15px;
      line-height: 1.25;
    }
    .option-card-title span {
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }
    .option-badge {
      border-radius: 999px;
      padding: 4px 8px;
      background: var(--blue-soft);
      color: var(--blue);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .option-facts {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .option-fact {
      border: 1px solid rgba(17, 17, 17, 0.08);
      border-radius: 8px;
      padding: 9px 10px;
      background: #ffffff;
    }
    .option-fact span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.25;
    }
    .option-fact strong {
      display: block;
      margin-top: 5px;
      font-size: 13px;
      line-height: 1.25;
    }
    .option-judgement {
      margin: 0;
      color: #39424d;
      line-height: 1.6;
    }
    .option-panel-note {
      margin: 12px 0 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 12px;
    }
    .cell-symbol strong { font-size: 14px; }
    .cell-symbol span {
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
    }
    .empty {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 36px 28px;
      color: var(--muted);
      text-align: center;
      box-shadow: var(--shadow);
    }
    @media (max-width: 760px) {
      header {
        margin: 12px 14px 0;
        padding: 14px;
        align-items: flex-start;
        gap: 12px;
      }
      main { padding: 14px; }
      .hero {
        grid-template-columns: 1fr;
        padding: 18px;
      }
      .hero-points { grid-template-columns: 1fr; }
      .summary { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
      .notes { grid-template-columns: 1fr; }
      .source-grid { grid-template-columns: 1fr; }
      .option-grid { grid-template-columns: 1fr; }
      button, a.download { flex: 1 1 150px; }
      .clock-wrap { text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-logo-wrap" aria-label="泽沐资本 Zemira Capital logo">
        <div class="zemira-lockup compact">
          <div class="zemira-mark" aria-hidden="true">
            <span class="base"></span>
            <span class="cut"></span>
            <span class="blue"></span>
            <span class="dot-top"></span>
            <span class="dot-right"></span>
          </div>
          <div class="zemira-wordmark">
            <strong>泽沐资本</strong>
            <em>ZEMIRA CAPITAL</em>
          </div>
        </div>
      </div>
      <div class="brand-copy">
        <strong>商品期货全品种监控</strong>
        <span>泽沐资本商品研究系统</span>
      </div>
    </div>
    <div class="clock-wrap">
      <span>实时监控时点</span>
      <strong id="clock"></strong>
      <a class="logout-link" href="/logout">退出登录</a>
    </div>
  </header>
  <main>
    <section class="hero">
      <div class="hero-copy">
        <h2>供需矛盾与盘面强弱同屏观察</h2>
        <p>
          用统一口径跟踪国内库存、LME / COMEX 海外库存、增仓确认和小时级别触发，把强逻辑与好位置放在一张桌面上看。
        </p>
        <div class="hero-points">
          <div class="hero-point">
            <strong>供需口径</strong>
            <span>国内库存优先，外盘映射品种加入海内外库存与价格对比。</span>
          </div>
          <div class="hero-point">
            <strong>资金确认</strong>
            <span>多头看上涨增仓，空头看下跌增仓，过滤弱确认噪音。</span>
          </div>
          <div class="hero-point">
            <strong>触发结构</strong>
            <span>小时级别只认 N 字反转和震荡中枢突破。</span>
          </div>
        </div>
      </div>
      <div class="hero-side">
        <div class="hero-side-card logo-card">
          <span>泽沐资本</span>
          <div class="zemira-lockup" aria-label="泽沐资本 Zemira Capital logo">
            <div class="zemira-mark" aria-hidden="true">
              <span class="base"></span>
              <span class="cut"></span>
              <span class="blue"></span>
              <span class="dot-top"></span>
              <span class="dot-right"></span>
            </div>
            <div class="zemira-wordmark">
              <strong>泽沐资本</strong>
              <em>ZEMIRA CAPITAL</em>
            </div>
          </div>
          <p>商品期货全品种监控</p>
        </div>
        <div class="hero-side-card">
          <span>研究方法</span>
          <strong>商品期货全品种监控</strong>
          <p>把库存、外盘联动、持仓确认和结构触发放在一张桌面上同步观察。</p>
        </div>
        <div class="hero-side-card">
          <span>输出目标</span>
          <strong>3 只左右最清晰的方向性机会</strong>
          <p>先筛掉逻辑模糊、资金确认不足和小时线未触发的候选。</p>
        </div>
      </div>
    </section>
    <section class="toolbar">
      <button id="sampleBtn">跑样例</button>
      <button id="akBtn" class="primary">跑实盘 AkShare</button>
      <a id="download" class="download" href="#" style="display:none">下载 CSV</a>
      <div id="status" class="status"></div>
    </section>
    <section class="summary">
      <div class="metric"><span>数据源</span><strong id="provider">-</strong></div>
      <div class="metric"><span>候选数量</span><strong id="count">-</strong></div>
      <div class="metric"><span>最强方向</span><strong id="topDirection">-</strong></div>
      <div class="metric"><span>最高分</span><strong id="topScore">-</strong></div>
    </section>
    <section class="notes">
      <section class="panel">
        <h2>判定说明</h2>
        <div class="rule">
          <strong id="ruleSupplyTitle">供需矛盾怎么判断</strong>
          <p id="ruleSupplyBody">当前版本优先看国内库存30日变化；有外盘映射的品种再看LME或COMEX库存与价格是否同方向。</p>
        </div>
        <div class="rule">
          <strong id="rulePositionTitle">技术面怎么确认</strong>
          <p id="rulePositionBody">持仓20日变化必须明显增加。多头重点看上涨增仓，空头重点看下跌增仓。</p>
        </div>
        <div class="rule">
          <strong id="ruleIntradayTitle">小时级别怎么触发</strong>
          <p id="ruleIntradayBody">小时线只认N字反转或震荡中枢突破，并且要和日线方向一致。</p>
        </div>
        <div class="rule">
          <strong id="ruleOptionTitle">期权结构怎么判断</strong>
          <p id="ruleOptionBody">真实期权链可用时，计算Call/Put持仓墙、净增仓、ATM IV和Skew；字段缺失则留空。</p>
        </div>
      </section>
      <section class="panel coverage">
        <h2>本次数据可用性</h2>
        <p id="coverageSummary">运行后会显示这次拿到了多少个品种的库存和小时线数据。</p>
        <p id="coverageInventory">库存数据：-</p>
        <p id="coverageExternalInventory">海外库存：-</p>
        <p id="coverageExternalPrice">外盘价格：-</p>
        <p id="coverageHourly">小时线数据：-</p>
        <p id="coverageOption">期权结构：-</p>
      </section>
    </section>
    <section class="panel source-panel">
      <h2>数据来源与严谨性</h2>
      <p id="sourceHeadline" class="source-headline">运行后会显示实盘模式的真实数据链路，以及程序对缺失数据的处理原则。</p>
      <div id="sourcePolicy" class="source-policy">原则：只用公开可复核数据；取不到就留空；不把缺失值硬解释成方向结论。</div>
      <div id="sourceGrid" class="source-grid"></div>
      <p id="sourceFootnote" class="source-footnote"></p>
    </section>
    <section id="optionPanel" class="panel option-panel">
      <h2>期权结构判断</h2>
      <div id="optionGrid" class="option-grid"></div>
      <p class="option-panel-note">只展示交易所公开期权链能计算出的结构；隐波或 Delta 字段缺失时，IV 与 Skew 保持留空。</p>
    </section>
    <section id="content" class="empty">点击上方按钮开始运行。</section>
  </main>
  <script>
    const statusEl = document.querySelector("#status");
    const contentEl = document.querySelector("#content");
    const sampleBtn = document.querySelector("#sampleBtn");
    const akBtn = document.querySelector("#akBtn");
    const download = document.querySelector("#download");
    const ruleSupplyTitleEl = document.querySelector("#ruleSupplyTitle");
    const ruleSupplyBodyEl = document.querySelector("#ruleSupplyBody");
    const rulePositionTitleEl = document.querySelector("#rulePositionTitle");
    const rulePositionBodyEl = document.querySelector("#rulePositionBody");
    const ruleIntradayTitleEl = document.querySelector("#ruleIntradayTitle");
    const ruleIntradayBodyEl = document.querySelector("#ruleIntradayBody");
    const ruleOptionTitleEl = document.querySelector("#ruleOptionTitle");
    const ruleOptionBodyEl = document.querySelector("#ruleOptionBody");
    const coverageSummaryEl = document.querySelector("#coverageSummary");
    const coverageInventoryEl = document.querySelector("#coverageInventory");
    const coverageExternalInventoryEl = document.querySelector("#coverageExternalInventory");
    const coverageExternalPriceEl = document.querySelector("#coverageExternalPrice");
    const coverageHourlyEl = document.querySelector("#coverageHourly");
    const coverageOptionEl = document.querySelector("#coverageOption");
    const sourceHeadlineEl = document.querySelector("#sourceHeadline");
    const sourcePolicyEl = document.querySelector("#sourcePolicy");
    const sourceGridEl = document.querySelector("#sourceGrid");
    const sourceFootnoteEl = document.querySelector("#sourceFootnote");
    const optionPanelEl = document.querySelector("#optionPanel");
    const optionGridEl = document.querySelector("#optionGrid");

    const pct = value => value === null || Number.isNaN(value) ? "" : `${(value * 100).toFixed(2)}%`;
    const num = value => value === null || Number.isNaN(value) ? "" : Number(value).toFixed(3);
    const level = value => value === null || Number.isNaN(value) ? "" : Number(value).toFixed(2);
    const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const whole = value => value === null || Number.isNaN(value) ? "" : Number(value).toFixed(0);

    function renderOption(option) {
      if (!option || !option.available) {
        return `<span class="option-missing">${esc(option?.summary || "期权数据暂缺")}</span>`;
      }
      const lines = [];
      if (option.call_wall !== null) lines.push(`Call Wall ${whole(option.call_wall)} / OI ${whole(option.call_wall_oi)}`);
      if (option.put_wall !== null) lines.push(`Put Wall ${whole(option.put_wall)} / OI ${whole(option.put_wall_oi)}`);
      if (option.call_oi_change !== null || option.put_oi_change !== null) {
        lines.push(`净增仓 C ${whole(option.call_oi_change)} / P ${whole(option.put_oi_change)}`);
      }
      if (option.atm_iv !== null) lines.push(`ATM IV ${pct(option.atm_iv)}`);
      if (option.skew_25d !== null) lines.push(`25D Skew ${pct(option.skew_25d)}`);
      if (option.skew_10d !== null) lines.push(`10D Skew ${pct(option.skew_10d)}`);
      return `${esc(lines.join("；") || option.summary)}<span class="option-meta">${esc(option.trade_date)} · ${esc(option.source)}</span>`;
    }

    function optionBias(row, option) {
      if (!option || !option.available) return "期权链暂缺";
      const callChange = Number(option.call_oi_change ?? 0);
      const putChange = Number(option.put_oi_change ?? 0);
      if (callChange > putChange && row.direction === "多头") return "期权净增仓顺着多头方向";
      if (putChange > callChange && row.direction === "空头") return "期权净增仓顺着空头方向";
      if (callChange > putChange) return "Call净增仓更强，和当前方向存在分歧";
      if (putChange > callChange) return "Put净增仓更强，和当前方向存在分歧";
      return "Call与Put净增仓接近，期权方向信号中性";
    }

    function renderOptionPanel(rows) {
      const cards = rows.map(row => {
        const option = row.option_structure;
        const available = option && option.available;
        return `
          <article class="option-card">
            <div class="option-card-head">
              <div class="option-card-title">
                <strong>${esc(row.name)} ${esc(row.direction)}</strong>
                <span>${esc(row.symbol)} · ${available ? esc(option.trade_date) : "期权数据暂缺"}</span>
              </div>
              <span class="option-badge">${available ? "已接入" : "缺失"}</span>
            </div>
            <div class="option-facts">
              <div class="option-fact"><span>Call Wall</span><strong>${available && option.call_wall !== null ? whole(option.call_wall) : "-"}</strong></div>
              <div class="option-fact"><span>Put Wall</span><strong>${available && option.put_wall !== null ? whole(option.put_wall) : "-"}</strong></div>
              <div class="option-fact"><span>Call净增仓</span><strong>${available && option.call_oi_change !== null ? whole(option.call_oi_change) : "-"}</strong></div>
              <div class="option-fact"><span>Put净增仓</span><strong>${available && option.put_oi_change !== null ? whole(option.put_oi_change) : "-"}</strong></div>
            </div>
            <p class="option-judgement">${esc(available ? optionBias(row, option) : option?.summary || "暂未配置该品种期权链。")}</p>
            <span class="option-meta">${esc(available ? option.source : "")}</span>
          </article>
        `;
      }).join("");
      optionGridEl.innerHTML = cards;
      optionPanelEl.style.display = rows.length ? "block" : "none";
    }

    function setBusy(busy) {
      sampleBtn.disabled = busy;
      akBtn.disabled = busy;
    }

    async function run(provider) {
      setBusy(true);
      statusEl.textContent = provider === "akshare" ? "正在拉取实盘数据..." : "正在生成样例结果...";
      download.style.display = "none";
      try {
        const res = await fetch(`/api/run?provider=${provider}`);
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || "运行失败");
        render(data);
        statusEl.textContent = `运行完成，CSV 已生成：${data.csv}`;
      } catch (err) {
        statusEl.textContent = err.message;
        contentEl.className = "empty";
        contentEl.textContent = "运行失败。";
      } finally {
        setBusy(false);
      }
    }

    function render(data) {
      document.querySelector("#provider").textContent = data.provider;
      document.querySelector("#count").textContent = data.count;
      document.querySelector("#topDirection").textContent = data.rows[0]?.direction ?? "-";
      document.querySelector("#topScore").textContent = data.rows[0]?.score ?? "-";
      ruleSupplyTitleEl.textContent = data.explanation.supply_demand_title;
      ruleSupplyBodyEl.textContent = data.explanation.supply_demand_body;
      rulePositionTitleEl.textContent = data.explanation.positioning_title;
      rulePositionBodyEl.textContent = data.explanation.positioning_body;
      ruleIntradayTitleEl.textContent = data.explanation.intraday_title;
      ruleIntradayBodyEl.textContent = data.explanation.intraday_body;
      ruleOptionTitleEl.textContent = data.explanation.option_title;
      ruleOptionBodyEl.textContent = data.explanation.option_body;
      coverageSummaryEl.textContent =
        `当前配置要求${data.coverage.supply_required ? "方向化供需一致" : "供需中性可过"}，` +
        `${data.coverage.intraday_required ? "并且必须有小时触发。" : "小时触发作为加分。"}`
      coverageInventoryEl.innerHTML =
        `库存数据：<strong>${data.coverage.inventory_ready} / ${data.coverage.contracts}</strong> 个品种拿到足够的30日库存。`;
      coverageExternalInventoryEl.innerHTML =
        `海外库存：<strong>${data.coverage.external_inventory_ready} / ${data.coverage.contracts}</strong> 个品种拿到LME/COMEX库存。`;
      coverageExternalPriceEl.innerHTML =
        `外盘价格：<strong>${data.coverage.external_price_ready} / ${data.coverage.contracts}</strong> 个品种拿到外盘20日价格数据。`;
      coverageHourlyEl.innerHTML =
        `小时线数据：<strong>${data.coverage.hourly_ready} / ${data.coverage.contracts}</strong> 个品种拿到小时线；持仓门槛是 <strong>${pct(data.coverage.positioning_threshold)}</strong>。`;
      coverageOptionEl.innerHTML =
        `期权结构：<strong>${data.coverage.option_ready} / ${data.coverage.option_checked}</strong> 个候选品种拿到交易所期权链。`;
      sourceHeadlineEl.textContent = data.sources?.headline ?? "当前 provider 未返回来源说明。";
      sourcePolicyEl.textContent = data.sources?.policy ?? "原则：缺失数据留空，不做主观补值。";
      sourceGridEl.innerHTML = (data.sources?.items ?? []).map(item => `
        <article class="source-item">
          <strong>${esc(item.title)}</strong>
          <p>${esc(item.detail)}</p>
        </article>
      `).join("");
      const externalContracts = data.sources?.external_contracts?.length
        ? `外盘映射品种：${data.sources.external_contracts.join("、")} 。`
        : "";
      sourceFootnoteEl.textContent =
        `${data.sources?.strict ? "实盘模式" : "样例模式"}：${externalContracts}程序所有供需结论都先展示原始变化，再给解释文本。`;
      download.href = `/download/${data.provider}_selection.csv`;
      download.style.display = "inline-flex";
      if (!data.rows.length) {
        optionPanelEl.style.display = "none";
        contentEl.className = "empty";
        contentEl.textContent = "没有候选。当前过滤条件下，没有品种同时满足供需、增仓和小时触发。先看上面的数据可用性，尤其是库存覆盖率。";
        return;
      }
      renderOptionPanel(data.rows);
      contentEl.className = "table-wrap";
      contentEl.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>排名</th><th>品种</th><th>方向</th><th>综合分</th><th>多头分</th><th>空头分</th>
              <th>20日</th><th>60日</th><th>库存30日</th><th>持仓20日</th><th>成交10日</th>
              <th>供需分析</th><th>期权结构</th><th>小时结构</th><th>触发位</th><th>理由</th>
            </tr>
          </thead>
          <tbody>
            ${data.rows.map(row => `
              <tr>
                <td>${row.rank}</td>
                <td class="cell-symbol"><strong>${esc(row.name)}</strong><span>${esc(row.symbol)} / ${esc(row.sector)}</span></td>
                <td class="dir ${row.direction === "多头" ? "long" : "short"}">${esc(row.direction)}</td>
                <td>${num(row.score)}</td>
                <td>${num(row.long_score)}</td>
                <td>${num(row.short_score)}</td>
                <td>${pct(row.return_20d)}</td>
                <td>${pct(row.return_60d)}</td>
                <td>${pct(row.inventory_30d)}</td>
                <td>${pct(row.oi_20d)}</td>
                <td>${pct(row.volume_ratio_10d)}</td>
                <td class="reason">${esc(row.supply_demand_analysis || "")}</td>
                <td class="option-cell">${renderOption(row.option_structure)}</td>
                <td>${esc(row.hourly_setup || "")}</td>
                <td>${level(row.hourly_trigger_level)}</td>
                <td class="reason">${esc(row.reason)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>`;
    }

    sampleBtn.addEventListener("click", () => run("sample"));
    akBtn.addEventListener("click", () => run("akshare"));
    setInterval(() => {
      document.querySelector("#clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
    }, 1000);
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()
