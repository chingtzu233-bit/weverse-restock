#!/usr/bin/env python3
"""Read-only Weverse restock monitor. Python 3.10+; no checkout automation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib import error, request
from urllib.parse import urlparse

PRODUCT_NAME = "SOM-MOONG-CHILL KEYRING_JAEHYUN ver."
PRODUCT_URL = "https://shop.weverse.io/en/shop/USD/artists/127/sales/61792"
SALE_ID = "61792"
STATE_FILE = Path("state/stock.json")
OUT_LABELS = {"SOLD OUT", "OUT OF STOCK", "售罄", "已售完", "품절"}
BUY_LABELS = {"BUY NOW", "BUY", "PURCHASE", "ADD TO CART", "PRE-ORDER",
              "PREORDER", "立即購買", "購買", "加入購物車", "預購", "구매하기"}

# Anchor to the product heading; never scan recommendations or the entire page
# for a generic Buy button. CSS class names are deliberately not hardcoded.
SNAPSHOT_JS = r"""() => {
  const title = Array.from(document.querySelectorAll('h1')).find(
    h => h.innerText.trim() === 'SOM-MOONG-CHILL KEYRING_JAEHYUN ver.');
  if (!title) return {heading: '', buttons: []};
  const known = /^(sold out|out of stock|buy now|buy|purchase|add to cart|pre-order|preorder|售罄|已售完|立即購買|購買|加入購物車|預購|품절|구매하기)$/i;
  let p = title;
  for (let depth = 0; p && depth < 7; depth++, p = p.parentElement) {
    if (p === document.body || p === document.documentElement || p.tagName === 'MAIN') break;
    if (Array.from(p.querySelectorAll('h1,h2,h3')).some(h => h !== title)) break;
    const buttons = Array.from(p.querySelectorAll('button')).filter(b => {
      const s = getComputedStyle(b), r = b.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' &&
             s.display !== 'none' && !b.closest('[aria-hidden="true"]');
    }).map(b => ({text: b.innerText.trim(),
      disabled: b.disabled || b.getAttribute('aria-disabled') === 'true'}));
    if (buttons.some(b => known.test(b.text.replace(/\s+/g, ' '))))
      return {heading: title.innerText.trim(), buttons};
  }
  return {heading: title.innerText.trim(), buttons: []};
}"""


class MonitorError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(snapshot: dict) -> str:
    """Only affirmative enabled purchase controls mean available."""
    if snapshot.get("heading") != PRODUCT_NAME:
        return "unknown"
    buttons = snapshot.get("buttons", [])
    buy = any(re.sub(r"\s+", " ", str(b.get("text", ""))).upper() in BUY_LABELS
              and b.get("disabled") is False for b in buttons)
    sold = any(re.sub(r"\s+", " ", str(b.get("text", ""))).upper() in OUT_LABELS
               for b in buttons)
    if sold and buy:
        return "unknown"  # Contradictory UI, possibly still rendering.
    if sold:
        return "sold_out"
    return "available" if buy else "unknown"


def read_stock(*, executable: str | None = None) -> str:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as pw:
        options = {"headless": True}
        if executable:
            options["executable_path"] = executable
        browser = pw.chromium.launch(**options)
        try:
            context = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
            page = context.new_page()
            response = page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=45000)
            if response is None or response.status >= 400:
                status = response.status if response else "no response"
                raise MonitorError(f"商品頁讀取失敗（HTTP {status}），未判定為補貨。")
            final = urlparse(page.url)
            if final.hostname != "shop.weverse.io" or not final.path.rstrip("/").endswith("/sales/" + SALE_ID):
                raise MonitorError("商品頁導向其他頁面，未判定為補貨。")
            # Wait for asynchronous content, without treating disappearance as stock.
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                text = page.locator("body").inner_text(timeout=5000)[:12000].lower()
                if any(s in text for s in ("verify you are human", "checking your browser",
                                            "automated traffic", "unusual traffic", "access denied")):
                    raise MonitorError("頁面要求驗證或拒絕存取；停止本次檢查，請手動查看。")
                result = classify(page.evaluate(SNAPSHOT_JS))
                if result != "unknown":
                    # Confirm stable rendering in the same page, no extra navigation.
                    page.wait_for_timeout(800)
                    if classify(page.evaluate(SNAPSHOT_JS)) == result:
                        return result
                page.wait_for_timeout(1000)
            raise MonitorError("找不到可信的庫存按鈕（可能需登入、頁面改版或驗證）；未判定為補貨。")
        except PWTimeout:
            raise MonitorError("商品頁逾時，未判定為補貨。") from None
        finally:
            browser.close()


def telegram_api(method: str, payload: dict, *, token: str | None = None):
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise MonitorError("請在 Secret 或環境變數設定有效的 TELEGRAM_BOT_TOKEN。")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except error.HTTPError as exc:
        # Never print exception strings: they can contain the token in the URL.
        raise MonitorError(f"Telegram HTTP {exc.code}；請確認 Bot Token、Chat ID，並先對 Bot 按 Start。") from None
    except (error.URLError, TimeoutError, ValueError, OSError):
        raise MonitorError("Telegram 連線或回應失敗；未將通知記為成功。") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise MonitorError("Telegram 拒絕請求；請檢查設定。")
    return data.get("result")


def notify(message: str, *, chat_id: str | None = None):
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not re.fullmatch(r"-?\d+", chat_id):
        raise MonitorError("請先配對，再設定數字格式的 TELEGRAM_CHAT_ID Secret。")
    telegram_api("sendMessage", {"chat_id": chat_id, "text": message,
                                  "link_preview_options": {"is_disabled": True}})


def pair_chat():
    """Use a user-created secret code to avoid claiming an arbitrary /start."""
    code = os.environ.get("PAIRING_CODE", "").strip()
    if len(code) < 16 or any(c.isspace() for c in code):
        raise MonitorError("請設定至少 16 字元、沒有空白的隨機 PAIRING_CODE Secret。")
    updates = telegram_api("getUpdates", {"timeout": 0, "limit": 100,
                                           "allowed_updates": ["message"]})
    chat_id = matching_chat(updates or [], code)
    if chat_id is None:
        raise MonitorError("未找到配對訊息。請先私訊自己的 Bot：/pair 加空白和配對碼，再重新執行 pair。")
    notify("✅ 配對成功！\n你的 TELEGRAM_CHAT_ID 是：\n" + str(chat_id) +
           "\n\n請將這個數字存到 GitHub 的 TELEGRAM_CHAT_ID Secret，然後執行 test-notification。"
           "\n這則訊息僅完成配對，尚未確認庫存監控正常。", chat_id=str(chat_id))
    print("配對資訊已私訊符合配對碼的 Telegram 帳號，沒有輸出 Token 或 Chat ID。")


def matching_chat(updates: list, code: str) -> int | None:
    matches = set()
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        if (chat.get("type") == "private" and message.get("text", "").strip() == "/pair " + code
                and isinstance(chat.get("id"), int)):
            matches.add(chat["id"])
    if len(matches) > 1:
        raise MonitorError("配對碼出現在多個帳號，請更換新配對碼後重試。")
    return next(iter(matches), None)


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("sale_id") != SALE_ID:
            return {"sale_id": SALE_ID}
        return state
    except (FileNotFoundError, ValueError):
        return {"sale_id": SALE_ID}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def apply_stock(state: dict, status: str, send=notify):
    if status not in {"sold_out", "available"}:
        raise MonitorError("庫存未知，保留上次成功狀態。")
    if status == "available" and state.get("last_stock") != "available":
        send("🟢 偵測到商品可購買！\n" + PRODUCT_NAME + "\n" + PRODUCT_URL +
             "\n\n請立即打開確認。庫存可能再次售完；配送資格需登入後確認。")
        state["last_stock_alert_at"] = now_iso()
    # A failed send leaves last_stock unchanged, so the next check retries it.
    state.update(last_stock=status, checked_at=now_iso(), failures=0)


def run_once(path=STATE_FILE, *, executable=None, check=read_stock, send=notify) -> int:
    state = load_state(path)
    try:
        status = check(executable=executable)
        apply_stock(state, status, send=send)
        state.pop("last_error", None)
        print(f"{now_iso()} {'售罄' if status == 'sold_out' else '可購買'}")
        code = 0
    except MonitorError as exc:
        state["failures"] = int(state.get("failures", 0)) + 1
        state.update(last_attempt_at=now_iso(), last_error=str(exc))
        print(str(exc), file=sys.stderr)
        last = float(state.get("last_error_alert_epoch", 0))
        if state["failures"] >= 3 and time.time() - last >= 6 * 3600:
            try:
                send("⚠️ Weverse 監控連續檢查失敗，不能確認庫存。\n" + str(exc) +
                     "\n請查看 GitHub Actions 紀錄或手動開啟：\n" + PRODUCT_URL)
                state["last_error_alert_epoch"] = time.time()
            except MonitorError:
                print("錯誤提醒也未送達；請檢查 Telegram 設定。", file=sys.stderr)
        code = 2
    save_state(path, state)
    return code


def main():
    parser = argparse.ArgumentParser(description="Weverse 補貨監控 → 免費 Telegram 通知")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="持續開機裝置：定期檢查")
    mode.add_argument("--pair", action="store_true", help="配對 Telegram 並私訊 Chat ID")
    mode.add_argument("--test-notification", action="store_true", help="只傳送測試通知")
    mode.add_argument("--dry-run", action="store_true", help="只檢查頁面，不通知、不寫狀態")
    parser.add_argument("--interval", type=int, default=30, help="watch 間隔秒數，最小 30")
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--chromium-path", default=os.environ.get("CHROMIUM_PATH"))
    args = parser.parse_args()
    try:
        if args.pair:
            pair_chat()
            return 0
        if args.test_notification:
            notify("🔔 Weverse 補貨通知測試成功！\n這是測試，不代表已補貨。\n" + PRODUCT_URL)
            print("測試通知已送出；請確認手機實際收到。")
            return 0
        if args.dry_run:
            print("庫存檢查結果：" + read_stock(executable=args.chromium_path))
            return 0
        # Fail before checking stock if notification credentials are absent.
        if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
            raise MonitorError("尚未設定 Telegram Secrets；請先按照 README 完成配對。")
        if args.interval < 30:
            raise MonitorError("檢查間隔請設為至少 30 秒。")
        if not args.watch:
            return run_once(args.state, executable=args.chromium_path)
        print("開始監控，按 Ctrl+C 停止。錯誤時會降低檢查頻率。")
        while True:
            result = run_once(args.state, executable=args.chromium_path)
            failures = load_state(args.state).get("failures", 0)
            # Stop immediately probing a failed/blocked endpoint repeatedly.
            delay = args.interval if result == 0 else max(300, min(3600, 300 * 2 ** min(int(failures) - 1, 4)))
            time.sleep(delay)
    except KeyboardInterrupt:
        print("監控已停止。")
        return 0
    except MonitorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Avoid accidental credential disclosure in a traceback from HTTP clients.
        print("程式或瀏覽器發生未預期錯誤。請確認依賴、Chromium 安裝及網路連線。", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
