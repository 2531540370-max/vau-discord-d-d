import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

URL = "https://nrl.vau.edu.vn/events"
HOME_DIR = Path(".vau-state")
HOME_DIR.mkdir(exist_ok=True)
CFG = HOME_DIR / "bot.json"
STATE = HOME_DIR / "su-kien-v2.json"


def save(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
    temp.replace(path)


def post(url, message):
    payload = {"content": message[:2000], "allowed_mentions": {"parse": []}}
    req = Request(url + "?wait=true", data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "User-Agent": "VAU-Discord/1.0"})
    with urlopen(req, timeout=30) as response:
        if not json.load(response).get("id"):
            raise RuntimeError("Discord chưa xác nhận tin nhắn.")


def read_events(page, tab):
    response = page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    if response and response.status >= 400:
        raise RuntimeError(f"Trang trường trả mã {response.status}")
    # Mobile and desktop layouts may expose different controls.
    labels = page.get_by_text(tab, exact=True)
    try:
        labels.first.wait_for(state="attached", timeout=10000)
        control = next((item for item in labels.all() if item.is_visible()), None)
        if control is None:
            raise RuntimeError("Mục đang bị ẩn")
        control.click(timeout=7000)
    except Exception:
        body = " ".join(page.locator("body").inner_text(timeout=4000).split())
        raise RuntimeError(f"Chưa chọn được mục {tab}. Trang hiển thị: {body[:180]}")
    card = page.get_by_text("Thời gian đăng ký:", exact=False)
    empty = page.get_by_text(re.compile(r"^(?:Không (?:tìm thấy|có)|Chưa có).*?(?:sự kiện|hoạt động)", re.I))
    card.or_(empty).first.wait_for(timeout=30000)
    page.wait_for_timeout(1500)
    if not card.count() and empty.is_visible():
        return []
    rows = page.evaluate("""() => {
      const rows = [];
      for (const el of document.querySelectorAll('body *')) {
        if (!el.getClientRects().length || !el.textContent.trim().startsWith('Thời gian đăng ký:') ||
            [...el.children].some(c => c.textContent.trim().startsWith('Thời gian đăng ký:'))) continue;
        let box = el;
        while (box && !(box.querySelector('img,h2,h3,h4') && box.innerText.includes('Thời gian diễn ra:'))) box = box.parentElement;
        if (!box || box.innerText.split('Thời gian đăng ký:').length !== 2) throw Error('Thẻ sự kiện đã đổi');
        const lines = box.innerText.split(/\\n/).map(x => x.trim()).filter(Boolean);
        const title = box.querySelector('h2,h3,h4')?.textContent.trim() || lines[0];
        const link = box.closest('a[href]') || box.querySelector('a[href]');
        rows.push({title, registration: lines.find(x=>x.startsWith('Thời gian đăng ký:')),
          when: lines.find(x=>x.startsWith('Thời gian diễn ra:')), url:link?.href || ''});
      }
      return rows;
    }""")
    if not rows or any(not r.get("registration") or not r.get("when") for r in rows):
        raise RuntimeError("Không đọc đủ thẻ sự kiện.")
    return rows


def main():
    cfg = {"url": os.environ["DISCORD_WEBHOOK_URL"]}
    if not cfg["url"]:
        raise SystemExit("Thiếu secret DISCORD_WEBHOOK_URL")
    state = json.loads(STATE.read_text(encoding="utf8")) if STATE.exists() else {"seen": [], "day": ""}
    def send(message):
        post(cfg["url"], message)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(locale="vi-VN", viewport={"width": 1440, "height": 900})
        previous = tuple(state["status"]) if "status" in state else None
        while True:
            failed = []
            total = 0
            fresh = 0
            baseline = False
            for tab in ("Đang mở đăng ký", "Đang diễn ra", "Đã kết thúc"):
                print(time.strftime("%H:%M:%S"), "Đang đọc:", tab, flush=True)
                try:
                    rows = read_events(page, tab)
                    # Existing v2 state represents a successful scan of every tab.
                    if "ready" not in state:
                        state["ready"] = (["Đang mở đăng ký", "Đang diễn ra", "Đã kết thúc"]
                                          if STATE.exists() else [])
                    first = tab not in state["ready"]
                    for row in rows:
                        identity = row["url"] if row["url"].startswith(URL + "/") else row["title"] + "|" + row["when"]
                        key = hashlib.sha256(identity.encode()).hexdigest()
                        if key in state["seen"]:
                            continue
                        if not first:
                            send("\n".join(["VAU • Sự kiện mới xuất hiện", row["title"],
                                            row["registration"], row["when"], row["url"] or URL]))
                        fresh += int(not first)
                        state["seen"].append(key)
                        if not first:
                            save(STATE, state)
                    baseline |= first
                    total += len(rows)
                    if first:
                        state["ready"].append(tab)
                    save(STATE, state)
                    print("  Đã đọc:", len(rows), "sự kiện.", flush=True)
                except Exception as error:
                    failed.append(tab)
                    details = str(error).splitlines()
                    reason = details[0] if details else type(error).__name__
                    print("  Lỗi:", re.sub(r"https://[^ ]*discord[^ ]*", "[Discord]", reason),
                          flush=True)
                    if "Timeout" in type(error).__name__:
                        print("  Trang đang mở:", page.url.split("?")[0], flush=True)
            now = datetime.now(timezone(timedelta(hours=7)))
            status = tuple(failed)
            day = now.date().isoformat()
            daily = now.hour >= 20 and state.get("report_day") != day
            if previous != status or daily:
                if failed:
                    message = "VAU • Chưa kiểm tra đủ. Mục gặp lỗi: " + ", ".join(failed) + ". Tool sẽ thử lại."
                elif baseline:
                    message = f"VAU • Đã kiểm tra đủ 3 mục, ghi nhận {total} sự kiện hiện có làm mốc theo dõi."
                elif fresh:
                    message = f"VAU • Đã kiểm tra đủ 3 mục. Lượt này phát hiện {fresh} sự kiện mới."
                else:
                    message = "VAU • Đã kiểm tra đủ 3 mục. Lượt này không thấy sự kiện mới."
                try:
                    send(now.strftime("%H:%M %d/%m") + " | " + message)
                    if daily:
                        state["report_day"] = day
                        if state.get("ready"):
                            save(STATE, state)
                    state["status"] = list(status)
                    if "ready" not in state:
                        state["ready"] = []
                    save(STATE, state)
                except Exception as error:
                    print("Chưa gửi được kết quả Discord:", type(error).__name__, flush=True)
            if failed:
                raise SystemExit("Có mục chưa kiểm tra được")
            return


if __name__ == "__main__":
    main()
