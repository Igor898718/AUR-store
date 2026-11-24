#!/usr/bin/env python3
"""AUR helper с рекурсивным разрешением зависимостей.

Helper клонирует git-репозиторий пакета с aur.archlinux.org, читает зависимости
из PKGBUILD через `makepkg --printsrcinfo`, рекурсивно доустанавливает AUR-зависимости
и собирает пакет через Arch Build System (`makepkg -si`). Зависимости из
официальных репозиториев устанавливает сам makepkg.

Примеры:

    python aur_helper.py clone yay
    python aur_helper.py build yay
    python aur_helper.py install yay
    python aur_helper.py install --noconfirm yay
    python aur_helper.py install --with-optdeps yay
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUR_GIT_URL = "https://aur.archlinux.org/{name}.git"
AUR_RPC_URL = "https://aur.archlinux.org/rpc/v5"
USER_AGENT = "AUR-Store/1.0 (+https://github.com/Igor898718/AUR-store)"
RPC_TIMEOUT = 30

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+-]*$")
DEP_NAME_RE = re.compile(r"^([A-Za-z0-9@._+-]+)")
DEPENDENCY_KEYS = ("depends", "makedepends", "checkdepends", "optdepends")


class HelperError(RuntimeError):
    """Ошибка выполнения helper'а."""


class _DependencyContext:
    def __init__(
        self,
        build_dir: Path,
        *,
        noconfirm: bool,
        with_optdeps: bool,
        nocheck: bool,
    ) -> None:
        self.build_dir = build_dir
        self.noconfirm = noconfirm
        self.with_optdeps = with_optdeps
        self.nocheck = nocheck
        self.installed: set[str] = set()
        self.stack: list[str] = []


def default_build_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "aur-store" / "build"


def _require(command: str) -> None:
    if shutil.which(command) is None:
        raise HelperError(f"команда «{command}» не найдена в PATH")


def _ensure_build_environment() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise HelperError(
            "makepkg нельзя запускать от root — запустите helper от обычного пользователя"
        )
    _require("makepkg")


def _validate(package: str) -> str:
    if not NAME_RE.fullmatch(package):
        raise HelperError(f"недопустимое имя пакета: {package!r}")
    return package


def _unique(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _run(command: list[str], cwd: Path | None = None) -> None:
    printable = " ".join(shlex.quote(str(part)) for part in command)
    location = f" (в {cwd})" if cwd else ""
    print(f"+ {printable}{location}", flush=True)

    try:
        result = subprocess.run([str(part) for part in command], cwd=cwd)
    except FileNotFoundError as exc:
        raise HelperError(f"команда «{command[0]}» не найдена") from exc
    except KeyboardInterrupt as exc:
        raise HelperError("выполнение прервано пользователем") from exc

    if result.returncode != 0:
        raise HelperError(
            f"команда завершилась с кодом {result.returncode}: {command[0]}"
        )


def _run_capture(
    command: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(part) for part in command],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HelperError(f"команда «{command[0]}» не найдена") from exc
    except KeyboardInterrupt as exc:
        raise HelperError("выполнение прервано пользователем") from exc


def _current_arch() -> str:
    return os.environ.get("CARCH") or platform.machine() or "x86_64"


def _dep_spec(dependency: str) -> str:
    """Убрать описание опциональной зависимости, сохранив версию."""
    return re.split(r":\s+", dependency.strip(), maxsplit=1)[0].strip()


def _dep_name(spec: str) -> str:
    match = DEP_NAME_RE.match(spec.strip())
    return match.group(1) if match else ""


def _parse_srcinfo(text: str, arch: str) -> dict[str, list[str]]:
    dependencies: dict[str, list[str]] = {key: [] for key in DEPENDENCY_KEYS}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or " = " not in stripped:
            continue
        key, value = stripped.split(" = ", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            continue

        base, separator, suffix = key.partition("_")
        if base not in dependencies:
            continue
        if separator and suffix != arch:
            continue
        dependencies[base].append(value)

    return {key: _unique(values) for key, values in dependencies.items()}


def read_dependencies(repository: Path) -> dict[str, list[str]]:
    """Прочитать depends/makedepends/checkdepends/optdepends из PKGBUILD."""
    result = _run_capture(["makepkg", "--printsrcinfo"], cwd=repository)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise HelperError(
            f"не удалось прочитать зависимости {repository.name}: {details}"
        )
    return _parse_srcinfo(result.stdout, _current_arch())


def _pacman_missing(specs: list[str]) -> list[str]:
    """Вернуть зависимости из списка, которые ещё не установлены."""
    specs = _unique(_dep_spec(spec) for spec in specs)
    if not specs:
        return []

    _require("pacman")
    result = _run_capture(["pacman", "-T", *specs])
    missing = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if missing:
        return missing
    if result.returncode != 0:
        details = result.stderr.strip() or f"код {result.returncode}"
        raise HelperError(f"не удалось проверить зависимости: {details}")
    return []


def _repo_provides(spec: str) -> bool:
    """Может ли официальный репозиторий удовлетворить зависимость."""
    result = _run_capture(
        ["pacman", "-Sp", "--noconfirm", "--print-format", "%n", spec]
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _rpc_request(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=RPC_TIMEOUT) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HelperError(f"ошибка запроса к AUR RPC: {exc}") from exc
    if not isinstance(data, dict):
        raise HelperError("AUR RPC вернул неожиданный ответ")
    return data


def _aur_info(name: str) -> dict | None:
    query = urllib.parse.urlencode([("arg[]", name)])
    data = _rpc_request(f"{AUR_RPC_URL}/info?{query}")
    results = data.get("results") or []
    return results[0] if results else None


def _aur_search_provides(name: str) -> list[dict]:
    query = urllib.parse.urlencode({"by": "provides"})
    data = _rpc_request(
        f"{AUR_RPC_URL}/search/{urllib.parse.quote(name)}?{query}"
    )
    results = data.get("results") or []
    return [result for result in results if isinstance(result, dict)]


def _resolve_aur_provider(name: str, exclude: set[str]) -> str | None:
    """Найти AUR-пакет для зависимости: точное имя или провайдер."""
    if name not in exclude and _aur_info(name) is not None:
        return name

    candidates = []
    for result in _aur_search_provides(name):
        provider = result.get("Name")
        if not isinstance(provider, str) or not NAME_RE.fullmatch(provider):
            continue
        if provider in exclude:
            continue
        candidates.append(result)

    if not candidates:
        return None

    candidates.sort(
        key=lambda result: (
            result.get("OutOfDate") is not None,
            -(result.get("NumVotes") or 0),
            -(result.get("Popularity") or 0),
            str(result.get("Name", "")).lower(),
        )
    )
    chosen = candidates[0]["Name"]
    print(
        f"==> Для виртуальной зависимости «{name}» выбран AUR-провайдер "
        f"«{chosen}».",
        flush=True,
    )
    return chosen


def clone_package(package: str, build_dir: Path) -> Path:
    """Клонировать (или обновить) репозиторий пакета и вернуть его путь."""
    package = _validate(package)
    _require("git")

    repository = build_dir / package
    if repository.exists():
        if not (repository / ".git").is_dir():
            raise HelperError(
                f"каталог {repository} уже существует и не является git-репозиторием"
            )
        print(f"==> Обновление {package}", flush=True)
        _run(["git", "-C", str(repository), "pull", "--ff-only"])
        return repository

    build_dir.mkdir(parents=True, exist_ok=True)
    print(f"==> Клонирование {package}", flush=True)
    _run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            AUR_GIT_URL.format(name=package),
            str(repository),
        ]
    )
    return repository


def build_repository(
    repository: Path,
    package: str,
    *,
    install: bool = True,
    noconfirm: bool = False,
    force: bool = False,
    nocheck: bool = False,
) -> Path:
    """Собрать уже склонированный пакет через makepkg."""
    _ensure_build_environment()

    command = ["makepkg", "-s"]
    if install:
        command.append("-i")
    if nocheck:
        command.append("--nocheck")
    if noconfirm:
        command.append("--noconfirm")
    if force:
        command.append("--force")

    action = "Сборка и установка" if install else "Сборка"
    print(f"==> {action}: {package}", flush=True)
    _run(command, cwd=repository)
    print(f"==> Готово: {package}", flush=True)
    return repository


def build_package(
    package: str,
    build_dir: Path,
    *,
    install: bool = True,
    noconfirm: bool = False,
    force: bool = False,
    nocheck: bool = False,
) -> Path:
    """Собрать пакет без рекурсивного разрешения AUR-зависимостей."""
    _ensure_build_environment()
    repository = clone_package(package, build_dir)
    return build_repository(
        repository,
        package,
        install=install,
        noconfirm=noconfirm,
        force=force,
        nocheck=nocheck,
    )


def _process_package(
    package: str,
    context: _DependencyContext,
    *,
    install: bool,
    force: bool,
) -> None:
    package = _validate(package)
    if package in context.installed:
        return
    if package in context.stack:
        cycle = " -> ".join([*context.stack, package])
        raise HelperError(f"циклическая зависимость: {cycle}")

    print(f"==> {package}: проверка зависимостей", flush=True)
    repository = clone_package(package, context.build_dir)
    dependencies = read_dependencies(repository)

    required = [*dependencies["depends"], *dependencies["makedepends"]]
    if not context.nocheck:
        required.extend(dependencies["checkdepends"])
    if context.with_optdeps:
        required.extend(dependencies["optdepends"])

    specs = _unique(_dep_spec(dependency) for dependency in required)
    missing = _pacman_missing(specs)
    if missing:
        print(
            f"==> {package}: не хватает зависимостей: {', '.join(missing)}",
            flush=True,
        )

    context.stack.append(package)
    try:
        for spec in missing:
            spec = _dep_spec(spec)
            if not spec:
                continue
            if not _pacman_missing([spec]):
                continue

            name = _dep_name(spec)
            if not name or name == package:
                continue
            if name in context.installed:
                continue
            if name in context.stack:
                cycle = " -> ".join([*context.stack, name])
                raise HelperError(f"циклическая зависимость: {cycle}")

            if _repo_provides(spec):
                print(
                    f"    {spec}: официальный репозиторий, установит makepkg -s",
                    flush=True,
                )
                continue

            provider = _resolve_aur_provider(
                name, exclude=set(context.stack) | context.installed
            )
            if provider is None:
                raise HelperError(
                    f"не удалось найти зависимость «{spec}» для «{package}» "
                    "ни в официальных репозиториях, ни в AUR"
                )
            if provider == package:
                continue

            print(
                f"==> {package}: установка AUR-зависимости {provider}",
                flush=True,
            )
            _process_package(provider, context, install=True, force=False)

        build_repository(
            repository,
            package,
            install=install,
            noconfirm=context.noconfirm,
            force=force,
            nocheck=context.nocheck,
        )
    finally:
        context.stack.pop()

    if install:
        context.installed.add(package)


def process_package(
    package: str,
    build_dir: Path,
    *,
    install: bool = True,
    noconfirm: bool = False,
    force: bool = False,
    with_deps: bool = True,
    with_optdeps: bool = False,
    nocheck: bool = False,
) -> None:
    """Установить пакет вместе со всеми зависимостями."""
    _ensure_build_environment()

    if not with_deps:
        build_package(
            package,
            build_dir,
            install=install,
            noconfirm=noconfirm,
            force=force,
            nocheck=nocheck,
        )
        return

    _require("pacman")
    context = _DependencyContext(
        build_dir,
        noconfirm=noconfirm,
        with_optdeps=with_optdeps,
        nocheck=nocheck,
    )
    _process_package(package, context, install=install, force=force)


def _build_parser() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument(
        "--dir",
        default=str(default_build_dir()),
        help="каталог для клонов (по умолчанию: %(default)s)",
    )

    build_options = argparse.ArgumentParser(add_help=False)
    build_options.add_argument(
        "--noconfirm",
        action="store_true",
        help="передать makepkg флаг --noconfirm",
    )
    build_options.add_argument(
        "--force",
        action="store_true",
        help="пересобрать запрошенный пакет даже при наличии артефактов",
    )
    build_options.add_argument(
        "--no-deps",
        action="store_true",
        help="не разрешать AUR-зависимости рекурсивно (только makepkg -s)",
    )
    build_options.add_argument(
        "--with-optdeps",
        action="store_true",
        help="устанавливать также все опциональные зависимости",
    )
    build_options.add_argument(
        "--nocheck",
        action="store_true",
        help="не устанавливать checkdepends и передать makepkg --nocheck",
    )

    parser = argparse.ArgumentParser(
        description="Клонирование и сборка пакетов из AUR через makepkg.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    clone_parser = subparsers.add_parser(
        "clone", parents=[base], help="только клонировать/обновить репозиторий"
    )
    clone_parser.add_argument("packages", nargs="+", help="имена пакетов AUR")

    build_parser = subparsers.add_parser(
        "build", parents=[base, build_options], help="собрать пакет без установки"
    )
    build_parser.add_argument("packages", nargs="+", help="имена пакетов AUR")

    install_parser = subparsers.add_parser(
        "install",
        parents=[base, build_options],
        help="собрать и установить пакет (makepkg -si)",
    )
    install_parser.add_argument("packages", nargs="+", help="имена пакетов AUR")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    build_dir = Path(args.dir).expanduser().resolve()

    try:
        for package in args.packages:
            if args.command == "clone":
                clone_package(package, build_dir)
            else:
                process_package(
                    package,
                    build_dir,
                    install=args.command == "install",
                    noconfirm=args.noconfirm,
                    force=args.force,
                    with_deps=not args.no_deps,
                    with_optdeps=args.with_optdeps,
                    nocheck=args.nocheck,
                )
    except HelperError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
