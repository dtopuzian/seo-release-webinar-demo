"""Send a bounded, honest demo result to the presenter, without polling."""
import html
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


def failure_reason(result):
    """Use only a verified main-page observation; never infer an HTTP code."""
    checks = result.get("checks")
    main = next((item for item in checks if type(item) is dict and item.get("path") == "/"), None) if type(checks) is list else None
    if main and main.get("passed") is False:
        directive = main.get("robots_directive")
        if (main.get("code") == "unexpected_noindex" and type(directive) is str
                and directive in {"noindex", "none"}):
            return f"на главной странице найден {directive}."
        status = main.get("http_status")
        if (main.get("code") == "required_http_200_complete"
                and type(status) is int and 100 <= status <= 599 and status != 200):
            return f"Главная страница вернула HTTP {status}."
    return "Не удалось завершить SEO-проверку."


def message(result, scene):
    if type(result) is not dict:
        result = unconfirmed()
    outcome = result.get("outcome") if result.get("verified") is True else "unconfirmed"
    check_incomplete = (outcome == "error" and scene == "checked"
                        and result.get("reason_codes") == ["check_incomplete"])
    if outcome == "published":
        rows = ["✅ Деплой выполнен."]
    elif outcome == "blocked" or check_incomplete:
        rows = ["⛔️ Деплой остановлен.", "Причина: " + failure_reason(result)]
    else:
        rows = ["⚠️ Деплой не подтверждён.", "Причина: не удалось подтвердить результат выпуска."]
    run = result.get("run")
    pipeline = workflow_url(os.environ, run)
    links = []
    if pipeline is not None:
        links.append('Ссылка на run: <a href="' + html.escape(pipeline, quote=True) + '">Открыть запуск</a>')
    if (outcome == "blocked" and matches(r"[1-9][0-9]{0,19}", run)
            and result.get("target") == scene and scene in {"unsafe", "checked"}):
        url = report_url(result.get("report_url"), run, scene)
        if url is not None:
            links.append('Ссылка на отчёт: <a href="' + html.escape(url, quote=True) + '">Открыть отчёт</a>')
    if links:
        rows.append("\n".join(links))
    return "\n\n".join(rows)


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
                           "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}).encode()
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
