# -*- coding: utf-8 -*-
"""
Бот: следит за отключениями ДТЭК Киевские электросети по одному адресу
и публикует изменения в группу Telegram.

Запуск:
    python dtek_bot.py --test   # один проход: показать данные и отправить тестовое сообщение
    python dtek_bot.py --once   # одна проверка (так его запускает GitHub по расписанию)
    python dtek_bot.py          # работать постоянно, проверка каждые CHECK_EVERY_MIN минут
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV = None  # на Windows без пакета tzdata берётся время компьютера

# ================== НАСТРОЙКИ ==================
# На GitHub всё берётся из Secrets. Для запуска на компьютере можно вписать прямо сюда.
TG_TOKEN = os.environ.get("TG_TOKEN") or "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER"
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or "ВСТАВЬ_ID_ГРУППЫ"

STREET = os.environ.get("STREET") or "вул. Окіпної Раїси"   # точно как в списке на сайте
HOUSE = os.environ.get("HOUSE") or "7/А"

GROUP = None          # группа графика (например "GPV2.1"); None = определить по адресу автоматически
CHECK_EVERY_MIN = 10  # как часто проверять сайт
HEADLESS = True       # если сайт не пускает, поставь False (откроется видимое окно браузера)
# ===============================================

URL = "https://www.dtek-kem.com.ua/ua/shutdowns"
STATE_FILE = Path(__file__).with_name("dtek_state.json")
LOG_FILE = Path(__file__).with_name("dtek_bot.log")


def now():
    return datetime.now(KYIV) if KYIV else datetime.now()


def log(msg):
    line = f"[{now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- получение данных с сайта ----------

JS_FACT = """() => {
  try { if (typeof DisconSchedule !== 'undefined' && DisconSchedule.fact) return DisconSchedule.fact; } catch (e) {}
  return null;
}"""

JS_FLAGS = """() => {
  try { return {showTableFact: DisconSchedule.showTableFact, showCurSchedule: DisconSchedule.showCurSchedule}; }
  catch (e) { return {}; }
}"""

JS_AJAX = """async ([street, update]) => {
  const token = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const body = new URLSearchParams();
  body.append('method', 'getHomeNum');
  body.append('data[0][name]', 'street');
  body.append('data[0][value]', street);
  body.append('data[1][name]', 'updateFact');
  body.append('data[1][value]', update || '');
  const r = await fetch('/ua/ajax', {
    method: 'POST',
    headers: {
      'X-CSRF-Token': token,
      'X-Requested-With': 'XMLHttpRequest',
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
    },
    body
  });
  return await r.text();
}"""


def fetch_site():
    """Открывает страницу в браузере, возвращает (fact, house_info)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(locale="uk-UA", timezone_id="Europe/Kyiv")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)  # дать защите сайта и скриптам отработать

        fact = page.evaluate(JS_FACT)
        if not fact:
            m = re.search(r"DisconSchedule\.fact\s*=\s*(\{.*?\})\s*(?:;|\n|</script>)", page.content(), re.S)
            if m:
                fact = json.loads(m.group(1))

        flags = page.evaluate(JS_FLAGS) or {}
        update = (fact or {}).get("update", "")
        raw = page.evaluate(JS_AJAX, [STREET, update])
        browser.close()

    try:
        resp = json.loads(raw)
    except Exception:
        raise RuntimeError("Сайт вернул не JSON. Начало ответа: " + raw[:300])

    if resp.get("fact"):
        fact = resp["fact"]
    if not fact:
        raise RuntimeError("Не найден график (DisconSchedule.fact) на странице.")

    houses = resp.get("data") or {}
    info = find_house(houses, HOUSE)
    if info is None:
        raise RuntimeError(f"Дом {HOUSE} не найден. Есть, например: {list(houses)[:15]}")
    info["_updated"] = resp.get("updateTimestamp", "")
    info["_flags"] = flags
    return fact, info


def norm(s):
    # латинские и кириллические буквы, пробелы, регистр
    s = str(s).upper().replace(" ", "")
    return s.translate(str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ"))


def find_house(houses, house):
    target = norm(house)
    for k, v in houses.items():
        if norm(k) == target:
            return dict(v)
    return None


# ---------- разбор графика ----------

def half_hour_slots(day):
    """day: {"1": "yes", ..., "24": "no"} -> 48 значений: 'on' / 'off' / 'maybe'."""
    slots = []
    for h in range(1, 25):
        v = str(day.get(str(h), "yes")).lower()
        if v == "no":
            a = b = "off"
        elif v == "first":
            a, b = "off", "on"
        elif v == "second":
            a, b = "on", "off"
        elif v == "maybe":
            a = b = "maybe"
        elif v == "mfirst":
            a, b = "maybe", "on"
        elif v == "msecond":
            a, b = "on", "maybe"
        else:
            a = b = "on"
        slots += [a, b]
    return slots


def t(i):
    return f"{i // 2:02d}:{'30' if i % 2 else '00'}"


def ranges(slots, kind):
    out, start = [], None
    for i, s in enumerate(slots + ["end"]):
        if s == kind and start is None:
            start = i
        elif s != kind and start is not None:
            out.append(f"{t(start)}–{t(i) if i < 48 else '24:00'}")
            start = None
    return out


def schedule_text(day, title):
    slots = half_hour_slots(day)
    off, maybe = ranges(slots, "off"), ranges(slots, "maybe")
    lines = [f"🗓 <b>{title}</b>"]
    if off:
        lines.append("Без світла: " + ", ".join(off))
    if maybe:
        lines.append("Можливі відключення: " + ", ".join(maybe))
    if not off and not maybe:
        lines.append("Відключень не заплановано ✅")
    return "\n".join(lines)


def days_of(fact):
    """Возвращает [(дата, данные дня)] по порядку: сегодня, завтра."""
    data = fact.get("data") or {}
    out = []
    for ts in sorted(data, key=lambda x: int(x)):
        dt = datetime.fromtimestamp(int(ts), KYIV) if KYIV else datetime.fromtimestamp(int(ts))
        date = dt.strftime("%d.%m.%Y")
        out.append((date, data[ts]))
    return out


def detect_group(info):
    if GROUP:
        return GROUP
    reason = info.get("sub_type_reason") or []
    if isinstance(reason, str):
        reason = [reason]
    return reason[0] if reason else None


# ---------- картинка с графиком ----------

NAVY = "#172741"
BOLT_ON = ('<svg viewBox="0 0 24 24" width="17" height="17"><path d="M13.5 2 4.5 13.6h6.6L10 22l9-11.8h-6.7L13.5 2z" '
           'fill="#F5B731"/></svg>')
BOLT_OFF = ('<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="{c}" stroke-width="1.7" '
            'stroke-linejoin="round" stroke-linecap="round"><path d="M13.5 2 4.5 13.6h6.6L10 22l9-11.8h-6.7L13.5 2z"/>'
            '<path d="M4 3.5 20 20.5"/></svg>')

CARD_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400&display=block');
body { margin: 0; background: #fff; }
.grid { display: grid; grid-template-columns: repeat(4, 104px); gap: 11px; padding: 22px 21px;
        width: max-content; background: #fff; font-family: Roboto, Arial, sans-serif; }
.tile { position: relative; height: 59px; border-radius: 8px; border: 1.5px solid #E6E6E6;
        box-sizing: border-box; overflow: hidden; background: #fff; }
.tile.off { background: NAVY; border-color: NAVY; }
.tile.maybe { background: #DCE3EE; border-color: #DCE3EE; }
.tile.now { border: 2px solid #1A8CFF; box-shadow: 0 0 0 1.5px #1A8CFF; }
.fill { position: absolute; top: 1.5px; bottom: 1.5px; background: NAVY; }
.fill.m { background: #C9D3E2; }
.fill.l { left: 1.5px; width: calc(50% - 1.5px); border-radius: 6px 0 0 6px; }
.fill.r { right: 1.5px; width: calc(50% - 1.5px); border-radius: 0 6px 6px 0; }
.label { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
         gap: 4px; font-size: 15.5px; color: #333; letter-spacing: .2px; }
.tile.off .label { color: #fff; }
.tile.maybe .label { color: NAVY; }
.label.w { color: #fff; }
.tile.gray { background: #E7E7E9; border-color: #E7E7E9; }
""".replace("NAVY", NAVY)


def tile_html(hour, value, now=False):
    v = str(value).lower()
    hh = f"{hour:02d}:00"
    cls = "tile" + (" now" if now else "")
    if v == "gray":
        return f'<div class="{cls} gray"><div class="label">{hh}</div></div>'
    if v == "no":
        return f'<div class="{cls} off"><div class="label">{BOLT_OFF.format(c="#fff")}{hh}</div></div>'
    if v == "maybe":
        return f'<div class="{cls} maybe"><div class="label">{BOLT_OFF.format(c=NAVY)}{hh}</div></div>'
    if v in ("first", "second", "mfirst", "msecond"):
        side = "l" if v in ("first", "mfirst") else "r"
        m = " m" if v.startswith("m") else ""
        clip = "inset(0 50% 0 0)" if side == "l" else "inset(0 0 0 50%)"
        white = "" if m else f'<div class="label w" style="clip-path:{clip}">{hh}</div>'
        return (f'<div class="{cls}"><div class="fill {side}{m}"></div>'
                f'<div class="label">{hh}</div>{white}</div>')
    return f'<div class="{cls}"><div class="label">{BOLT_ON}{hh}</div></div>'


def card_html(day, now_hour=None):
    tiles = "".join(tile_html(h - 1, day.get(str(h), "yes"), now=(h - 1 == now_hour)) for h in range(1, 25))
    return f'<html><head><meta charset="utf-8"><style>{CARD_CSS}</style></head><body><div class="grid">{tiles}</div></body></html>'


def render_cards(cards):
    """cards: [(day, now_hour)] -> [png bytes]"""
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(device_scale_factor=2)
        for day, now_hour in cards:
            page.set_content(card_html(day, now_hour), wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            out.append(page.locator(".grid").screenshot())
        browser.close()
    return out


# ---------- экстренные отключения ----------

GRAY_DAY = {str(h): "gray" for h in range(1, 25)}


def has_outage(info):
    return bool(info.get("type") or info.get("sub_type") or info.get("start_date"))


def is_emergency(info):
    if "екстрен" in str(info.get("sub_type", "")).lower():
        return True
    flags = info.get("_flags") or {}
    # ДТЭК скрыл таблицы графиков, а света по адресу нет
    return flags.get("showTableFact") is False and has_outage(info)


# ---------- предупреждения за полчаса ----------

WARN_OFF = "🍒 Дівульки, красотульки, за півгодинки може зникнути світло, готуємось."
WARN_ON = "🔆 Не сумуємо, чекаємо, за півгодинки обіцяли дати світло."
WARN_WINDOW_MIN = 40  # предупреждать, если до события осталось не больше стольких минут


def day_start(date):
    d = datetime.strptime(date, "%d.%m.%Y")
    return d.replace(tzinfo=KYIV) if KYIV else d


def upcoming_switches(fact, group, now_dt):
    """[(время, 'off' | 'on')] переходов по графику, до которых осталось 0..WARN_WINDOW_MIN минут."""
    seq = []
    for date, day_data in days_of(fact)[:2]:
        day = day_data.get(group)
        if not day:
            break
        start = day_start(date)
        for i, s in enumerate(half_hour_slots(day)):
            seq.append((start + timedelta(minutes=30 * i), s == "off"))
    out = []
    for (_, was_off), (when, is_off) in zip(seq, seq[1:]):
        left = (when - now_dt).total_seconds()
        if was_off != is_off and 0 < left <= WARN_WINDOW_MIN * 60:
            out.append((when, "off" if is_off else "on"))
    return out


# ---------- статус сейчас ----------

STATUS_ON = "✨ Кажуть, що світло вже є. Перевіряємо."

def status_text(info):
    if info.get("type") or info.get("sub_type") or info.get("start_date"):
        lines = ["🔴 <b>Світла немає</b>"]
        if info.get("sub_type"):
            lines.append("Причина: " + info["sub_type"])
        if info.get("start_date"):
            lines.append("Початок: " + info["start_date"])
        if info.get("end_date"):
            lines.append("Орієнтовне відновлення: до " + info["end_date"])
        return "\n".join(lines)
    return "🟢 <b>Світло є</b> (за даними ДТЕК)"


def status_key(info):
    return json.dumps([info.get("type"), info.get("sub_type"), info.get("start_date"),
                       info.get("end_date")], ensure_ascii=False)


# ---------- Telegram ----------

def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=30)
    if not r.ok:
        log(f"Ошибка Telegram: {r.text[:300]}")


def send_photo(png, caption):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                      data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                      files={"photo": ("grafik.png", png, "image/png")}, timeout=60)
    if not r.ok:
        log(f"Ошибка Telegram (фото): {r.text[:300]}")


# ---------- основной цикл ----------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check(test=False):
    fact, info = fetch_site()
    group = detect_group(info)
    state = load_state()
    head = f"📍 {STREET}, {HOUSE}"
    messages = []
    cards = []  # (day, now_hour, caption)

    sk = status_key(info)
    if test or state.get("status") != sk:
        messages.append(f"{head}\n{status_text(info)}" if has_outage(info) else STATUS_ON)
        state["status"] = sk

    emergency = is_emergency(info)
    days = days_of(fact)
    today_key = "sched_" + now().strftime("%d.%m.%Y")
    if emergency:
        if test or not state.get("emergency"):
            cards.append((GRAY_DAY, None, "⚠️ Діють екстрені відключення"))
    elif state.get("emergency"):
        state.pop(today_key, None)  # экстренные закончились: снова прислать обычный график на сегодня
    state["emergency"] = emergency

    if group:
        for n, (date, day_data) in enumerate(days[:2]):
            day = day_data.get(group)
            if not day:
                continue
            key = "sched_" + date
            if emergency and key == today_key:
                continue
            sig = json.dumps(day, sort_keys=True)
            if test or state.get(key) != sig:
                title = ("графік на сьогодні " if n == 0 else "графік на завтра ") + date
                if state.get(key) and not test:
                    title += " (оновлено)"
                cards.append((day, None, f"👋 Любі друзі,\n🔅 ось {title}"))
                state[key] = sig
    else:
        log("Группа графика не определена: поставь GROUP вручную в настройках.")

    warnings = []
    if group and not emergency:
        for when, kind in upcoming_switches(fact, group, now()):
            key = f"warn_{when:%Y%m%d%H%M}_{kind}"
            if state.get(key):
                continue
            state[key] = 1
            if kind == "off" and has_outage(info):
                continue  # света уже нет, предупреждать не о чем
            warnings.append(WARN_OFF if kind == "off" else WARN_ON)

    # чистим старые даты
    keep = {"sched_" + d for d, _ in days_of(fact)}
    state = {k: v for k, v in state.items() if not k.startswith("sched_") or k in keep}
    days_keep = {datetime.strptime(d, "%d.%m.%Y").strftime("%Y%m%d") for d, _ in days_of(fact)}
    state = {k: v for k, v in state.items() if not k.startswith("warn_") or k[5:13] in days_keep}
    state["errors"] = 0

    if test:
        print("\n--- ДАННЫЕ ДОМА ---\n", json.dumps(info, ensure_ascii=False, indent=2))
        print("Группа графика:", group)
        print("\n--- СООБЩЕНИЯ ---")
        for m in messages:
            print(m, "\n")
        for w in warnings:
            print("[предупреждение]", w, "\n")
        for c in cards:
            print("[картинка]", c[2], "\n")

    if messages:
        send("\n\n".join(messages) + (f"\n\n<i>Дані ДТЕК: {info['_updated']}</i>" if info.get("_updated") and has_outage(info) else ""))
    for w in warnings:
        send(w)
    if cards:
        for png, (_, _, caption) in zip(render_cards([(d, h) for d, h, _ in cards]), cards):
            send_photo(png, caption)
    if messages or cards or warnings:
        log(f"Отправлено: текст {len(messages)}, предупреждений {len(warnings)}, картинок {len(cards)}")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def main():
    if "--test" in sys.argv:
        check(test=True)
        return
    if "--once" in sys.argv:
        try:
            check()
        except Exception as e:
            log(f"Ошибка: {e}")
            state = load_state()
            state["errors"] = state.get("errors", 0) + 1
            if state["errors"] == 4:  # примерно час подряд не работает
                send("⚠️ Бот не може отримати дані з сайту ДТЕК.")
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return
    log("Бот запущен.")
    errors = 0
    while True:
        try:
            check()
            errors = 0
        except Exception as e:
            errors += 1
            log(f"Ошибка: {e}")
            if errors == 6:  # примерно час подряд не работает
                send("⚠️ Бот не може отримати дані з сайту ДТЕК. Перевірте dtek_bot.log")
        time.sleep(CHECK_EVERY_MIN * 60)


if __name__ == "__main__":
    main()
