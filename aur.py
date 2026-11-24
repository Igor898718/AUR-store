#!/usr/bin/env python3
"""Небольшой клиент AUR для графического интерфейса.

Модуль не зависит от Qt и умеет:
  * загружать и кешировать список имён пакетов (packages.gz);
  * искать пакеты по именам;
  * загружать страницу пакета на aur.archlinux.org и разбирать её;
  * превращать сведения о пакете в HTML для панели информации.
"""

from __future__ import annotations

import gzip
import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUR_BASE = "https://aur.archlinux.org"
PACKAGES_URL = f"{AUR_BASE}/packages.gz"

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "aur-store"
CACHE_FILE = CACHE_DIR / "packages.gz"
CACHE_MAX_AGE = 24 * 60 * 60

REQUEST_TIMEOUT = 30
USER_AGENT = "AUR-Store/1.0 (+https://github.com/Igor898718/AUR-store)"


class AurError(RuntimeError):
    """Общая ошибка при обращении к AUR."""


class PackageNotFound(AurError):
    """Пакет отсутствует на AUR (страница вернула 404)."""


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def _decode_names(raw: bytes) -> list[str]:
    try:
        text = gzip.decompress(raw).decode("utf-8", "replace")
    except (OSError, gzip.BadGzipFile):
        text = raw.decode("utf-8", "replace")
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


def load_package_db(force: bool = False) -> list[str]:
    """Вернуть отсортированный список имён пакетов AUR.

    Свежий кеш (моложе суток) используется без обращения к сети. При ошибке
    загрузки возвращается даже устаревший кеш, если он есть.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not force and CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_MAX_AGE:
            try:
                return _decode_names(CACHE_FILE.read_bytes())
            except OSError:
                pass

    try:
        raw = _download(PACKAGES_URL)
        CACHE_FILE.write_bytes(raw)
        return _decode_names(raw)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if CACHE_FILE.exists():
            return _decode_names(CACHE_FILE.read_bytes())
        raise AurError(f"Не удалось загрузить базу пакетов: {exc}") from exc


def search_packages(names: list[str], query: str, limit: int = 500) -> list[str]:
    """Найти пакеты по подстроке в имени.

    Сначала идут точное совпадение и совпадения с начала имени, затем более
    короткие имена. Пустой запрос не возвращает ничего.
    """
    query = query.strip().casefold()
    if not query:
        return []

    matches: list[tuple[int, int, str, str]] = []
    for name in names:
        lowered = name.casefold()
        position = lowered.find(query)
        if position < 0:
            continue
        rank = 0 if lowered == query else 1 if position == 0 else 2
        matches.append((rank, len(name), lowered, name))

    matches.sort()
    return [entry[3] for entry in matches[:limit]]


def fetch_package_info(package: str) -> dict:
    """Загрузить страницу пакета и вернуть разобранные сведения."""
    page = fetch_package_page(package)
    info = parse_package_page(page)
    info.setdefault("name", package)
    info["aur_url"] = f"{AUR_BASE}/packages/{urllib.parse.quote(package)}"
    return info


def fetch_package_page(package: str) -> str:
    url = f"{AUR_BASE}/packages/{urllib.parse.quote(package)}"
    try:
        raw = _download(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PackageNotFound(f"Пакет «{package}» не найден на AUR") from exc
        raise AurError(f"AUR вернул HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise AurError(f"Не удалось загрузить страницу пакета: {exc}") from exc
    return raw.decode("utf-8", "replace")


_PKGINFO_TABLE = re.compile(r'<table\s+id="pkginfo">(.*?)</table>', re.S | re.I)
_TABLE_ROW = re.compile(
    r"<tr[^>]*>\s*<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>\s*</tr>",
    re.S | re.I,
)
_FIELD_NAMES = {
    "Description": "description",
    "Upstream URL": "upstream_url",
    "Package Base": "package_base",
    "Git Clone URL": "git_url",
    "Licenses": "licenses",
    "Submitter": "submitter",
    "Maintainer": "maintainer",
    "Last Packager": "last_packager",
    "Votes": "votes",
    "Popularity": "popularity",
    "First Submitted": "first_submitted",
    "Last Updated": "last_updated",
    "Keywords": "keywords",
}


def _to_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _find_hrefs(fragment: str) -> list[str]:
    return [html.unescape(href) for href in re.findall(r'href="([^"]+)"', fragment, re.I)]


def parse_package_page(page: str) -> dict:
    """Разобрать HTML страницы пакета AUR в словарь сведений."""
    info: dict = {}
    links: dict[str, str] = {}

    title_match = re.search(
        r"<h2[^>]*>\s*Package Details:\s*(.*?)</h2>", page, re.S | re.I
    )
    if title_match:
        parts = _to_text(title_match.group(1)).rsplit(maxsplit=1)
        if len(parts) == 2:
            info["name"], info["version"] = parts
        elif parts:
            info["name"] = parts[0]

    table_match = _PKGINFO_TABLE.search(page)
    if table_match:
        for header, cell in _TABLE_ROW.findall(table_match.group(1)):
            label = _to_text(header).rstrip(":").strip()
            field = _FIELD_NAMES.get(label)
            if not field:
                continue

            value = _to_text(cell)
            hrefs = _find_hrefs(cell)
            if hrefs:
                links[field] = hrefs[0]
            if field == "git_url" and hrefs:
                value = hrefs[0]
            value = re.sub(r"\s*\(read-only[^)]*\)\s*$", "", value).strip()

            if field == "votes":
                info[field] = int(value) if value.isdigit() else 0
            elif field == "popularity":
                try:
                    info[field] = float(value)
                except ValueError:
                    info[field] = 0.0
            else:
                info[field] = value

    deps_block = re.search(r'<div id="pkgdeps"[^>]*>(.*?)</div>', page, re.S | re.I)
    if deps_block:
        dependencies = []
        for item in re.findall(r"<li[^>]*>(.*?)</li>", deps_block.group(1), re.S | re.I):
            anchor = re.search(r"<a[^>]*>(.*?)</a>", item, re.S | re.I)
            name = _to_text(anchor.group(1)) if anchor else _to_text(item)
            if not name:
                continue
            text = _to_text(item)
            flags = [
                flag
                for flag in ("make", "check", "optional")
                if f"({flag})" in text
            ]
            dependencies.append({"name": name, "flags": flags})
        info["dependencies"] = dependencies

    required_match = re.search(r"<h3[^>]*>\s*Required by \((\d+)\)", page, re.I)
    if required_match:
        info["required_by"] = int(required_match.group(1))

    if links:
        info["links"] = links
    return info


def _escape(value: object) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


def _safe_link(url: str, text: str | None = None) -> str:
    label = html.escape(text or url)
    if url.lower().startswith(("http://", "https://")):
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
    return label


_FLAG_LABELS = {"make": "сборка", "check": "проверка", "optional": "опционально"}


def format_package_html(info: dict, installed: bool = False) -> str:
    """Собрать HTML-представление сведений о пакете для QTextBrowser."""
    name = _escape(info.get("name"))
    version = _escape(info.get("version"))
    description = _escape(info.get("description"))
    links = info.get("links") or {}
    installed_badge = (
        '<span style="background:#1a7f37; color:#ffffff; padding:2px 8px; '
        'border-radius:4px; font-size:9.5pt; margin-left:6px;">установлен</span>'
        if installed
        else ""
    )

    rows: list[tuple[str, str]] = []
    if info.get("package_base"):
        rows.append(("Package Base", _escape(info["package_base"])))
    if info.get("upstream_url"):
        rows.append(("Upstream URL", _safe_link(info["upstream_url"])))
    if info.get("git_url"):
        rows.append(("Git Clone URL", f"<code>{_escape(info['git_url'])}</code>"))
    if info.get("licenses"):
        rows.append(("Лицензии", _escape(info["licenses"])))
    if info.get("maintainer"):
        rows.append(("Maintainer", _escape(info["maintainer"])))
    if info.get("submitter") and info.get("submitter") != info.get("maintainer"):
        rows.append(("Submitter", _escape(info["submitter"])))
    if info.get("votes") is not None:
        rows.append(("Голоса", _escape(info.get("votes"))))
    if info.get("popularity") is not None:
        rows.append(("Популярность", _escape(info.get("popularity"))))
    if info.get("first_submitted"):
        rows.append(("Первая публикация", _escape(info["first_submitted"])))
    if info.get("last_updated"):
        rows.append(("Обновлён", _escape(info["last_updated"])))
    if info.get("required_by") is not None:
        rows.append(("Требуется пакетам", _escape(info["required_by"])))

    meta_html = "".join(
        '<tr>'
        f'<td style="color:#57606a; padding:3px 18px 3px 0; white-space:nowrap;">{label}</td>'
        f'<td style="padding:3px 0;">{value}</td>'
        "</tr>"
        for label, value in rows
    )

    dependencies = info.get("dependencies") or []
    if dependencies:
        chips = []
        for dependency in dependencies:
            dep_name = html.escape(str(dependency.get("name", "")))
            flags = " · ".join(
                _FLAG_LABELS.get(flag, flag) for flag in dependency.get("flags", [])
            )
            suffix = (
                f'<span style="color:#8c959f; font-size:9pt;"> ({html.escape(flags)})</span>'
                if flags
                else ""
            )
            chips.append(
                '<span style="background:#eef1f4; padding:2px 6px; '
                'border-radius:4px;">'
                f"<code>{dep_name}</code>{suffix}</span>"
            )
        dependencies_html = " ".join(chips)
    else:
        dependencies_html = '<span style="color:#8c959f;">нет</span>'

    aur_url = info.get("aur_url") or f"{AUR_BASE}/packages/{urllib.parse.quote(str(info.get('name', '')))}"

    return f"""
<div style="font-size:10.5pt; color:#1f2328;">
  <div style="font-size:20pt; font-weight:700; color:#1f2328;">{name}</div>
  <div style="margin-top:2px;">
    <span style="background:#1793d1; color:#ffffff; padding:2px 8px;
                 border-radius:4px; font-size:9.5pt;">{version}</span> {installed_badge}
  </div>
  <p style="margin:14px 0 16px 0; font-size:10.5pt; color:#24292f;">{description}</p>
  <table cellspacing="0" cellpadding="0" style="font-size:10pt;">{meta_html}</table>
  <div style="margin-top:20px;">
    <div style="color:#57606a; font-size:9pt; font-weight:700;
                letter-spacing:0.5px;">ЗАВИСИМОСТИ</div>
    <div style="margin-top:8px; line-height:1.9;">{dependencies_html}</div>
  </div>
  <div style="margin-top:22px; padding-top:12px; border-top:1px solid #d8dee4;">
    {_safe_link(aur_url, "Открыть страницу на AUR")}
  </div>
</div>
"""
