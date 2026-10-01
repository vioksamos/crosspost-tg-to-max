"""
Кросспостинг: Telegram-канал -> группа VK + канал MAX.
Запускается по расписанию (GitHub Actions), забирает новые посты канала
через Telegram-бота и публикует их в VK и MAX.

Режимы:
  python bot.py run    — переслать новые посты (по умолчанию)
  python bot.py chats  — показать каналы MAX, где бот администратор (чтобы узнать MAX_CHAT_ID)
"""
import collections
import html
import os
import sys
import time

import requests

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHANNEL = os.environ.get("TG_CHANNEL", "").strip()      # необязательно: @username или id канала
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip()    # число без минуса
MAX_TOKEN = os.environ.get("MAX_TOKEN", "").strip()
MAX_CHAT_ID = os.environ.get("MAX_CHAT_ID", "").strip()

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TG_TOKEN}"
VK_API = "https://api.vk.com/method"
VK_V = "5.199"
MAX_API = os.environ.get("MAX_API", "").strip() or "https://platform-api.max.ru"

TG_MAX_FILE = 20 * 1024 * 1024  # лимит Telegram на скачивание файлов ботом


# ---------- Telegram ----------

def tg(method, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=60).json()
    if not r.get("ok"):
        raise RuntimeError(f"Telegram {method}: {r}")
    return r["result"]


def tg_download(file_id):
    f = tg("getFile", file_id=file_id)
    if f.get("file_size", 0) > TG_MAX_FILE:
        raise RuntimeError("файл больше 20 МБ, Telegram не даёт боту его скачать")
    data = requests.get(f"{TG_FILE}/{f['file_path']}", timeout=180).content
    return os.path.basename(f["file_path"]), data


# ---------- Форматирование текста ----------

TAGS = {"bold": "b", "italic": "i", "underline": "u",
        "strikethrough": "s", "code": "code", "pre": "pre"}


def render(text, entities, mode):
    """mode='html' для MAX (жирный, курсив, ссылки), mode='plain' для VK
    (VK не понимает разметку, ссылки дописываются в скобках)."""
    if not text:
        return ""
    opens = collections.defaultdict(list)
    closes = collections.defaultdict(list)
    for e in entities or []:
        t, start, end = e["type"], e["offset"], e["offset"] + e["length"]
        if mode == "html":
            if t in TAGS:
                o, c = f"<{TAGS[t]}>", f"</{TAGS[t]}>"
            elif t == "text_link":
                o, c = f'<a href="{html.escape(e["url"], quote=True)}">', "</a>"
            else:
                continue
        else:
            if t == "text_link":
                o, c = "", f" ({e['url']})"
            else:
                continue
        opens[start].append(o)
        closes[end].insert(0, c)

    out, pos = [], 0  # pos в единицах UTF-16, как считает Telegram
    for ch in text:
        out.extend(closes.pop(pos, []))
        out.extend(opens.pop(pos, []))
        out.append(html.escape(ch, quote=False) if mode == "html" else ch)
        pos += 2 if ord(ch) > 0xFFFF else 1
    out.extend(closes.pop(pos, []))
    return "".join(out)


# ---------- VK ----------

def vk(method, **params):
    params.update(access_token=VK_TOKEN, v=VK_V)
    r = requests.post(f"{VK_API}/{method}", data=params, timeout=60).json()
    if "error" in r:
        raise RuntimeError(f"VK {method}: {r['error'].get('error_msg')} ({r['error'].get('error_code')})")
    return r["response"]


def vk_photo(name, data):
    srv = vk("photos.getWallUploadServer", group_id=VK_GROUP_ID)
    up = requests.post(srv["upload_url"], files={"photo": (name, data)}, timeout=180).json()
    saved = vk("photos.saveWallPhoto", group_id=VK_GROUP_ID,
               photo=up["photo"], server=up["server"], hash=up["hash"])[0]
    return f"photo{saved['owner_id']}_{saved['id']}"


def vk_video(name, data):
    srv = vk("video.save", group_id=VK_GROUP_ID, name="Видео", wallpost=0)
    requests.post(srv["upload_url"], files={"video_file": (name, data)}, timeout=600).raise_for_status()
    return f"video{srv['owner_id']}_{srv['video_id']}"


def send_vk(post, files):
    attachments = []
    for kind, name, data in files:
        try:
            attachments.append(vk_photo(name, data) if kind == "photo" else vk_video(name, data))
        except Exception as e:
            print(f"  VK: не удалось загрузить {kind}: {e}")
    text = render(post["text"], post["entities"], "plain")
    if not text and not attachments:
        print("  VK: нечего публиковать")
        return
    vk("wall.post", owner_id=f"-{VK_GROUP_ID}", from_group=1,
       message=text, attachments=",".join(attachments))
    print("  VK: опубликовано")


# ---------- MAX ----------

def max_req(method, path, **kw):
    r = requests.request(method, f"{MAX_API}{path}",
                         headers={"Authorization": MAX_TOKEN}, timeout=180, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"MAX {path}: {r.status_code} {r.text}")
    return r.json()


def max_upload(kind, name, data):
    max_kind = "image" if kind == "photo" else "video"
    info = max_req("POST", "/uploads", params={"type": max_kind})
    resp = requests.post(info["url"], files={"data": (name, data)}, timeout=600)
    resp.raise_for_status()
    if max_kind == "image":
        return {"type": "image", "payload": resp.json()}
    return {"type": "video", "payload": {"token": info["token"]}}


def send_max(post, files):
    attachments = []
    for kind, name, data in files:
        try:
            attachments.append(max_upload(kind, name, data))
        except Exception as e:
            print(f"  MAX: не удалось загрузить {kind}: {e}")
    text = render(post["text"], post["entities"], "html")
    if not text and not attachments:
        print("  MAX: нечего публиковать")
        return
    body = {"format": "html", "attachments": attachments}
    if text:
        body["text"] = text
    for attempt in range(8):  # видео в MAX обрабатывается не сразу
        try:
            max_req("POST", "/messages", params={"chat_id": MAX_CHAT_ID}, json=body)
            print("  MAX: опубликовано")
            return
        except RuntimeError as e:
            if "not.ready" in str(e) and attempt < 7:
                time.sleep(10)
                continue
            raise


def find_chats(obj, found):
    """Ищет все chat_id (и названия чатов) в ответе MAX."""
    if isinstance(obj, dict):
        cid = obj.get("chat_id")
        if cid is not None:
            found.setdefault(cid, obj.get("title") or "")
        for v in obj.values():
            find_chats(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_chats(v, found)


def max_list_chats():
    """MAX больше не отдаёт список каналов бота, поэтому ловим события:
    пока этот режим работает (~4 минуты), опубликуйте любой пост в канале MAX."""
    print("Жду события от MAX около 4 минут.")
    print("Сейчас опубликуйте любое сообщение в своём канале MAX (потом его можно удалить).")
    found, marker, deadline = {}, None, time.time() + 240
    while time.time() < deadline:
        params = {"timeout": 30, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        data = max_req("GET", "/updates", params=params)
        marker = data.get("marker", marker)
        find_chats(data.get("updates", []), found)
        if found:
            break
    if not found:
        print("Событий не пришло. Проверьте, что бот — администратор канала, и запустите ещё раз.")
    for cid, title in found.items():
        print(f"MAX_CHAT_ID = {cid}   {title}")


# ---------- Основной цикл ----------

def is_my_channel(chat):
    if not TG_CHANNEL:
        return True
    want = TG_CHANNEL.lstrip("@").lower()
    return want in (str(chat.get("id")), (chat.get("username") or "").lower())


def collect_posts(updates):
    posts, albums = [], {}
    for u in updates:
        m = u.get("channel_post")
        if not m or not is_my_channel(m["chat"]):
            continue
        gid = m.get("media_group_id")
        if gid and gid in albums:
            post = albums[gid]
        else:
            post = {"text": "", "entities": [], "media": []}
            posts.append(post)
            if gid:
                albums[gid] = post
        text = m.get("text") or m.get("caption")
        if text:
            post["text"] = text
            post["entities"] = m.get("entities") or m.get("caption_entities") or []
        if "photo" in m:
            post["media"].append(("photo", m["photo"][-1]["file_id"]))
        elif "video_note" in m:
            post["media"].append(("video", m["video_note"]["file_id"]))
        elif "video" in m:
            post["media"].append(("video", m["video"]["file_id"]))
    return posts


def run():
    updates = tg("getUpdates", timeout=0, limit=100, allowed_updates=["channel_post"])
    if not updates:
        print("Новых постов нет.")
        return 0
    posts = collect_posts(updates)
    print(f"Новых постов: {len(posts)}")
    failed = False
    for i, post in enumerate(posts, 1):
        print(f"Пост {i}: {post['text'][:60]!r}")
        files = []
        for kind, file_id in post["media"]:
            try:
                name, data = tg_download(file_id)
                files.append((kind, name, data))
            except Exception as e:
                print(f"  Telegram: не удалось скачать {kind}: {e}")
                failed = True
        for name, enabled, sender in (("VK", VK_TOKEN and VK_GROUP_ID, send_vk),
                                      ("MAX", MAX_TOKEN and MAX_CHAT_ID, send_max)):
            if not enabled:
                continue
            try:
                sender(post, files)
            except Exception as e:
                print(f"  {name}: ОШИБКА {e}")
                failed = True
    # подтверждаем Telegram, что эти посты обработаны, чтобы не было повторов
    tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0, limit=1)
    return 1 if failed else 0  # при ошибке GitHub пришлёт письмо


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "chats":
        max_list_chats()
    else:
        sys.exit(run())
