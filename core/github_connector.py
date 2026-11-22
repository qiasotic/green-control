"""Получение CSV/XLSX из публичных репозиториев через официальный GitHub API."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = 8
API = "https://api.github.com"
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,100}")
GIT_SHA = re.compile(r"[0-9a-f]{40}")
EXTENSIONS = (".csv", ".xlsx")


class GitHubError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    owner: str
    repository: str
    ref: str = ""
    path: str = ""

    @property
    def address(self):
        return f"https://github.com/{self.owner}/{self.repository}"

    @property
    def api(self):
        return f"/repos/{self.owner}/{self.repository}"


@dataclass(frozen=True)
class File:
    source: Source
    size: int
    sha: str


@dataclass(frozen=True)
class Download:
    file: File
    content: bytes
    received_at: str

    def metadata(self):
        source = self.file.source
        return {
            "repository": source.address, "path": source.path, "ref": source.ref,
            "blob_sha": self.file.sha, "digest": sha256(self.content).hexdigest(),
            "received_at": self.received_at,
        }


def _path_ok(path):
    parts = path.split("/")
    return (len(path) <= 1500 and all(part and part not in (".", "..") for part in parts)
            and "\\" not in path and not any(ord(char) < 32 or ord(char) == 127 for char in path)
            and PurePosixPath(path).suffix.lower() in EXTENSIONS)


def parse_address(address):
    """Принимает адрес репозитория, адрес файла на GitHub или raw-ссылку."""
    address = address.strip()
    try:
        url = urlsplit(address)
    except ValueError as exc:
        raise GitHubError("Проверьте адрес GitHub.") from exc
    if (len(address) > 2000 or url.scheme != "https" or url.query or url.fragment
            or url.netloc.lower() not in ("github.com", "raw.githubusercontent.com")
            or any(ord(char) < 32 or ord(char) == 127 for char in address)):
        raise GitHubError("Укажите HTTPS-ссылку на публичный репозиторий или файл GitHub без дополнительных параметров.")
    parts = [unquote(part) for part in url.path.strip("/").split("/")]
    if len(parts) < 2 or any(not IDENTIFIER.fullmatch(part) or part in (".", "..") for part in parts[:2]):
        raise GitHubError("В адресе должны быть указаны владелец и название репозитория.")
    owner, repository = [name.lower() for name in parts[:2]]
    if url.netloc.lower() == "github.com" and len(parts) == 2:
        repository = repository.removesuffix(".git")
        if not repository:
            raise GitHubError("Укажите название репозитория.")
        return Source(owner, repository)
    if url.netloc.lower() == "github.com":
        if len(parts) < 5 or parts[2] != "blob":
            raise GitHubError("Откройте файл CSV или XLSX в репозитории и скопируйте ссылку на него.")
        parts = parts[:2] + parts[3:]
    elif len(parts) >= 6 and parts[2] == "refs" and parts[3] in ("heads", "tags"):
        # GitHub's raw link also uses refs/heads or refs/tags before the short name.
        parts = parts[:2] + parts[4:]
    if len(parts) < 4:
        raise GitHubError("В ссылке не указан файл CSV или XLSX.")
    ref, path = parts[2], "/".join(parts[3:])
    if (not ref or len(ref) > 240 or "\\" in ref
            or any(value in ("", ".", "..") for value in ref.split("/"))
            or any(ord(char) < 33 or ord(char) == 127 for char in ref)
            or not _path_ok(path)):
        raise GitHubError("Нужна ссылка на допустимый файл CSV или XLSX в публичном репозитории.")
    return Source(owner, repository, ref, path)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise GitHubError("GitHub вернул перенаправление. Проверьте актуальный адрес репозитория.")


def _get(endpoint, *, raw=False):
    # Endpoints are assembled locally. Remote URLs from metadata are never followed.
    req = Request(API + endpoint, headers={
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "User-Agent": "Green-Control",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with build_opener(_NoRedirect()).open(req, timeout=TIMEOUT) as response:
            length = response.headers.get("Content-Length", "")
            if length.isascii() and length.isdigit() and int(length) > MAX_BYTES:
                raise GitHubError("Ответ превышает допустимый размер 10 МБ.")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise GitHubError("Ответ превышает допустимый размер 10 МБ.")
            return data
    except HTTPError as exc:
        try:
            status = exc.code
            if status == 404:
                message = "Репозиторий или файл не найден. Проверьте адрес и публичный доступ."
            elif status == 429 or status == 403 and exc.headers is not None and exc.headers.get("X-RateLimit-Remaining") == "0":
                message = "GitHub ограничил число запросов. Повторите попытку позднее."
            elif status == 403:
                message = "GitHub отказал в доступе. Поддерживаются только публичные репозитории."
            else:
                message = f"Не удалось получить ответ GitHub. Код ответа {status}."
        finally:
            exc.close()
        raise GitHubError(message) from exc
    except (URLError, OSError) as exc:
        raise GitHubError("Не удалось соединиться с GitHub. Проверьте сеть и повторите попытку.") from exc


def _json(endpoint):
    try:
        value = json.loads(_get(endpoint))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GitHubError("GitHub вернул ответ в неожиданном формате.") from exc
    if not isinstance(value, dict):
        raise GitHubError("GitHub вернул ответ в неожиданном формате.")
    return value


def _file(source, data):
    size, sha = data.get("size"), data.get("sha")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not GIT_SHA.fullmatch(str(sha)):
        raise GitHubError("Не удалось определить размер и версию файла.")
    return File(source, size, sha)


def list_files(source):
    """Возвращает файлы и признак неполного списка GitHub."""
    if source.path:
        return [file_info(source)], False
    repository = _json(source.api)
    if repository.get("private") is not False:
        raise GitHubError("Поддерживаются только публичные репозитории.")
    ref = repository.get("default_branch")
    if not isinstance(ref, str) or not ref:
        raise GitHubError("В репозитории нет доступной основной ветки.")
    tree = _json(f"{source.api}/git/trees/{quote(ref, safe='')}?recursive=1")
    if not isinstance(tree.get("tree"), list):
        raise GitHubError("Не удалось прочитать список файлов репозитория.")
    files = []
    for entry in tree["tree"]:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if (entry.get("type") == "blob" and entry.get("mode") in ("100644", "100755")
                and isinstance(path, str) and _path_ok(path)):
            files.append(_file(replace(source, ref=ref, path=path), entry))
    return sorted(files, key=lambda item: item.source.path.casefold()), tree.get("truncated") is True


def file_info(source):
    if not _path_ok(source.path):
        raise GitHubError("Выберите файл CSV или XLSX.")
    return _walk_tree(source)


def _walk_tree(source):
    parts = source.path.split("/")
    ref = source.ref
    for index, part in enumerate(parts):
        tree = _json(f"{source.api}/git/trees/{quote(ref, safe='')}")
        entry = next((item for item in tree.get("tree", [])
                      if isinstance(item, dict) and item.get("path") == part), None)
        if entry is None:
            break
        if index == len(parts) - 1 and entry.get("type") == "blob" and entry.get("mode") in ("100644", "100755"):
            return _file(source, entry)
        ref = entry.get("sha", "")
        if entry.get("type") != "tree" or not GIT_SHA.fullmatch(str(ref)):
            break
    raise GitHubError("По этому пути нет обычного файла CSV или XLSX.")


def download(file):
    if file.size > MAX_BYTES:
        raise GitHubError("Файл превышает допустимый размер 10 МБ.")
    if not GIT_SHA.fullmatch(file.sha) or not _path_ok(file.source.path):
        raise GitHubError("Не удалось проверить версию файла.")
    data = _get(f"{file.source.api}/git/blobs/{file.sha}", raw=True)
    if len(data) != file.size:
        raise GitHubError("Размер полученного файла отличается от указанного в GitHub.")
    return Download(file, data, datetime.now(timezone.utc).isoformat(timespec="seconds"))
