#!/usr/bin/env python3
"""Local WeChat article renderer and resumable official publication client."""
from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/account.json"
OUTPUT = Path(os.environ.get("WECHAT_OUTPUT_ROOT", str(ROOT / "outputs")))
API = "https://api.weixin.qq.com/cgi-bin/"
VERSION = "1.1.0"


class PublisherError(Exception):
    pass


class UnknownResult(PublisherError):
    """A modifying API call may have succeeded; never blindly replay it."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def immutable_write(path, data):
    path = Path(path)
    if path.exists():
        if path.read_bytes() != data:
            raise PublisherError("同名成果内容不同，请使用新的任务版本：" + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def archive_json(path, value):
    """User-facing receipts are versioned; only internal journals are mutable."""
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, indent=2).encode()
    if path.exists() and path.read_bytes() != data:
        path = path.with_name(path.stem + "-v-" + digest(data)[:12] + path.suffix)
    immutable_write(path, data)
    return str(path)


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise PublisherError("同一任务正在执行，或上次异常中断留下锁。先检查进程再处理：" + str(path))
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def local_asset(base, name):
    if not isinstance(name, str) or not name or urllib.parse.urlsplit(name).scheme:
        raise PublisherError("图片必须是任务目录内的本地文件")
    asset = (base / name).resolve()
    if not asset.is_relative_to(base.resolve()) or not asset.is_file():
        raise PublisherError("素材路径越界或不存在：" + name)
    if asset.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise PublisherError("图片请使用 PNG 或 JPEG：" + name)
    from PIL import Image
    with Image.open(asset) as picture:
        if picture.width < 64 or picture.height < 64:
            raise PublisherError("图片尺寸过小：" + name)
        picture.verify()
    return asset


THEMES = {
    "editorial": {"accent": "#176456", "muted": "#f0f7f4", "text": "#25302b"},
    "warm": {"accent": "#986339", "muted": "#faf5ed", "text": "#39322e"},
    "tech": {"accent": "#235da8", "muted": "#eff5fc", "text": "#263244"},
}


class WechatHTML(HTMLParser):
    def __init__(self, base, theme):
        super().__init__(convert_charrefs=True)
        self.base, self.theme = base, THEMES[theme]
        self.output, self.images = [], []
        accent, muted, text = (self.theme[k] for k in ("accent", "muted", "text"))
        self.styles = {
            "p": "margin:0 0 18px;line-height:1.85;text-align:justify;",
            "h1": f"font-size:24px;line-height:1.5;color:{accent};margin:24px 0 18px;",
            "h2": f"font-size:20px;line-height:1.55;color:{accent};margin:30px 0 18px;border-bottom:2px solid {accent};padding-bottom:8px;",
            "h3": f"font-size:17px;line-height:1.65;color:{accent};margin:24px 0 12px;",
            "h4": f"font-size:16px;line-height:1.65;color:{accent};margin:20px 0 12px;",
            "blockquote": f"margin:20px 0;padding:16px 18px;background:{muted};border-left:3px solid {accent};color:{text};",
            "ul": "padding-left:24px;margin:12px 0 20px;",
            "ol": "padding-left:24px;margin:12px 0 20px;",
            "li": "line-height:1.85;margin-bottom:8px;",
            "strong": f"color:{accent};font-weight:700;",
            "em": "font-style:italic;",
            "a": f"color:{accent};text-decoration:underline;word-break:break-all;",
            "code": f"font-family:monospace;background:{muted};padding:2px 4px;word-break:break-all;",
            "pre": f"background:{muted};padding:14px;white-space:pre-wrap;word-break:break-all;line-height:1.6;",
            "table": "width:100%;border-collapse:collapse;margin:18px 0;font-size:14px;",
            "th": f"background:{muted};border:1px solid #d9e2df;padding:8px;text-align:left;",
            "td": "border:1px solid #d9e2df;padding:8px;line-height:1.7;",
            "hr": "border:0;border-top:1px solid #d9e2df;margin:26px 0;",
            "img": "display:block;width:100%;height:auto;margin:20px auto;border-radius:6px;",
        }
        self.allowed = set(self.styles) | {"br", "thead", "tbody", "tr", "sup", "sub", "del"}

    def handle_starttag(self, tag, attributes):
        if tag not in self.allowed:
            raise PublisherError("正文包含不支持的 HTML 标签：" + tag)
        attrs = dict(attributes)
        kept = {"style": self.styles[tag]} if tag in self.styles else {}
        if tag == "img":
            asset = local_asset(self.base, attrs.get("src"))
            image_id = "asset-" + digest(asset.read_bytes())[:24]
            if not any(i["id"] == image_id for i in self.images):
                self.images.append({"id": image_id, "source": str(asset), "sha256": digest(asset.read_bytes())})
            kept.update(src=image_id, alt=attrs.get("alt", ""))
        if tag == "a":
            url = attrs.get("href", "")
            if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
                raise PublisherError("链接仅支持 http/https")
            kept["href"] = url
        encoded = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in kept.items())
        self.output.append(f"<{tag}{encoded}>")

    def handle_endtag(self, tag):
        if tag not in self.allowed:
            raise PublisherError("正文包含不支持的 HTML 标签：" + tag)
        if tag not in {"img", "br", "hr"}:
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        self.output.append(html.escape(data))

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)

    def handle_comment(self, data):
        pass


def build(article_path, output):
    import markdown
    article_path = Path(article_path).resolve()
    article = read_json(article_path)
    base = article_path.parent
    for field, maximum in [("title", 64), ("author", 16), ("digest", 120)]:
        value = article.get(field, "")
        if not isinstance(value, str) or len(value) > maximum:
            raise PublisherError(f"{field} 必须为文本，最多 {maximum} 字")
    if not article.get("title", "").strip():
        raise PublisherError("缺少标题")
    theme = article.get("theme", "editorial")
    if theme not in THEMES:
        raise PublisherError("未知排版主题")
    markdown_path = (base / article.get("markdown", "article.md")).resolve()
    if not markdown_path.is_relative_to(base) or not markdown_path.is_file():
        raise PublisherError("Markdown 路径不存在或越界")
    source = markdown_path.read_text(encoding="utf-8")
    if not source.strip():
        raise PublisherError("正文为空")
    cover = local_asset(base, article.get("cover"))
    renderer = WechatHTML(base, theme)
    renderer.feed(markdown.markdown(source, extensions=["tables", "fenced_code", "sane_lists"]))
    renderer.close()
    body = '<section style="font-size:16px;line-height:1.85;color:' + THEMES[theme]["text"] + ';padding:8px 4px;word-wrap:break-word;">' + "".join(renderer.output) + "</section>"
    validate_body(body)
    if not renderer.images and not article.get("allow_no_images", False):
        raise PublisherError("正文没有插图；确需纯文字时设置 allow_no_images=true")
    normalized = {key: article.get(key, "") for key in ["title", "author", "digest", "content_source_url"]}
    source_url = normalized["content_source_url"]
    if source_url and urllib.parse.urlsplit(source_url).scheme not in {"http", "https"}:
        raise PublisherError("阅读原文链接仅支持 http/https")
    for key in ["need_open_comment", "only_fans_can_comment"]:
        value = article.get(key, 1 if key == "need_open_comment" else 0)
        if value not in (0, 1):
            raise PublisherError(key + " 必须是 0 或 1")
        normalized[key] = value
    normalized["request_original_declaration"] = bool(article.get("request_original_declaration", False))
    normalized["collection"] = article.get("collection", "")
    if not isinstance(normalized["collection"], str) or len(normalized["collection"]) > 64:
        raise PublisherError("collection 必须是 64 字以内的合集名称")
    # Crop coordinates leave the original pixels intact; keep central subject safe.
    from PIL import Image
    with Image.open(cover) as picture:
        w, h = picture.size
    if w >= 2.35 * h:
        margin = (1 - 2.35 * h / w) / 2
        crop = (margin, 0, 1 - margin, 1)
    else:
        margin = (1 - w / (2.35 * h)) / 2
        crop = (0, margin, 1, 1 - margin)
    normalized["pic_crop_235_1"] = "_".join(f"{v:.6f}" for v in crop)
    square_x, square_y = min(w, h) / w, min(w, h) / h
    normalized["pic_crop_1_1"] = "_".join(f"{v:.6f}" for v in ((1-square_x)/2, (1-square_y)/2, (1+square_x)/2, (1+square_y)/2))
    cover_hash = digest(cover.read_bytes())
    fingerprint = digest(json.dumps({"article": normalized, "body": body, "cover": cover_hash}, sort_keys=True, ensure_ascii=False).encode())
    job = Path(output) / (re.sub(r"[^\w\-]+", "-", article["title"]).strip("-")[:35] + "-" + fingerprint[:16])
    images = []
    for item in renderer.images:
        original = Path(item["source"])
        relative = "图片/" + item["id"] + original.suffix.lower()
        immutable_write(job / relative, original.read_bytes())
        images.append({"id": item["id"], "path": relative, "sha256": item["sha256"]})
    cover_relative = "图片/cover-" + cover_hash[:16] + cover.suffix.lower()
    immutable_write(job / cover_relative, cover.read_bytes())
    immutable_write(job / "文章/article.md", source.encode())
    immutable_write(job / "文章/article.html", body.encode())
    manifest = {"version": VERSION, "fingerprint": fingerprint, "article": normalized, "html": "文章/article.html", "cover": {"path": cover_relative, "sha256": cover_hash}, "images": images}
    preview_body = body
    for item in images:
        asset = job / item["path"]
        mime = "image/png" if asset.suffix == ".png" else "image/jpeg"
        preview_body = preview_body.replace('src="' + item["id"] + '"', 'src="data:' + mime + ';base64,' + base64.b64encode(asset.read_bytes()).decode() + '"')
    preview = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(article["title"]) + '</title><body style="max-width:677px;margin:24px auto;padding:0 20px;background:#fff;"><h1 style="font-size:24px;line-height:1.5;">' + html.escape(article["title"]) + '</h1>' + preview_body + '</body></html>'
    immutable_write(job / "预览/公众号预览.html", preview.encode())
    immutable_write(job / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    return {"ok": True, "job": str(job), "preview": str(job / "预览/公众号预览.html"), "title": article["title"], "inline_images": len(images), "network_calls": 0}


def validate_body(body):
    if len(body) >= 20000 or len(body.encode()) >= 1_000_000:
        raise PublisherError("排版正文超过接口大小限制，请减少装饰或拆分文章")


ERROR_HINTS = {40164: "请将当前微信 API 出口 IP 加入公众号 IP 白名单", 48001: "公众号未授权该接口，请检查接口权限", 40001: "凭据或 access_token 无效", 40125: "AppSecret 无效", 40013: "AppID 无效", 45009: "接口调用额度已用完", 45028: "群发额度限制"}


class Client:
    def __init__(self, config):
        self.config, self.token = config, None

    def request(self, endpoint, payload=None, multipart=None, modifying=False, authenticated=True):
        params = {"access_token": self.access_token()} if authenticated else {}
        if endpoint == "material/add_material":
            params["type"] = "image"
        headers = {}
        if multipart:
            filename, content = multipart
            boundary = "wechat-" + uuid.uuid4().hex
            data = (f'--{boundary}\r\nContent-Disposition: form-data; name="media"; filename="{filename}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode() + content + f"\r\n--{boundary}--\r\n".encode())
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        elif payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
        else:
            data = None
        url = API + endpoint + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                result = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError):
            # Never include urllib exception text: it may contain tokens/secret URLs.
            error = UnknownResult if modifying else PublisherError
            raise error("微信接口网络/响应异常：" + endpoint + ("。结果未知，先检查后台，禁止自动重试。" if modifying else "，可重试只读查询。")) from None
        if not isinstance(result, dict):
            error = UnknownResult if modifying else PublisherError
            raise error("微信接口响应结构异常：" + endpoint)
        code = result.get("errcode", 0)
        if code:
            hint = ERROR_HINTS.get(code, "请在官方文档查询错误码")
            if code == 40164:
                address = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", str(result.get("errmsg", "")))
                if address:
                    hint += "；微信识别的当前 API 出口 IP：" + address.group(0)
            raise PublisherError(f"微信接口 {endpoint} 返回 {code}：" + hint)
        return result

    def access_token(self):
        if not self.token:
            result = self.request("stable_token", {"grant_type": "client_credential", "appid": self.config["app_id"], "secret": self.config["app_secret"], "force_refresh": False}, authenticated=False)
            if not result.get("access_token"):
                raise PublisherError("微信没有返回 access_token")
            self.token = result["access_token"]
        return self.token

    def upload(self, path, cover=False):
        from PIL import Image
        # Transport encoding only: keep source images and dimensions unchanged.
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                rgb = Image.new("RGB", image.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            for quality in [92, 85, 75, 60, 45]:
                encoded = io.BytesIO()
                rgb.save(encoded, format="JPEG", quality=quality, optimize=True)
                if len(encoded.getvalue()) < (9_500_000 if cover else 950_000):
                    break
            else:
                raise PublisherError("图片超过接口大小限制，请重新生成较小图片")
        endpoint = "material/add_material" if cover else "media/uploadimg"
        return self.request(endpoint, multipart=("image.jpg", encoded.getvalue()), modifying=True)


def load_config(path=CONFIG):
    if not Path(path).is_file():
        raise PublisherError("尚未连接公众号。请运行 configure，在本机终端填写 AppID / AppSecret。")
    config = read_json(path)
    for key in ["app_id", "app_secret"]:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise PublisherError("本地公众号配置缺少 " + key)
    return config


def checked_job(job):
    job = Path(job).resolve()
    manifest = read_json(job / "manifest.json")
    body_path = (job / manifest["html"]).resolve()
    if not body_path.is_relative_to(job):
        raise PublisherError("任务正文路径越界")
    body = body_path.read_text(encoding="utf-8")
    for item in [manifest["cover"]] + manifest["images"]:
        asset = (job / item["path"]).resolve()
        if not asset.is_relative_to(job) or digest(asset.read_bytes()) != item["sha256"]:
            raise PublisherError("图片发生变化或路径越界，请重新 build")
    actual = digest(json.dumps({"article": manifest["article"], "body": body, "cover": manifest["cover"]["sha256"]}, sort_keys=True, ensure_ascii=False).encode())
    if actual != manifest["fingerprint"]:
        raise PublisherError("任务内容发生变化，请重新 build")
    return manifest, body


def journal_path(config, manifest, state_root=None):
    account = digest(config["app_id"].encode())[:16]
    return Path(state_root or ROOT / "logs/publication") / account / (manifest["fingerprint"] + ".json")


def poll(client, state, state_path, wait=0):
    deadline = time.monotonic() + max(0, wait)
    while True:
        result = client.request("freepublish/get", {"publish_id": state["publish_id"]})
        code = result.get("publish_status")
        if code not in {0, 1, 2, 3, 4, 5, 6}:
            raise PublisherError("微信未返回有效发布状态；保留 publish_id，可稍后继续查询")
        state["publish_status"] = code
        state["updated_at"] = time.time()
        if code == 0:
            items = result.get("article_detail", {}).get("item", [])
            state.update(stage="published", article_id=result.get("article_id"), urls=[item["article_url"] for item in items if item.get("article_url")])
        elif code == 1:
            state["stage"] = "publishing"
        else:
            state.update(stage="publish_failed", fail_idx=result.get("fail_idx", []))
        write_json(state_path, state)
        if code != 1 or time.monotonic() >= deadline:
            return {"ok": code in (0, 1), "published": code == 0, "stage": state["stage"], "publish_status": code, "publish_id": state["publish_id"], "article_urls": state.get("urls", []), "title": state["title"]}
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def draft_result(state, config, requested_publish=False):
    actions = ["在微信编辑器完成原创声明，核对后保存"] if state.get("request_original_declaration") else []
    if state.get("collection"):
        actions.append("在微信编辑器选择合集：" + state["collection"])
    return {"ok": True, "published": False, "stage": "draft_ready", "delivery": "draft", "manual_publish_required": True, "draft_media_id": state["draft_media_id"], "title": state["title"], "account_name": config.get("account_name", ""), "backend_url": "https://mp.weixin.qq.com/", "message": "图文已保存到公众号草稿箱，请在后台预览后发表。", "publish_api_available": config.get("publish_api_available"), "publish_requested": requested_publish, "editor_actions": actions}


def api_article(manifest, body, cover_id, updating=False):
    article = {**manifest["article"], "article_type": "news", "content": body, "thumb_media_id": cover_id}
    article.pop("request_original_declaration", None)  # Not an official draft API field.
    article.pop("collection", None)  # Selected in the WeChat editor, not draft/update.
    if updating:
        crops = []
        for key, ratio in [("pic_crop_235_1", "2.35_1"), ("pic_crop_1_1", "1_1")]:
            values = article.pop(key).split("_")
            crops.append(dict(ratio=ratio, **dict(zip(["x1", "y1", "x2", "y2"], values))))
        article["cover_info"] = {"crop_percent_list": crops}
    return article


def visible_text(body):
    class Extract(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)
    parser = Extract()
    parser.feed(body)
    return re.sub(r"\s+", "", "".join(parser.parts))


def update_draft(job, previous_job, config, client=None, state_root=None):
    job, previous_job = Path(job).resolve(), Path(previous_job).resolve()
    manifest, body = checked_job(job)
    previous, previous_body = checked_job(previous_job)
    path = journal_path(config, manifest, state_root)
    old_path = journal_path(config, previous, state_root)
    if path == old_path:
        raise PublisherError("正文和设置未变化，请直接查询原草稿")
    client = client or Client(config)
    with contextlib.ExitStack() as stack:
        for journal in sorted([path, old_path]):
            stack.enter_context(locked(journal.with_suffix(".lock")))
        if not old_path.exists():
            raise PublisherError("旧任务没有已创建的草稿")
        old = read_json(old_path)
        state = read_json(path) if path.exists() else {"fingerprint": manifest["fingerprint"], "title": manifest["article"]["title"], "stage": "new", "images": dict(old.get("images", {})), "previous_job": str(previous_job)}
        if old.get("superseded_by") == str(job) and state.get("stage") == "draft_ready":
            return draft_result(state, config)
        if old.get("superseded_by") or state.get("superseded_by"):
            raise PublisherError("此草稿已有更新版本，请使用最新任务目录")
        if old.get("publish_id") or old.get("stage") not in {"draft_ready", "submit_rejected"} or not old.get("draft_media_id"):
            raise PublisherError("旧草稿尚未就绪或发布结果未知，不能更新")
        if state.get("stage") in {"draft_update_inflight", "draft_update_unknown"} or old.get("revision_pending"):
            raise PublisherError("上次草稿更新结果未知，请在后台核对；禁止自动重试")
        if state.get("draft_media_id") or state.get("publish_id"):
            raise PublisherError("新任务已创建独立草稿，不能绑定到旧草稿")
        remote = client.request("draft/get", {"media_id": old["draft_media_id"]})
        items = remote.get("news_item", [])
        if len(items) != 1 or items[0].get("title") != previous["article"]["title"] or visible_text(items[0].get("content", "")) != visible_text(previous_body):
            raise PublisherError("后台草稿正文已有改动，请先同步修改，避免覆盖")
        archive_json(job / "发布记录/更新前草稿.json", remote)
        for item in manifest["images"]:
            if item["id"] not in state["images"]:
                url = client.upload(job / item["path"]).get("url", "")
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in {"http", "https"} or parsed.hostname != "mmbiz.qpic.cn":
                    raise PublisherError("图片上传未返回微信 URL，停止更新")
                state["images"][item["id"]] = url
                write_json(path, state)
            body = body.replace('src="' + item["id"] + '"', 'src="' + html.escape(state["images"][item["id"]], quote=True) + '"')
        if not state.get("cover_media_id"):
            if manifest["cover"]["sha256"] == previous["cover"]["sha256"]:
                state["cover_media_id"] = old["cover_media_id"]
            else:
                state["cover_media_id"] = client.upload(job / manifest["cover"]["path"], cover=True).get("media_id")
        if not state.get("cover_media_id"):
            raise PublisherError("封面上传未返回 media_id，停止更新")
        validate_body(body)
        payload = {"media_id": old["draft_media_id"], "index": 0, "articles": api_article(manifest, body, state["cover_media_id"], updating=True)}
        archive_json(job / "发布记录/draft-update-payload.json", payload)
        state["stage"] = "draft_update_inflight"
        write_json(path, state)
        old["revision_pending"] = str(job)
        write_json(old_path, old)
        try:
            result = client.request("draft/update", payload, modifying=True)
            if result.get("errcode") != 0:
                raise UnknownResult("微信未返回草稿更新确认，请在后台核对")
        except UnknownResult:
            state["stage"] = "draft_update_unknown"
            write_json(path, state)
            raise
        except PublisherError:
            state["stage"] = "draft_update_rejected"
            write_json(path, state)
            old.pop("revision_pending", None)
            write_json(old_path, old)
            raise
        state.update(stage="draft_ready", draft_media_id=old["draft_media_id"], request_original_declaration=manifest["article"].get("request_original_declaration", False), collection=manifest["article"].get("collection", ""))
        write_json(path, state)
        old.pop("revision_pending", None)
        old["superseded_by"] = str(job)
        write_json(old_path, old)
        result = draft_result(state, config)
        result["receipt"] = archive_json(job / "发布记录/result.json", result)
        return result


def run(job, config, publish=False, wait=20, client=None, state_root=None, status_only=False):
    job = Path(job).resolve()
    manifest, body = checked_job(job)
    state_path = journal_path(config, manifest, state_root)
    client = client or Client(config)
    requested_publish = publish
    if config.get("publish_api_available") is False:
        publish = False
    with locked(state_path.with_suffix(".lock")):
        state = read_json(state_path) if state_path.exists() else {"fingerprint": manifest["fingerprint"], "title": manifest["article"]["title"], "stage": "new", "images": {}}
        if state.get("superseded_by"):
            raise PublisherError("此草稿已更新为新版本，请使用新任务目录：" + state["superseded_by"])
        if state.get("revision_pending"):
            raise PublisherError("草稿更新结果尚未确认，请在后台核对")
        if state.get("publish_id"):
            result = poll(client, state, state_path, wait)
            result["receipt"] = archive_json(job / "发布记录/result.json", result)
            return result
        if status_only:
            if state.get("draft_media_id") and state["stage"] in {"draft_ready", "submit_rejected"}:
                return draft_result(state, config)
            return {"ok": True, "published": False, "stage": state["stage"], "title": state["title"]}
        if state["stage"] in {"draft_inflight", "draft_unknown", "submit_inflight", "submit_unknown", "draft_update_inflight", "draft_update_unknown"}:
            raise PublisherError("上次写入结果未知。先检查公众号后台；使用 reconcile 绑定已存在的草稿/发布 ID，禁止直接重试。")
        for item in manifest["images"]:
            if item["id"] not in state["images"]:
                uploaded = client.upload(job / item["path"])
                url = uploaded.get("url", "")
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in {"http", "https"} or parsed.hostname != "mmbiz.qpic.cn":
                    raise PublisherError("正文上传没有返回微信图片 URL，已停止发布")
                state["images"][item["id"]] = url
                write_json(state_path, state)
            body = body.replace('src="' + item["id"] + '"', 'src="' + html.escape(state["images"][item["id"]], quote=True) + '"')
        if not state.get("cover_media_id"):
            uploaded = client.upload(job / manifest["cover"]["path"], cover=True)
            if not uploaded.get("media_id"):
                raise PublisherError("封面上传没有返回永久 media_id，已停止发布")
            state["cover_media_id"] = uploaded["media_id"]
            write_json(state_path, state)
        validate_body(body)
        if not state.get("draft_media_id"):
            article = api_article(manifest, body, state["cover_media_id"])
            archive_json(job / "发布记录/draft-payload.json", {"articles": [article]})
            state["stage"] = "draft_inflight"
            write_json(state_path, state)
            try:
                result = client.request("draft/add", {"articles": [article]}, modifying=True)
                if not result.get("media_id"):
                    raise UnknownResult("微信未返回草稿 ID；请先检查草稿箱")
            except UnknownResult:
                state["stage"] = "draft_unknown"
                write_json(state_path, state)
                raise
            except PublisherError:
                state["stage"] = "draft_rejected"
                write_json(state_path, state)
                raise
            state.update(stage="draft_ready", draft_media_id=result["media_id"], request_original_declaration=manifest["article"].get("request_original_declaration", False), collection=manifest["article"].get("collection", ""))
            write_json(state_path, state)
        if not publish:
            result = draft_result(state, config, requested_publish)
            result["receipt"] = archive_json(job / "发布记录/result.json", result)
            return result
        state["stage"] = "submit_inflight"
        write_json(state_path, state)
        try:
            result = client.request("freepublish/submit", {"media_id": state["draft_media_id"]}, modifying=True)
            if not result.get("publish_id"):
                raise UnknownResult("微信未返回发布 ID；请先检查公众号后台")
        except UnknownResult:
            state["stage"] = "submit_unknown"
            write_json(state_path, state)
            raise
        except PublisherError:
            state["stage"] = "submit_rejected"
            write_json(state_path, state)
            raise
        state.update(stage="publishing", publish_id=result["publish_id"])
        write_json(state_path, state)
        result = poll(client, state, state_path, wait)
        result["receipt"] = archive_json(job / "发布记录/result.json", result)
        return result


def reconcile(job, config, draft_id=None, publish_id=None):
    manifest, _ = checked_job(job)
    state_path = journal_path(config, manifest)
    with locked(state_path.with_suffix(".lock")):
        if not state_path.exists():
            raise PublisherError("没有待恢复的任务")
        state = read_json(state_path)
        client = Client(config)
        if publish_id:
            if state["stage"] not in {"submit_inflight", "submit_unknown"}:
                raise PublisherError("仅发布结果未知时允许绑定发布 ID")
            candidate = dict(state, publish_id=publish_id)
            # Query before binding; this operation never re-submits publication.
            return poll(client, candidate, state_path, 0)
        if draft_id:
            if state["stage"] not in {"draft_inflight", "draft_unknown"}:
                raise PublisherError("仅草稿结果未知时允许绑定草稿 ID")
            result = client.request("draft/get", {"media_id": draft_id})
            items = result.get("news_item", [])
            if len(items) != 1 or items[0].get("title") != state["title"]:
                raise PublisherError("草稿标题/篇数不匹配，请核对后台完整正文")
            state.update(stage="draft_ready", draft_media_id=draft_id)
            write_json(state_path, state)
            return {"ok": True, "stage": "draft_ready", "draft_media_id": draft_id}
        raise PublisherError("指定已核实的 --draft-id 或 --publish-id")


def save_config(value, update=False, path=None):
    config_path = Path(path or CONFIG)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2).encode()
    if config_path.exists():
        if not update:
            raise PublisherError("已有配置；使用 configure --update 在本机更新")
        old = config_path.read_bytes()
        if old == data:
            return
        backup = config_path.parent / "备份" / ("account-v-" + digest(old)[:16] + ".json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            if backup.read_bytes() != old:
                raise PublisherError("本机配置备份校验失败，已停止更新")
        else:
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(old)
        temporary = config_path.with_name("account-update-" + uuid.uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
    else:
        descriptor = os.open(config_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)


def set_delivery(config_path, delivery, subject_type=None):
    config = load_config(config_path)
    if subject_type:
        config["subject_type"] = subject_type
        config["publish_api_available"] = False if subject_type == "personal" else None
    if delivery == "publish" and config.get("publish_api_available") is False:
        raise PublisherError("该账号已确认没有正式发布接口权限，请使用 draft 模式")
    config["delivery"] = delivery
    save_config(config, update=True, path=config_path)
    return {"ok": True, "account_name": config.get("account_name", ""), "delivery": delivery, "subject_type": config.get("subject_type", "unknown"), "publish_api_available": config.get("publish_api_available"), "credentials_changed": False}


def configure(update=False):
    if CONFIG.exists() and not update:
        raise PublisherError("已有配置。请在本机编辑配置或先备份为明确版本；不会覆盖。")
    if not sys.stdin.isatty():
        raise PublisherError("请在本机交互终端配置，密钥不要放进聊天或命令参数")
    current = read_json(CONFIG) if update and CONFIG.exists() else {}
    account_name = input("公众号名称（回车保留原值）：" if current else "公众号名称：").strip() or current.get("account_name", "")
    app_id = input("AppID（回车保留原值）：" if current else "AppID：").strip() or current.get("app_id", "")
    app_secret = getpass.getpass("AppSecret（输入隐藏）：").strip()
    author = input("默认作者（回车保留原值）：" if current else "默认作者（可留空）：").strip() or current.get("author", "")
    if not app_id or not app_secret:
        raise PublisherError("AppID 和 AppSecret 不能为空")
    if any(c.isspace() for c in app_id + app_secret):
        raise PublisherError("AppID 或 AppSecret 包含空白字符，请在本机重新复制完整值")
    value = {"delivery": "draft", "subject_type": "unknown", "publish_api_available": None, "theme": "editorial", "inline_images": 2, "output_root": str(OUTPUT), **current, "account_name": account_name, "app_id": app_id, "app_secret": app_secret, "author": author}
    save_config(value, update=update)
    return {"ok": True, "configured": True, "updated": bool(current), "account_name": account_name, "delivery": value["delivery"], "config": str(CONFIG)}


def doctor(config_path, online=False):
    dependencies = {}
    for name in ["markdown", "PIL"]:
        try:
            __import__(name)
            dependencies[name] = True
        except ImportError:
            dependencies[name] = False
    result = {"ok": all(dependencies.values()), "version": VERSION, "dependencies": dependencies, "configured": Path(config_path).is_file(), "output_root": str(OUTPUT), "live_publication_tested": False}
    if result["configured"]:
        config = load_config(config_path)
        result.update(account_name=config.get("account_name", ""), delivery=config.get("delivery", "draft"), subject_type=config.get("subject_type", "unknown"), publish_api_available=config.get("publish_api_available"), theme=config.get("theme", "editorial"), author=config.get("author", ""), need_open_comment=config.get("need_open_comment", 1), only_fans_can_comment=config.get("only_fans_can_comment", 0))
        if online:
            client = Client(config)
            client.access_token()
            client.request("draft/count")
            result["connection_ok"] = True
            result["publish_permission_verified"] = False
    return result


def main():
    parser = argparse.ArgumentParser(description="公众号自动成稿后的排版、配图上传和正式发布")
    parser.add_argument("--config", type=Path, default=CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    setup.add_argument("--update", action="store_true", help="在本机重新填写凭据，更新前保留私有配置备份")
    mode = commands.add_parser("set-delivery")
    mode.add_argument("--mode", choices=["draft", "publish"], required=True)
    mode.add_argument("--subject", choices=["personal", "organization", "unknown"])
    check = commands.add_parser("doctor")
    check.add_argument("--online", action="store_true")
    render = commands.add_parser("build")
    render.add_argument("--article", type=Path, required=True)
    render.add_argument("--output", type=Path, default=OUTPUT)
    revision = commands.add_parser("update-draft")
    revision.add_argument("--job", type=Path, required=True)
    revision.add_argument("--previous-job", type=Path, required=True)
    for name in ["run", "status", "reconcile"]:
        sub = commands.add_parser(name)
        sub.add_argument("--job", type=Path, required=True)
        if name == "reconcile":
            group = sub.add_mutually_exclusive_group(required=True)
            group.add_argument("--draft-id")
            group.add_argument("--publish-id")
        else:
            sub.add_argument("--wait", type=int, default=20 if name == "run" else 0, choices=range(0, 61), metavar="0..60")
            if name == "run":
                sub.add_argument("--publish", action="store_true", help="正式发布；不指定则仅创建草稿")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            result = configure(update=args.update)
        elif args.command == "set-delivery":
            result = set_delivery(args.config, args.mode, args.subject)
        elif args.command == "doctor":
            result = doctor(args.config, args.online)
        elif args.command == "build":
            result = build(args.article, args.output)
        elif args.command == "reconcile":
            result = reconcile(args.job, load_config(args.config), args.draft_id, args.publish_id)
        elif args.command == "update-draft":
            result = update_draft(args.job, args.previous_job, load_config(args.config))
        else:
            result = run(args.job, load_config(args.config), publish=getattr(args, "publish", False), wait=args.wait, status_only=args.command == "status")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    except (PublisherError, OSError, ValueError, KeyError, ImportError) as error:
        # Generic local errors can include private configuration values; avoid dumping them.
        message = str(error) if isinstance(error, PublisherError) else "本地文件、配置或依赖异常；请检查输入路径/JSON 和 doctor 结果"
        print(json.dumps({"ok": False, "error": message, "unknown_result": isinstance(error, UnknownResult)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
