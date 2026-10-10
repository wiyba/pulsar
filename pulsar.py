import asyncio
import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import re
import struct
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urljoin, urlparse

import httpx

CONFIG = json.load(open(sys.argv[1]))
STATE = os.environ.get("STATE_DIRECTORY", ".") + "/state.json"
LINKS = os.environ.get("LINKS", "http://127.0.0.1:8891/links")
PULSE = "https://pulse.mirea.ru"
LINK = re.compile(r"https://pulse\.mirea\.ru/selfapprove\?token=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
UA = "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
GRPC = {
    "Content-Type": "application/grpc-web+proto",
    "x-grpc-web": "1",
    "pulse-app-type": "pulse-app",
    "pulse-app-version": "1.9.2+10272",
}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
ATTEND = {1: "✗", 2: "✓", 3: "~"}
MARKS = {"Удовлетворительно": "3", "Хорошо": "4", "Отлично": "5"}
REASONS = {
    0: "неизвестная ошибка",
    1: "неверный или протухший токен",
    2: "ожидание подтверждения",
    3: "не записан на занятие",
    4: "журнал в архиве",
    5: "не прошёл турникет",
    6: "уважительное отсутствие",
    7: "нарушение распорядка",
}
COOLDOWN = {0: 60, 1: 0, 2: 120, 3: 120, 4: 120, 5: 60, 6: 120, 7: 120}

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
log = logging.getLogger("pulsar")

users = CONFIG["users"]
chats = {int(user["telegram"]): name for name, user in users.items()}
state = {"sessions": {}, "marks": {}, "cooldown": {}, "creds": {}, "last": "", "updated": 0, "pin": {"chat": None, "thread": None, "message": None}}
if os.path.exists(STATE):
    state |= json.load(open(STATE))
pending = {}
notified = {}
changed = asyncio.Event()
tg = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{CONFIG['token']}", timeout=70)


class Failed(Exception):
    pass


def save():
    with open(STATE, "w") as f:
        json.dump(state, f)


def keyboard(name):
    marks = "отметки: вкл" if state["marks"].get(name) else "отметки: выкл"
    return {"keyboard": [["расписание", "проходы", "брс"], [marks, "логин"]], "resize_keyboard": True, "is_persistent": True}


async def send(chat, text):
    await tg.post("/sendMessage", json={"chat_id": chat, "text": text, "reply_markup": keyboard(chats[chat]) if chat in chats else None})


async def api(method, **params):
    body = (await tg.post(f"/{method}", json=params)).json()
    if not body["ok"]:
        raise Failed(body["description"])
    return body["result"]


async def updates():
    offset = 0
    while True:
        try:
            res = await tg.get("/getUpdates", params={"offset": offset, "timeout": 50})
            for update in res.json()["result"]:
                offset = update["update_id"] + 1
                yield update
        except httpx.HTTPError as e:
            log.warning("telegram: %s", e)
            await asyncio.sleep(5)


def varint(n):
    out = bytearray()
    while True:
        out.append(n & 0x7F | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)


def enc(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def decode(data):
    fields = []
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 2:
            length, pos = read_varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        else:
            size = {1: 8, 5: 4}[wire]
            value, pos = data[pos:pos + size], pos + size
        fields.append((number, value))
    return fields


def read_varint(data, pos):
    result = shift = 0
    while True:
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        shift += 7
        if not byte & 0x80:
            return result, pos


def get(message, number):
    return [value for n, value in decode(message) if n == number]


def first(message, number, default=None):
    values = get(message, number)
    return values[0] if values else default


def text(message, number):
    return first(message, number, b"").decode()


def double(message, number):
    return struct.unpack("<d", first(message, number, bytes(8)))[0]


def seconds(message, number):
    return first(first(message, number, b""), 1, 0)


def client(cookies=None):
    jar = httpx.Cookies()
    for name, value, domain, path in cookies or []:
        jar.set(name, value, domain=domain, path=path)
    return httpx.AsyncClient(headers={"User-Agent": UA}, cookies=jar, timeout=30, follow_redirects=True)


def cookies_of(http):
    return [[c.name, c.value, c.domain, c.path] for c in http.cookies.jar]


async def rpc(http, service, method, payload=b""):
    res = await http.post(f"{PULSE}/rtu_tc.{service}/{method}", content=b"\x00" + len(payload).to_bytes(4, "big") + payload, headers=GRPC)
    if res.status_code != 200 or "grpc" not in res.headers.get("content-type", ""):
        raise Failed(f"сессия истекла ({res.status_code})")
    status, message, body, pos = res.headers.get("grpc-status"), res.headers.get("grpc-message", ""), b"", 0
    while pos + 5 <= len(res.content):
        flag, length = res.content[pos], int.from_bytes(res.content[pos + 1:pos + 5], "big")
        frame = res.content[pos + 5:pos + 5 + length]
        pos += 5 + length
        if flag & 0x80:
            trailers = dict(line.split(": ", 1) for line in frame.decode().splitlines() if ": " in line)
            status, message = trailers.get("grpc-status", status), trailers.get("grpc-message", message)
        else:
            body += frame
    if status not in (None, "0"):
        raise Failed(f"{method}: {unquote(message)}")
    return body


def mail_code(imap, since):
    box = imaplib.IMAP4_SSL(imap["host"])
    box.login(imap["user"], imap["password"])
    try:
        for folder in imap["folders"]:
            box.select(folder)
            for num in box.search(None, "UNSEEN")[1][0].split():
                message = email.message_from_bytes(box.fetch(num, "(RFC822)")[1][0][1])
                if "mirea" not in (message["From"] or ""):
                    continue
                subject = str(email.header.make_header(email.header.decode_header(message["Subject"] or "")))
                sent = email.utils.parsedate_to_datetime(message["Date"]).timestamp()
                found = re.search(r"\b\d{6}\b", subject)
                log.info("mail %s: %r, code %s", folder, subject, found and found.group())
                if sent >= since - 60 and found:
                    box.store(num, "+FLAGS", "\\Seen")
                    return found.group()
    finally:
        box.logout()


async def ask_mail(imap):
    since = time.time()
    for _ in range(36):
        code = await asyncio.to_thread(mail_code, imap, since)
        if code:
            return code
        await asyncio.sleep(5)
    raise Failed("код не пришёл на почту")


async def ask_chat(chat, prompt, secret=False):
    await send(chat, prompt)
    pending[chat] = asyncio.get_running_loop().create_future()
    try:
        message = await asyncio.wait_for(pending[chat], 600)
    finally:
        del pending[chat]
    if secret:
        await tg.post("/deleteMessage", json={"chat_id": chat, "message_id": message["message_id"]})
    return (message.get("text") or "").strip()


def kc(html, key):
    found = re.search(r'"' + key + r'"\s*:\s*"([^"]+)"', html)
    return found and found.group(1).replace("\\u0026", "&").replace("\\/", "/")


async def submit(http, page, data):
    action = kc(page.text, "loginAction")
    if not action:
        raise Failed(kc(page.text, "message") or "нет формы входа")
    res = await http.post(action, data=data, headers={"Referer": str(page.url)}, follow_redirects=False)
    while res.status_code in (302, 303):
        url = urljoin(str(res.url), res.headers["location"])
        if urlparse(url).hostname != "sso.mirea.ru":
            await http.get(url)
            return None
        res = await http.get(url, follow_redirects=False)
    if res.status_code != 200:
        raise Failed(f"sso {res.status_code}")
    return res


async def login(name):
    chat = int(users[name]["telegram"])
    user = users[name] | state["creds"].get(name, {})
    if "password" not in user:
        user["login"] = await ask_chat(chat, "логин")
        user["password"] = await ask_chat(chat, "пароль", secret=True)
        state["creds"][name] = {"login": user["login"], "password": user["password"]}
        save()
    async with client(state["sessions"].get(name)) as http:
        page = await http.get(PULSE + "/api/auth/login", params={"redirectUri": "/"})
        page = await submit(http, page, {"username": user["login"], "password": user["password"]}) if page.url.host == "sso.mirea.ru" else None
        for _ in range(5):
            if page is None:
                break
            kind = kc(page.text, "pageId")
            log.info("sso %s %s", name, kind)
            if "required-action" in str(page.url):
                page = await submit(http, page, {"skip": "true"})
            elif kind == "email-code-form":
                code = await (ask_mail(user["imap"]) if "imap" in user else ask_chat(chat, "код из письма"))
                page = await submit(http, page, {"emailCode": code})
            else:
                state["creds"].pop(name, None)
                raise Failed(kc(page.text, "message") or f"страница входа {kind}")
        cookies = cookies_of(http)
    if not any(c[0] == "Pulse.Auth.Cookie" for c in cookies):
        raise Failed("вход не дал Pulse.Auth.Cookie")
    log.info("login %s: %s", name, sorted(c[0] for c in cookies))
    state["sessions"][name] = cookies
    save()


async def call(name, fn):
    if name not in state["sessions"]:
        await login(name)
    for attempt in range(2):
        async with client(state["sessions"][name]) as http:
            try:
                return await fn(http)
            except Failed as e:
                if attempt or "истекла" not in str(e):
                    raise
            finally:
                state["sessions"][name] = cookies_of(http)
                save()
        await login(name)


async def student(http):
    membership = get(await rpc(http, "attendance.api.VisitingLogService", "GetAvailableVisitingLogsOfStudent"), 2)[-1]
    return text(first(membership, 1, b""), 2), text(first(first(membership, 1, b""), 1, b""), 1)


async def schedule(http):
    lines = []
    for offset in range(7):
        day = date.today() + timedelta(days=offset)
        payload = enc(2, enc(1, day.year) + enc(2, day.month) + enc(3, day.day))
        infos = get(await rpc(http, "attendance.api.LessonService", "GetAvailableLessonsOfVisitingLogs", payload), 2)
        if infos:
            lines.append(f"\n{WEEKDAYS[day.weekday()]} {day:%d.%m}")
        for info in sorted(infos, key=lambda info: seconds(first(info, 3, b""), 2)):
            lesson = first(info, 3, b"")
            start = datetime.fromtimestamp(seconds(lesson, 2))
            kind = next((number for number, _ in decode(first(info, 4, b""))), 0)
            mark = ATTEND.get(kind, "") if start < datetime.now() else ""
            rooms = ", ".join(text(room, 2) for room in get(lesson, 6))
            lines.append(" ".join(filter(None, [f"{start:%H:%M}", mark, text(first(lesson, 5, b""), 2), text(first(lesson, 4, b""), 2), rooms])))
    return "\n".join(lines).strip() or "пар нет"


async def passes(http):
    _, human = await student(http)
    start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
    payload = enc(1, human) + enc(2, enc(1, enc(1, start)) + enc(2, enc(1, start + 86400))) + enc(3, 1) + enc(5, 2)
    rows = get(await rpc(http, "rtu_attend.humanpass.HumanPassService", "GetHumanAcsEvents", payload), 1)
    lines = []
    for row in sorted(rows, key=lambda row: seconds(row, 2)):
        when = datetime.fromtimestamp(seconds(row, 2))
        lines.append(f"{when:%H:%M} {text(first(row, 4, b''), 2)} -> {text(first(row, 3, b''), 2)}")
    return "\n".join(lines) or "проходов сегодня нет"


async def grades(http):
    student_id, _ = await student(http)
    plans = get(await rpc(http, "student.api.StudentService", "GetStudentTermPlansWithDisciplines", enc(1, student_id)), 1)
    ids = dict.fromkeys(text(first(entry, 1, b""), 1) for plan in plans for entry in get(plan, 2) + get(plan, 3))
    payload = b"".join(enc(1, i) for i in ids) + enc(2, student_id)
    lines = []
    for report in get(await rpc(http, "attendance.api.LearnRatingScoreService", "GetAttendancesAndLRSByDisciplinesIds", payload), 1):
        if not get(report, 3):
            continue
        part = get(report, 3)[-1]
        attendance = first(first(part, 2, b""), 2, b"")
        lrs = first(part, 4, b"")
        if not lrs:
            lines.append(f"{text(report, 2)}: посещения {first(attendance, 2, 0)} из {first(attendance, 4, 0)}")
            continue
        summary = double(first(lrs, 4, b""), 2)
        borders = sorted((first(b, 2, 0), MARKS.get(text(b, 3), text(b, 3).lower())) for b in get(lrs, 3))
        ahead = [(score, mark) for score, mark in borders if score > summary]
        goal = f"до «{ahead[0][1]}» ещё {ahead[0][0] - summary:g}" if ahead else str([mark for score, mark in borders if score <= summary][-1])
        lines.append(f"{text(report, 2)}: {summary:g} из {first(lrs, 5, 0)}, {goal}, +{double(lrs, 7):g} за посещение")
    return "\n".join(sorted(lines)) or "дисциплин нет"


async def approve(http, token):
    res = await rpc(http, "attendance.api.AttendanceService", "SelfApproveAttendanceThroughQRCode", enc(1, token))
    if first(res, 2) is not None:
        return None
    return first(first(res, 1, b""), 1, 0)


HANDLERS = {"расписание": schedule, "проходы": passes, "брс": grades}


async def handle(name, chat, message):
    if chat in pending:
        pending[chat].set_result(message)
        return
    command = (message.get("text") or "").strip().lower().lstrip("/")
    try:
        if command.startswith("отметки"):
            state["marks"][name] = not state["marks"].get(name)
            save()
            reply = "отметки включены" if state["marks"][name] else "отметки выключены"
        elif command == "логин":
            await login(name)
            reply = "вошёл"
        elif command in HANDLERS:
            reply = await call(name, HANDLERS[command])
        elif command.startswith("setup"):
            reply = await setup(*map(int, command.split()[1:]))
        else:
            reply = "кнопки снизу"
    except Exception as e:
        log.exception("%s %s", name, command)
        reply = f"ошибка: {e}"
    await send(chat, reply)


async def mark(name, token):
    if time.time() < state["cooldown"].get(name, 0):
        return
    state["cooldown"][name] = time.time() + 60
    try:
        reason = await call(name, lambda http: approve(http, token))
        result = "✓ отмечен" if reason is None else f"✗ {REASONS.get(reason, reason)}"
        cooldown = 120 if reason is None else COOLDOWN.get(reason, 60)
    except Exception as e:
        log.exception("mark %s", name)
        result, cooldown = f"✗ {e}", 60
    state["cooldown"][name] = time.time() + cooldown
    save()
    log.info("mark %s %s: %s", name, token, result)
    if result.startswith("✓") or notified.get(name) != result:
        await send(int(users[name]["telegram"]), result)
    notified[name] = result


async def links(group):
    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as http, http.stream("GET", LINKS) as res:
                async for line in res.aiter_lines():
                    found = LINK.fullmatch(line.removeprefix("data: "))
                    if not found or found.group() == state["last"]:
                        continue
                    state["last"] = found.group()
                    state["updated"] = int(time.time())
                    save()
                    changed.set()
                    for name in users:
                        if state["marks"].get(name):
                            group.create_task(mark(name, found.group(1)))
        except httpx.HTTPError as e:
            log.warning("links: %s", e)
        await asyncio.sleep(1)


def render():
    text = "кода нет"
    if state["last"]:
        stamp = f'<tg-time unix="{state["updated"]}" format="r">только что</tg-time>'
        text = f'<a href="{state["last"]}">отметиться</a>\n\nобновлено {stamp}'
    return {"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}


async def publish():
    while True:
        await changed.wait()
        changed.clear()
        if not state["pin"]["message"]:
            continue
        try:
            res = await tg.post("/editMessageText", json={"chat_id": state["pin"]["chat"], "message_id": state["pin"]["message"]} | render())
        except httpx.HTTPError as e:
            log.warning("publish: %s", e)
            continue
        if res.status_code == 429:
            await asyncio.sleep(res.json()["parameters"]["retry_after"])
            changed.set()
        elif not res.is_success:
            log.warning("publish: %s", res.text)


async def setup(chat, thread=None):
    params = {"chat_id": chat} | render()
    if thread:
        params["message_thread_id"] = thread
    sent = await api("sendMessage", **params)
    state["pin"] = {"chat": chat, "thread": thread, "message": sent["message_id"]}
    save()
    await api("pinChatMessage", chat_id=chat, message_id=sent["message_id"], disable_notification=True)
    return "закрепил"


async def keepalive():
    while True:
        now = datetime.now()
        target = datetime.combine(now.date(), datetime.min.time()) + timedelta(hours=8)
        await asyncio.sleep(((target if target > now else target + timedelta(days=1)) - now).total_seconds())
        for name in list(state["sessions"]):
            try:
                await call(name, student)
            except Exception as e:
                log.exception("keepalive %s", name)
                await send(int(users[name]["telegram"]), f"✗ {e}")


async def main():
    async with asyncio.TaskGroup() as group:
        group.create_task(links(group))
        group.create_task(publish())
        group.create_task(keepalive())
        async for update in updates():
            message = update.get("message") or {}
            chat = message.get("chat", {}).get("id")
            if chat in chats:
                group.create_task(handle(chats[chat], chat, message))


if __name__ == "__main__":
    asyncio.run(main())
