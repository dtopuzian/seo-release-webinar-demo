"""Send a bounded, honest demo result to the presenter, without polling."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

try:
    from .client import expected_binding, matches, report_url, unconfirmed, validated_result, workflow_url
except ImportError:  # Direct `python ci/notify.py` invocation.
    from client import expected_binding, matches, report_url, unconfirmed, validated_result, workflow_url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def message(result, scene):
    if type(result) is not dict:
        result = unconfirmed()
    outcome = result.get("outcome") if result.get("verified") is True else "unconfirmed"
    if outcome == "published":
        heading = ("✅ Демо: витрина возвращена к v1" if result.get("variant") == "good"
                   else "✅ Демо: новая версия опубликована")
    elif outcome == "blocked":
        heading = "⛔ Демо: SEO-проверка остановила выпуск"
    else:
        heading = "⚠️ Демо: выпуск не подтверждён"
    process = {"checked": "с SEO-проверкой", "unsafe": "без SEO-проверки"}.get(scene, "не подтверждён")
    rows = [heading, "Процесс: " + process]
    if outcome == "blocked":
        checks = result.get("checks")
        forbidden = [item["path"] for item in checks if type(item) is dict
                     and item.get("code") == "unexpected_noindex" and item.get("passed") is False
                     and matches(r"/(?:[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*\.html)?", item.get("path"))] if type(checks) is list else []
        if forbidden:
            rows.append("Найден meta robots noindex на страницах, которые должны индексироваться:")
            rows.extend(forbidden[:8])
        else:
            rows.append("Обязательные SEO-условия не подтверждены. См. отчёт проверки.")
        published = result.get("published")
        if (type(published) is dict and matches(r"[0-9a-f]{64}", result.get("artifact_sha256"))
                and published.get("artifact_sha256") == result["artifact_sha256"]):
            rows.append("Этот артефакт уже был опубликован ранее. Автоматический откат не выполнялся.")
        elif type(published) is dict and published.get("version") in {"v1", "v2"}:
            rows.append("Кандидат не опубликован. На витрине остаётся версия " + published["version"] + ".")
        else:
            rows.append("Публикация этого кандидата не подтверждена; текущее состояние витрины неизвестно.")
    elif outcome not in {"published", "blocked"}:
        rows.append("Не удалось подтвердить публикацию кандидата. Это не сообщение об успешном выпуске.")
    run = result.get("run")
    pipeline = workflow_url(os.environ, run)
    if pipeline is not None:
        rows.append("GitHub: " + pipeline)
    if matches(r"[1-9][0-9]{0,19}", run) and result.get("target") == scene and scene in {"unsafe", "checked"}:
        url = report_url(result.get("report_url"), run, scene)
        if url is not None and result.get("verified") is True:
            rows.append("Отчёт: " + url)
    return "\n".join(rows)[:3800]


def load_result():
    expected = expected_binding(os.environ)
    try:
        raw = Path("demo-result.json").read_bytes()
        if len(raw) > 65536:
            raise ValueError("oversized receipt")
        result = json.loads(raw)
        if type(result) is not dict or result.get("verified") is not True:
            return unconfirmed(expected)
        return validated_result(result, expected, result.get("action"), result.get("exit_code"))
    except (OSError, ValueError, TypeError):
        return unconfirmed(expected)


def main():
    try:
        result = load_result()
        token = os.environ["DEMO_TELEGRAM_TOKEN"]
        chat = os.environ["DEMO_TELEGRAM_CHAT"]
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"[1-9][0-9]*", chat):
            raise ValueError("invalid credentials")
        data = json.dumps({"chat_id": int(chat), "text": message(result, os.environ["DEMO_SCENE"]),
                           "link_preview_options": {"is_disabled": True}}).encode()
        request = Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data,
                          headers={"Content-Type": "application/json"}, method="POST")
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            payload = json.loads(response.read(65536))
        if type(payload) is not dict or payload.get("ok") is not True:
            raise ValueError("notification refused")
    except (OSError, KeyError, ValueError, TypeError, URLError):
        print("Telegram notification failed; the release decision is unchanged.", file=sys.stderr)
        return 1
    print("Presenter notified in Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
