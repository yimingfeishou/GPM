#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

GPM_VERSION = "0.1.0 Preview"

GALLT_HOME = Path(os.environ.get("GALLT_HOME") or (Path.home() / ".gallt"))
CONFIG_FILE = GALLT_HOME / "config.json"
CACHE_DIR = GALLT_HOME / "cache"
DOWNLOADS_DIR = CACHE_DIR / "downloads"
REGISTRY_CACHE = CACHE_DIR / "registry.json"
REGISTRY_META = CACHE_DIR / "registry.meta.json"

MANIFEST_NAME = "gallt.json"
LOCK_NAME = "gallt.lock.json"
MODULES_DIRNAME = "gallt_modules"
LOCAL_REGISTRY_NAME = "index.json"

DEFAULT_REGISTRY = os.environ.get("GPM_REGISTRY", f"./{LOCAL_REGISTRY_NAME}")

SEMVER_RE = re.compile(
    r"^v?"
    r"(\d+)(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:-([0-9A-Za-z.\-]+))?"
    r"(?:\+([0-9A-Za-z.\-]+))?$"
)


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


_COLOR = _supports_color()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(f"{_c('·', '36')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('✓', '32')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('!', '33')} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{_c('✗', '31')} {msg}", file=sys.stderr)


class GpmError(Exception):
    pass


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(normalized.encode("utf-8"))


def read_text_utf8(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GpmError(f"Cannot read {path}: {exc}") from exc

    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GpmError(
            f"{path} is not valid UTF-8 text: {exc} (save as UTF-8 without BOM)"
        ) from exc


def normalize_to_lf(path: Path) -> bool:
    data = path.read_bytes()
    original = data

    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    if data != original:
        path.write_bytes(data)
        return True
    return False


def _cmp_prerelease(a: Tuple[str, ...], b: Tuple[str, ...]) -> int:
    if a == b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for x, y in zip(a, b):
        xd, yd = x.isdigit(), y.isdigit()
        if xd and yd:
            xi, yi = int(x), int(y)
            if xi != yi:
                return -1 if xi < yi else 1
        elif xd != yd:
            return -1 if xd else 1
        elif x != y:
            return -1 if x < y else 1
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return 0


@functools.total_ordering
class SemVer:
    __slots__ = ("major", "minor", "patch", "prerelease", "build")

    def __init__(
        self,
        major: int,
        minor: int = 0,
        patch: int = 0,
        prerelease: Sequence[str] = (),
        build: str = "",
    ) -> None:
        self.major = int(major)
        self.minor = int(minor)
        self.patch = int(patch)
        self.prerelease: Tuple[str, ...] = tuple(prerelease)
        self.build = build or ""

    @classmethod
    def parse(cls, text: str) -> "SemVer":
        m = SEMVER_RE.match(text.strip())
        if not m:
            raise ValueError(f"Invalid version: {text!r}")
        major, minor, patch, pre, build = m.groups()
        return cls(
            int(major),
            int(minor or 0),
            int(patch or 0),
            tuple(pre.split(".")) if pre else (),
            build or "",
        )

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(self.prerelease)
        if self.build:
            s += "+" + self.build
        return s

    __repr__ = __str__

    def _core(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._core() == other._core() and _cmp_prerelease(
            self.prerelease, other.prerelease
        ) == 0

    def __lt__(self, other: "SemVer") -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        if self._core() != other._core():
            return self._core() < other._core()
        return _cmp_prerelease(self.prerelease, other.prerelease) < 0

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))


class VersionRange:
    __slots__ = ("raw", "_preds")

    def __init__(self, raw: Optional[str]) -> None:
        self.raw = (raw or "*").strip() or "*"
        tokens = [t for t in re.split(r"[\s,]+", self.raw) if t]
        if not tokens:
            tokens = ["*"]
        self._preds: List[Callable[[SemVer], bool]] = [
            self._compile(tok) for tok in tokens
        ]

    def matches(self, version: SemVer) -> bool:
        return all(pred(version) for pred in self._preds)

    def __contains__(self, version: SemVer) -> bool:
        return self.matches(version)

    def __repr__(self) -> str:
        return f"VersionRange({self.raw!r})"

    @staticmethod
    def _compile(token: str) -> Callable[[SemVer], bool]:
        if token in ("*", "x", "X", ""):
            return lambda _v: True

        m = re.match(r"^(>=|<=|!=|>|<|=|\^|~)?\s*v?(.+)$", token)
        if not m:
            raise GpmError(f"Invalid version range: {token!r}")
        op = m.group(1) or "="
        body = m.group(2)

        if re.search(r"(?:^|\.)[xX*](?:$|\.)", body):
            return VersionRange._wildcard(body)

        try:
            ver = SemVer.parse(body)
        except ValueError as exc:
            raise GpmError(f"Invalid version range: {token!r}") from exc

        if op == "=":
            return lambda v: v == ver
        if op == "!=":
            return lambda v: v != ver
        if op == ">":
            return lambda v: v > ver
        if op == ">=":
            return lambda v: v >= ver
        if op == "<":
            return lambda v: v < ver
        if op == "<=":
            return lambda v: v <= ver
        if op == "^":
            if ver.major > 0:
                upper = SemVer(ver.major + 1, 0, 0)
            elif ver.minor > 0:
                upper = SemVer(0, ver.minor + 1, 0)
            else:
                upper = SemVer(0, 0, ver.patch + 1)
            return lambda v: ver <= v < upper
        if op == "~":
            upper = SemVer(ver.major, ver.minor + 1, 0)
            return lambda v: ver <= v < upper

        raise GpmError(f"Invalid version range operator: {op!r}")

    @staticmethod
    def _wildcard(body: str) -> Callable[[SemVer], bool]:
        nums: List[int] = []
        for part in body.split("."):
            if part.lower() in ("x", "*", ""):
                break
            if not part.isdigit():
                raise GpmError(f"Invalid version range: {body!r}")
            nums.append(int(part))

        if not nums:
            return lambda _v: True

        major = nums[0]
        minor = nums[1] if len(nums) > 1 else 0
        patch = nums[2] if len(nums) > 2 else 0
        lower = SemVer(major, minor, patch)

        if len(nums) == 1:
            upper = SemVer(major + 1, 0, 0)
        elif len(nums) == 2:
            upper = SemVer(major, minor + 1, 0)
        else:
            upper = SemVer(major, minor, patch + 1)

        return lambda v: lower <= v < upper


@dataclass
class Manifest:
    name: str = "unnamed-package"
    version: str = ""
    description: str = ""
    authors: List[str] = field(default_factory=list)
    license: str = ""
    entry: str = "src/main.glt"
    repository: str = ""
    keywords: List[str] = field(default_factory=list)
    dependencies: Dict[str, str] = field(default_factory=dict)
    dev_dependencies: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Manifest":
        if not isinstance(data, dict):
            raise GpmError(f"The top level of {MANIFEST_NAME} must be a JSON object")

        def _as_text(value: Any, field_name: str, default: str = "") -> str:
            if value is None:
                return default
            if isinstance(value, str):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
            raise GpmError(
                f"Field '{field_name}' in {MANIFEST_NAME} must be a string, "
                f"got {type(value).__name__}"
            )

        def _as_list(value: Any, field_name: str) -> List[str]:
            if value is None:
                return []
            if not isinstance(value, list):
                raise GpmError(
                    f"Field '{field_name}' in {MANIFEST_NAME} must be an array"
                )
            result: List[str] = []
            for index, item in enumerate(value):
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    result.append(str(item))
                else:
                    raise GpmError(
                        f"Field '{field_name}[{index}]' in {MANIFEST_NAME} must be a string, "
                        f"got {type(item).__name__}"
                    )
            return result

        def _as_dep_map(value: Any, field_name: str) -> Dict[str, str]:
            if value is None:
                return {}
            if not isinstance(value, dict):
                raise GpmError(
                    f"Field '{field_name}' in {MANIFEST_NAME} must be an object "
                    f"(package name -> version constraint)"
                )
            result: Dict[str, str] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key:
                    raise GpmError(
                        f"Keys of field '{field_name}' in {MANIFEST_NAME} "
                        f"must be non-empty strings"
                    )
                if not isinstance(item, str):
                    raise GpmError(
                        f"Value of field '{field_name}.{key}' in {MANIFEST_NAME} "
                        f"must be a string, got {type(item).__name__}"
                    )
                result[key] = item
            return result

        return cls(
            name=_as_text(data.get("name"), "name", "unnamed-package"),
            version=_as_text(data.get("version"), "version", ""),
            description=_as_text(data.get("description"), "description", ""),
            authors=_as_list(data.get("authors"), "authors"),
            license=_as_text(data.get("license"), "license", ""),
            entry=_as_text(data.get("entry"), "entry", "src/main.glt"),
            repository=_as_text(data.get("repository"), "repository", ""),
            keywords=_as_list(data.get("keywords"), "keywords"),
            dependencies=_as_dep_map(data.get("dependencies"), "dependencies"),
            dev_dependencies=_as_dep_map(
                data.get("devDependencies"), "devDependencies"
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "authors": self.authors,
            "license": self.license,
            "entry": self.entry,
            "repository": self.repository,
            "keywords": self.keywords,
            "dependencies": dict(sorted(self.dependencies.items())),
            "devDependencies": dict(sorted(self.dev_dependencies.items())),
        }


def find_project_root(start: Optional[Path] = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / MANIFEST_NAME).is_file():
            return candidate
    raise GpmError(
        f"Could not find {MANIFEST_NAME} in the current directory or any parent; "
        f"run 'gpm init' first or switch to a Gallt project directory"
    )


def load_manifest(root: Path) -> Manifest:
    path = root / MANIFEST_NAME
    try:
        data = json.loads(read_text_utf8(path))
    except json.JSONDecodeError as exc:
        raise GpmError(f"{path} is not valid JSON: {exc}") from exc
    return Manifest.from_dict(data)


def save_manifest(root: Path, manifest: Manifest) -> None:
    path = root / MANIFEST_NAME
    write_text_lf(
        path,
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )


def load_lockfile(root: Path) -> Optional[Dict[str, Any]]:
    path = root / LOCK_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(read_text_utf8(path))
    except (OSError, GpmError, json.JSONDecodeError):
        return None


def write_lockfile(
    root: Path, resolved: Dict[str, SemVer], registry: "Registry"
) -> None:
    packages: Dict[str, Any] = {}
    for name in sorted(resolved):
        version = resolved[name]
        meta = registry.version_meta(name, version)
        packages[name] = {
            "version": str(version),
            "resolved": meta.get("url", ""),
            "integrity": meta.get("sha256", ""),
            "dependencies": dict(sorted(meta.get("dependencies", {}).items())),
        }

    data = {
        "lockfileVersion": 1,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "registry": registry.source,
        "packages": packages,
    }
    write_text_lf(
        root / LOCK_NAME,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(read_text_utf8(CONFIG_FILE))
    except (OSError, GpmError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: Dict[str, Any]) -> None:
    write_text_lf(
        CONFIG_FILE,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    )


def http_get(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"gpm/{GPM_VERSION} (Gallt)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise GpmError(f"HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise GpmError(f"Network error: {exc.reason} ({url})") from exc


def read_source(url: str) -> bytes:
    if url.startswith("file://"):
        return Path(url[7:]).read_bytes()
    if "://" not in url:
        path = Path(url)
        if not path.is_file():
            raise GpmError(f"File not found: {path}")
        return path.read_bytes()
    return http_get(url)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Registry:
    def __init__(self, data: Dict[str, Any], source: str = "<memory>") -> None:
        self.data = data
        self.source = source
        packages = data.get("packages")
        self._packages: Dict[str, Any] = (
            packages if isinstance(packages, dict) else data
        )

    def names(self) -> List[str]:
        return sorted(self._packages)

    def has(self, name: str) -> bool:
        return name in self._packages

    def entry(self, name: str) -> Dict[str, Any]:
        if name not in self._packages:
            raise GpmError(f"Package '{name}' not found in registry")
        entry = self._packages[name]
        return entry if isinstance(entry, dict) else {}

    def versions(self, name: str) -> List[SemVer]:
        raw = self.entry(name).get("versions", {})
        versions: List[SemVer] = []
        for text in raw:
            try:
                versions.append(SemVer.parse(str(text)))
            except ValueError:
                continue
        return sorted(versions, reverse=True)

    def version_meta(self, name: str, version: SemVer) -> Dict[str, Any]:
        raw = self.entry(name).get("versions", {})
        meta = raw.get(str(version), {})
        return meta if isinstance(meta, dict) else {}

    def dependencies(self, name: str, version: SemVer) -> Dict[str, str]:
        deps = self.version_meta(name, version).get("dependencies", {})
        return dict(deps) if isinstance(deps, dict) else {}

    def search(self, query: str) -> List[Tuple[str, Dict[str, Any]]]:
        query = query.lower()
        hits: List[Tuple[str, Dict[str, Any]]] = []
        for name in self.names():
            entry = self.entry(name)
            haystack = " ".join(
                [
                    name,
                    str(entry.get("description", "")),
                    " ".join(str(k) for k in entry.get("keywords", [])),
                ]
            ).lower()
            if query in haystack:
                hits.append((name, entry))
        return hits


def resolve_registry_url(args: argparse.Namespace) -> str:
    explicit = getattr(args, "registry", None)
    if explicit:
        return explicit
    from_env = os.environ.get("GPM_REGISTRY")
    if from_env:
        return from_env
    from_config = load_config().get("registry")
    if isinstance(from_config, str) and from_config:
        return from_config
    return DEFAULT_REGISTRY


def load_registry(
    url: str, *, offline: bool = False, refresh: bool = False
) -> Registry:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cached_url = ""
    if REGISTRY_META.is_file():
        try:
            cached_url = json.loads(read_text_utf8(REGISTRY_META)).get("url", "")
        except (OSError, GpmError, json.JSONDecodeError):
            cached_url = ""

    def _from_cache() -> Optional[Registry]:
        if not REGISTRY_CACHE.is_file():
            return None
        try:
            data = json.loads(read_text_utf8(REGISTRY_CACHE))
        except (OSError, GpmError, json.JSONDecodeError):
            return None
        return Registry(data, url)

    if not refresh and REGISTRY_CACHE.is_file() and cached_url == url:
        cached = _from_cache()
        if cached is not None:
            return cached

    if offline:
        cached = _from_cache()
        if cached is not None:
            warn(f"Offline mode: using cached registry index ({url})")
            return cached
        raise GpmError("No cached registry available in offline mode")

    try:
        payload = json.loads(read_source(url).decode("utf-8"))
    except Exception as exc:
        cached = _from_cache()
        if cached is not None:
            warn(f"Failed to fetch registry ({exc}), using local cache")
            return cached
        raise GpmError(
            f"Cannot read registry '{url}': {exc}\n"
            f"Hint: the default registry is a local './{LOCAL_REGISTRY_NAME}' "
            f"created by 'gpm init'. To use a different registry, pass "
            f"--registry <path or URL>, set the GPM_REGISTRY environment variable, "
            f"run 'gpm config set registry <path or URL>', "
            f"or edit the 'registry' field in {CONFIG_FILE}."
        ) from exc

    write_text_lf(
        REGISTRY_CACHE,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    write_text_lf(
        REGISTRY_META,
        json.dumps({"url": url, "time": time.time()}) + "\n",
    )
    return Registry(payload, url)


class ResolveError(GpmError):
    pass


class Resolver:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def resolve(self, dependencies: Dict[str, str]) -> Dict[str, SemVer]:
        pending: Dict[str, List[VersionRange]] = {
            name: [VersionRange(rng)] for name, rng in dependencies.items()
        }
        return self._solve({}, pending)

    def _candidates(
        self, name: str, ranges: Sequence[VersionRange]
    ) -> List[SemVer]:
        if not self.registry.has(name):
            return []
        return [
            v
            for v in self.registry.versions(name)
            if all(r.matches(v) for r in ranges)
        ]

    def _solve(
        self,
        assigned: Dict[str, SemVer],
        pending: Dict[str, List[VersionRange]],
    ) -> Dict[str, SemVer]:
        unresolved = {n: rs for n, rs in pending.items() if n not in assigned}
        if not unresolved:
            return dict(assigned)

        def _key(name: str) -> Tuple[int, str]:
            return (len(self._candidates(name, unresolved[name])), name)

        name = min(unresolved, key=_key)
        ranges = unresolved[name]

        candidates = self._candidates(name, ranges)
        if not candidates:
            if not self.registry.has(name):
                raise ResolveError(f"Package '{name}' not found in registry")
            wanted = ", ".join(r.raw for r in ranges)
            raise ResolveError(f"Package '{name}' has no version matching {wanted}")

        last_error: Optional[Exception] = None

        for version in candidates:
            new_assigned = dict(assigned)
            new_assigned[name] = version

            new_pending = {n: list(rs) for n, rs in pending.items()}
            new_pending.pop(name, None)

            conflict = False
            for dep_name, dep_range_raw in self.registry.dependencies(
                name, version
            ).items():
                dep_range = VersionRange(dep_range_raw)
                if dep_name in new_assigned:
                    if not dep_range.matches(new_assigned[dep_name]):
                        conflict = True
                        break
                else:
                    new_pending.setdefault(dep_name, []).append(dep_range)

            if conflict:
                continue

            try:
                return self._solve(new_assigned, new_pending)
            except ResolveError as exc:
                last_error = exc
                continue

        wanted = ", ".join(r.raw for r in ranges)
        detail = f" (last attempt failed: {last_error})" if last_error else ""
        raise ResolveError(
            f"Unable to find a version combination for '{name}' "
            f"matching {wanted}{detail}"
        )


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_cache_filename(url: str) -> str:
    if "://" in url:
        _, _, rest = url.partition("://")
        slash = rest.find("/")
        path_part = rest[slash:] if slash >= 0 else "/"
    else:
        path_part = url

    path_part = path_part.split("?", 1)[0].split("#", 1)[0]
    basename = path_part.rsplit("/", 1)[-1] or "package.tar.gz"
    basename = _SAFE_FILENAME_RE.sub("_", basename).lstrip(".")

    if not basename:
        basename = "package.tar.gz"

    if len(basename) > 80:
        dot = basename.rfind(".")
        if dot > 0 and len(basename) - dot <= 16:
            ext = basename[dot:]
            basename = basename[: 80 - len(ext)] + ext
        else:
            basename = basename[:80]

    return basename


def fetch_archive(
    url: str, sha256: Optional[str] = None, *, offline: bool = False
) -> Path:
    if url.startswith("file://") or "://" not in url:
        path = Path(url[7:]) if url.startswith("file://") else Path(url)
        if not path.is_file():
            raise GpmError(f"Local archive not found: {path}")
        if sha256 and sha256_of(path) != sha256:
            raise GpmError(f"Checksum mismatch: {path}")
        return path

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    filename = _safe_cache_filename(url)
    cached = DOWNLOADS_DIR / f"{key}-{filename}"

    if cached.is_file():
        if not sha256:
            return cached
        if sha256_of(cached) == sha256:
            return cached
        try:
            cached.unlink()
            warn(
                f"Cache checksum mismatch, deleted and re-downloading: {cached.name}"
            )
        except OSError:
            pass

    if offline:
        raise GpmError(f"Cannot download in offline mode: {url}")

    info(f"Downloading {url}")
    data = http_get(url)
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise GpmError(f"Checksum mismatch: {url}")

    tmp = cached.with_name(cached.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, cached)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise GpmError(f"Failed to write cache: {cached}: {exc}") from exc

    return cached


def _common_prefix(members: Iterable[str]) -> str:
    prefixes = set()
    for member in members:
        normalized = member.replace("\\", "/").lstrip("/")
        if not normalized:
            continue
        prefixes.add(normalized.split("/", 1)[0] if "/" in normalized else "")
    if len(prefixes) == 1:
        prefix = prefixes.pop()
        return prefix + "/" if prefix else ""
    return ""


def _safe_destination(dest: Path, relative: str) -> Path:
    target = (dest / relative).resolve()
    root = dest.resolve()
    if target != root and root not in target.parents:
        raise GpmError(f"Archive contains illegal path: {relative}")
    return target


def extract_archive(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    dest.mkdir(parents=True, exist_ok=True)

    if name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        with tarfile.open(archive, "r:*") as tar:
            members = [m for m in tar.getmembers() if m.isdir() or m.isfile()]
            strip = _common_prefix(m.name for m in members)
            for member in members:
                rel = (
                    member.name[len(strip):]
                    if strip and member.name.startswith(strip)
                    else member.name
                )
                rel = rel.lstrip("/")
                if not rel:
                    continue
                target = _safe_destination(dest, rel)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        continue
                    with source, target.open("wb") as out:
                        shutil.copyfileobj(source, out)
        return

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            strip = _common_prefix(names)
            for info_ in zf.infolist():
                rel = (
                    info_.filename[len(strip):]
                    if strip and info_.filename.startswith(strip)
                    else info_.filename
                )
                rel = rel.lstrip("/")
                if not rel:
                    continue
                target = _safe_destination(dest, rel)
                if info_.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info_) as source, target.open("wb") as out:
                        shutil.copyfileobj(source, out)
        return

    raise GpmError(f"Unsupported archive format: {archive.name}")


def install_one(
    registry: Registry,
    modules_dir: Path,
    name: str,
    version: SemVer,
    *,
    offline: bool = False,
) -> bool:
    meta = registry.version_meta(name, version)
    url = meta.get("url")
    if not url:
        raise GpmError(
            f"Package '{name}@{version}' has no download URL in registry"
        )

    dest = modules_dir / name
    marker = dest / ".gpm-version"
    if marker.is_file():
        try:
            if read_text_utf8(marker).strip() == str(version):
                return False
        except (OSError, GpmError):
            pass

    archive = fetch_archive(url, meta.get("sha256"), offline=offline)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    extract_archive(archive, dest)
    marker.write_text(str(version), encoding="utf-8")
    return True


def parse_package_spec(spec: str) -> Tuple[str, Optional[str]]:
    if "@" in spec:
        name, _, rng = spec.partition("@")
        name = name.strip()
        rng = rng.strip() or None
    else:
        name, rng = spec.strip(), None

    if not name:
        raise GpmError(f"Invalid package spec: {spec!r}")
    return name, rng


def validate_package_name(name: str) -> None:
    if not name:
        raise GpmError("Package name must not be empty")
    if name in (".", ".."):
        raise GpmError(
            f"Package name {name!r} is not allowed (reserved directory name)"
        )
    if "\x00" in name:
        raise GpmError("Package name must not contain NUL characters")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GpmError(
            f"Package name is not valid UTF-8: {name!r} ({exc})"
        ) from exc
    for ch in name:
        if ch in "/\\":
            raise GpmError(
                f"Package name must not contain path separators: {name!r}"
            )
        if ch in '<>:"|?*':
            raise GpmError(
                f"Package name must not contain character {ch!r} "
                f"(not portable across platforms): {name!r}"
            )
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise GpmError(
                f"Package name must not contain control characters: {name!r}"
            )


MAIN_TEMPLATE = """int main() {
    output("This is an example project\\n")
    output("Start your project from here")
    return 0
}
"""

GITIGNORE_TEMPLATE = """# Gallt package manager
gallt_modules/
"""

VSCODE_SETTINGS_TEMPLATE = """{
  "files.eol": "\\n",
  "files.encoding": "utf8",
  "[gallt]": {
    "files.eol": "\\n"
  }
}
"""

GITATTRIBUTES_TEMPLATE = """*.glt text eol=lf
"""

REGISTRY_TEMPLATE = """{
  "packages": {}
}
"""


def cmd_init(args: argparse.Namespace) -> int:
    root = Path.cwd()
    manifest_path = root / MANIFEST_NAME

    if manifest_path.exists() and not args.force:
        raise GpmError(
            f"{manifest_path} already exists; use --force to overwrite"
        )

    name = args.name or root.name
    validate_package_name(name)

    manifest = Manifest(name=name, version="", entry="src/main.glt")
    save_manifest(root, manifest)
    ok(f"Created {MANIFEST_NAME}")

    entry = root / manifest.entry
    if not entry.exists():
        entry.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(entry, MAIN_TEMPLATE)
        ok(f"Created entry file {manifest.entry}")

    modules = root / MODULES_DIRNAME
    modules.mkdir(exist_ok=True)

    registry_file = root / LOCAL_REGISTRY_NAME
    if not registry_file.exists():
        write_text_lf(registry_file, REGISTRY_TEMPLATE)
        ok(f"Created {LOCAL_REGISTRY_NAME} (local package registry)")

    gitignore = root / ".gitignore"
    if not gitignore.exists():
        write_text_lf(gitignore, GITIGNORE_TEMPLATE)
        ok("Created .gitignore")

    if args.editor_config:
        gitattr = root / ".gitattributes"
        if not gitattr.exists():
            write_text_lf(gitattr, GITATTRIBUTES_TEMPLATE)
            ok("Created .gitattributes (*.glt forced to LF)")

        vscode_dir = root / ".vscode"
        vscode_settings = vscode_dir / "settings.json"
        if not vscode_settings.exists():
            write_text_lf(vscode_settings, VSCODE_SETTINGS_TEMPLATE)
            ok("Created .vscode/settings.json (default LF / UTF-8)")

    print()
    info("Next steps:")
    print(f"    gpm install <package>    (install dependencies)")
    print(f"    gpm build                (invoke sgc to compile {manifest.entry})")
    print()
    info(
        f"Default registry is the local './{LOCAL_REGISTRY_NAME}'. "
        f"Edit it to add packages, or switch with "
        f"'gpm config set registry <path-or-url>' or '--registry <path-or-url>'."
    )
    return 0


def _collect_dependencies(
    manifest: Manifest,
    specs: Sequence[str],
    registry: Registry,
    *,
    include_dev: bool = True,
) -> Dict[str, str]:
    deps = dict(manifest.dependencies)
    if include_dev:
        for name, rng in manifest.dev_dependencies.items():
            if name in deps:
                warn(
                    f"{name} is present in both dependencies and devDependencies, "
                    f"using constraint {deps[name]} from dependencies"
                )
                continue
            deps[name] = rng

    for spec in specs:
        name, rng = parse_package_spec(spec)
        if not registry.has(name):
            raise GpmError(f"Package '{name}' not found in registry")
        if rng is None:
            versions = registry.versions(name)
            if not versions:
                raise GpmError(f"Package '{name}' has no available versions")
            rng = f"^{versions[0]}"
            info(f"{name}: no version specified, using ^{versions[0]} by default")
        else:
            VersionRange(rng)
        deps[name] = rng
    return deps


def _run_install(args: argparse.Namespace, *, is_update: bool) -> int:
    root = find_project_root()
    manifest = load_manifest(root)
    registry = load_registry(
        resolve_registry_url(args),
        offline=getattr(args, "offline", False),
        refresh=is_update,
    )

    include_dev = not getattr(args, "production", False)

    if is_update and not args.packages:
        dependencies = dict(manifest.dependencies)
        if include_dev:
            for name, rng in manifest.dev_dependencies.items():
                dependencies.setdefault(name, rng)
    else:
        dependencies = _collect_dependencies(
            manifest,
            args.packages,
            registry,
            include_dev=include_dev,
        )

    if not dependencies:
        info("No dependencies to install")
        return 0

    info("Resolving dependencies...")
    resolver = Resolver(registry)
    try:
        resolved = resolver.resolve(dependencies)
    except ResolveError as exc:
        raise GpmError(str(exc)) from exc

    modules_dir = root / MODULES_DIRNAME
    modules_dir.mkdir(parents=True, exist_ok=True)

    installed = 0
    for name in sorted(resolved):
        version = resolved[name]
        changed = install_one(
            registry,
            modules_dir,
            name,
            version,
            offline=getattr(args, "offline", False),
        )
        if changed:
            ok(f"{name}@{version} installed")
            installed += 1
        else:
            info(f"{name}@{version} already up to date")

    write_lockfile(root, resolved, registry)

    if args.packages and not getattr(args, "no_save", False):
        target = (
            manifest.dev_dependencies
            if getattr(args, "save_dev", False)
            else manifest.dependencies
        )
        for spec in args.packages:
            name, rng = parse_package_spec(spec)
            target[name] = rng if rng is not None else f"^{resolved[name]}"
        save_manifest(root, manifest)
        ok(f"{MANIFEST_NAME} updated")

    print()
    ok(f"Done: {len(resolved)} packages total, {installed} installed/updated")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    return _run_install(args, is_update=False)


def cmd_update(args: argparse.Namespace) -> int:
    return _run_install(args, is_update=True)


def cmd_remove(args: argparse.Namespace) -> int:
    root = find_project_root()
    manifest = load_manifest(root)

    removed: List[str] = []
    for raw_name in args.packages:
        name, _ = parse_package_spec(raw_name)
        found = False
        for table in (manifest.dependencies, manifest.dev_dependencies):
            if name in table:
                del table[name]
                found = True
        if found:
            removed.append(name)
        else:
            warn(f"'{name}' is not in the dependency list")

    if not removed:
        return 0

    save_manifest(root, manifest)

    for name in removed:
        target = root / MODULES_DIRNAME / name
        if target.exists():
            shutil.rmtree(target)
        ok(f"Removed {name}")

    remaining = dict(manifest.dependencies)
    for name, rng in manifest.dev_dependencies.items():
        remaining.setdefault(name, rng)

    if not remaining:
        lock = root / LOCK_NAME
        if lock.exists():
            lock.unlink()
            ok(f"Deleted {LOCK_NAME}")
        return 0

    try:
        registry = load_registry(
            resolve_registry_url(args),
            offline=getattr(args, "offline", False),
        )
        resolved = Resolver(registry).resolve(remaining)
    except GpmError as exc:
        warn(
            f"Failed to re-resolve dependencies, skipping lockfile update "
            f"and unused-package cleanup: {exc}"
        )
        return 0

    write_lockfile(root, resolved, registry)

    keep = set(resolved)
    modules_dir = root / MODULES_DIRNAME
    if modules_dir.is_dir():
        for child in modules_dir.iterdir():
            if child.is_dir() and child.name not in keep:
                shutil.rmtree(child)
                info(f"Cleaned up unused package {child.name}")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = find_project_root()
    manifest = load_manifest(root)
    lock = load_lockfile(root)

    print(f"{_c(manifest.name, '1')} {manifest.version}")
    print()

    if not manifest.dependencies and not manifest.dev_dependencies:
        info("No dependencies declared")
        return 0

    resolved = (lock or {}).get("packages", {})

    def render(table: Dict[str, str], title: str) -> None:
        if not table:
            return
        print(f"{_c(title, '36')}")
        for name in sorted(table):
            wanted = table[name]
            entry = resolved.get(name, {})
            actual = entry.get("version")
            if actual:
                line = (
                    f"  {name} {_c(actual, '32')}  "
                    f"{_c('(' + wanted + ')', '90')}"
                )
            else:
                line = (
                    f"  {name} {_c('(not installed)', '33')}  "
                    f"{_c('(' + wanted + ')', '90')}"
                )
            print(line)
            for dep, rng in sorted(entry.get("dependencies", {}).items()):
                print(f"      └─ {dep} {_c(rng, '90')}")
        print()

    render(manifest.dependencies, "dependencies")
    render(manifest.dev_dependencies, "devDependencies")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    registry = load_registry(
        resolve_registry_url(args), offline=getattr(args, "offline", False)
    )
    hits = registry.search(args.query)

    if not hits:
        info(f"No packages matching '{args.query}'")
        return 0

    for name, entry in hits:
        versions = registry.versions(name)
        latest = str(versions[0]) if versions else "-"
        desc = entry.get("description", "")
        print(
            f"{_c(name, '1')} {_c(latest, '32')}"
            + (f"  {_c(desc, '90')}" if desc else "")
        )
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    registry = load_registry(
        resolve_registry_url(args), offline=getattr(args, "offline", False)
    )
    name, _ = parse_package_spec(args.package)

    entry = registry.entry(name)
    versions = registry.versions(name)

    print(f"{_c(name, '1')}")
    if entry.get("description"):
        print(f"  {entry['description']}")
    if entry.get("homepage"):
        print(f"  {_c('homepage', '90')}  {entry['homepage']}")
    if entry.get("keywords"):
        print(
            f"  {_c('keywords', '90')}  "
            f"{', '.join(map(str, entry['keywords']))}"
        )
    print()

    if not versions:
        warn("No versions available")
        return 1

    print(f"{_c('versions', '36')}")
    for version in versions[: args.limit]:
        meta = registry.version_meta(name, version)
        deps = meta.get("dependencies", {})
        marker = " *" if version == versions[0] else "  "
        print(f" {marker} {version}")
        if deps:
            for dep, rng in sorted(deps.items()):
                print(f"        └─ {dep} {rng}")
    if len(versions) > args.limit:
        print(f"  ... {len(versions)} versions total")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config()

    if args.action == "list":
        if not config:
            info(f"No configuration set ({CONFIG_FILE} does not exist or is empty)")
            return 0
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0

    if args.action == "get":
        if not args.key:
            raise GpmError("'config get' requires a key")
        if args.key not in config:
            warn(f"Key '{args.key}' is not set")
            return 1
        value = config[args.key]
        if isinstance(value, str):
            print(value)
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0

    if args.action == "set":
        if not args.key or args.value is None:
            raise GpmError("'config set' requires a key and a value")
        if args.key == "registry":
            if not args.value.strip():
                raise GpmError("registry must be a non-empty string")
        config[args.key] = args.value
        save_config(config)
        ok(f"Set {args.key} = {args.value}")
        info(f"Saved to {CONFIG_FILE}")
        return 0

    raise GpmError(f"Unknown config action: {args.action}")


def _locate_sgc() -> str:
    sgc = os.environ.get("SGC", "sgc")
    sgc_path = Path(sgc)

    if sgc_path.exists():
        if sgc_path.is_dir():
            raise GpmError(
                f"SGC points to a directory, not an executable: {sgc}\n"
                f"Please set SGC to the full path of the compiler executable."
            )
        return str(sgc_path)

    found = shutil.which(sgc)
    if found:
        return found

    raise GpmError(
        f"Gallt compiler '{sgc}' not found.\n"
        f"Add the compiler to PATH, or set the SGC environment variable "
        f"to the full path of the compiler executable."
    )


def cmd_build(args: argparse.Namespace) -> int:
    root = find_project_root()
    manifest = load_manifest(root)

    entry = root / manifest.entry
    if not entry.is_file():
        raise GpmError(f"Entry file not found: {manifest.entry}")

    try:
        entry_data = entry.read_bytes()
    except OSError as exc:
        raise GpmError(
            f"Cannot read entry file {manifest.entry}: {exc}"
        ) from exc

    issues: List[str] = []
    if entry_data.startswith(b"\xef\xbb\xbf"):
        issues.append("UTF-8 BOM")
    if b"\r\n" in entry_data or b"\r" in entry_data:
        issues.append("CRLF/CR line endings")
    if issues:
        warn(
            f"{manifest.entry} contains formats that SGC does not accept "
            f"({', '.join(issues)}); compilation may fail; "
            f"run 'gpm fix-eol' to fix and retry"
        )

    resolved_sgc = _locate_sgc()

    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = root / output
    else:
        out_dir = root / "build"
        out_dir.mkdir(exist_ok=True)
        output = out_dir / manifest.name

    output.parent.mkdir(parents=True, exist_ok=True)

    extra = list(args.args or [])
    if extra and extra[0] == "--":
        extra = extra[1:]

    user_has_output = any(a in ("--output", "-o") for a in extra)

    command: List[str] = [
        resolved_sgc,
        "--compile",
        "--input",
        str(entry),
    ]
    if not user_has_output:
        command += ["--output", str(output)]
    command += extra

    display = " ".join(f'"{c}"' if " " in c else c for c in command)
    info("Running: " + display)

    try:
        rc = subprocess.call(command, cwd=str(root))
    except PermissionError as exc:
        raise GpmError(
            f"Permission denied when launching compiler: {resolved_sgc}\n"
            f"Possible causes: it's a directory, missing execute permission, "
            f"blocked by antivirus, or locked.\n"
            f"Original error: {exc}"
        ) from exc
    except OSError as exc:
        raise GpmError(f"Cannot launch compiler: {exc}") from exc

    if rc == 0 and not user_has_output:
        candidates = [output]
        if not output.suffix:
            candidates.append(output.with_suffix(".exe"))
        real = next((p for p in candidates if p.exists()), None)
        if real is not None:
            ok(f"Generated {real}")
        else:
            ok("Compilation completed")
    return rc


def cmd_clean(args: argparse.Namespace) -> int:
    root = find_project_root()

    modules = root / MODULES_DIRNAME
    if modules.is_dir():
        shutil.rmtree(modules)
        ok(f"Deleted {MODULES_DIRNAME}/")

    lock = root / LOCK_NAME
    if lock.exists():
        lock.unlink()
        ok(f"Deleted {LOCK_NAME}")

    build_dir = root / "build"
    if build_dir.is_dir() and args.build:
        shutil.rmtree(build_dir)
        ok("Deleted build/")

    if args.all and CACHE_DIR.is_dir():
        shutil.rmtree(CACHE_DIR)
        ok(f"Cleared cache {CACHE_DIR}")

    return 0


def cmd_fix_eol(args: argparse.Namespace) -> int:
    root = find_project_root()
    targets = list(root.rglob("*.glt"))

    if not args.all:
        modules = (root / MODULES_DIRNAME).resolve()
        filtered: List[Path] = []
        for p in targets:
            try:
                if modules in p.resolve().parents:
                    continue
            except OSError:
                pass
            filtered.append(p)
        targets = filtered

    if not targets:
        info("No .glt files found")
        return 0

    changed = 0
    for path in targets:
        if normalize_to_lf(path):
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path
            ok(f"Converted {rel}")
            changed += 1

    if changed:
        ok(f"Modified {changed} files")
    else:
        info("All .glt files are already LF (no BOM)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    problems = 0

    print(f"{_c('GPM', '1')} {GPM_VERSION}")
    print(f"  Gallt Home   {GALLT_HOME}")
    print(f"  Python       {sys.version.split()[0]}  ({sys.executable})")
    print()

    try:
        sgc = _locate_sgc()
        ok(f"Found Gallt compiler: {sgc}")
    except GpmError as exc:
        warn(str(exc))
        sgc = None
        problems += 1

    stdlib_env = os.environ.get("SGC_STDLIB") or os.environ.get("SGC_SL")
    if stdlib_env:
        if Path(stdlib_env).is_dir():
            ok(f"SGC standard library path (env): {stdlib_env}")
        else:
            warn(
                f"SGC_STDLIB / SGC_SL points to a non-existent path: {stdlib_env}"
            )
            problems += 1
    else:
        info(
            "SGC_STDLIB / SGC_SL not set "
            "(only needed if stdlib is not next to compiler)"
        )

    try:
        root = find_project_root()
        ok(f"Project root: {root}")
        manifest = load_manifest(root)
        ok(f"{MANIFEST_NAME} valid: {manifest.name} {manifest.version}")

        entry = root / manifest.entry
        if entry.is_file():
            data = entry.read_bytes()
            issues: List[str] = []
            if b"\r\n" in data or b"\r" in data:
                issues.append("CRLF line endings")
            if data.startswith(b"\xef\xbb\xbf"):
                issues.append("UTF-8 BOM")
            if issues:
                warn(
                    f"{manifest.entry} has SGC-incompatible formats: "
                    + ", ".join(issues)
                    + " (run gpm fix-eol to fix)"
                )
                problems += 1
            else:
                ok(
                    f"Entry file line-ending/encoding check passed: "
                    f"{manifest.entry}"
                )
        else:
            warn(f"Entry file not found: {manifest.entry}")
            problems += 1
    except GpmError as exc:
        info(str(exc))

    url = resolve_registry_url(args)
    print()
    info(f"Registry: {url}")
    try:
        registry = load_registry(url, offline=getattr(args, "offline", False))
        ok(f"Registry reachable, {len(registry.names())} packages")
    except GpmError as exc:
        warn(str(exc))
        problems += 1

    print()
    if problems:
        warn(f"Found {problems} problem(s)")
        return 1
    ok("All checks passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--registry",
        metavar="URL",
        help="Registry URL (http(s)://, file://, or local path)",
    )
    common.add_argument(
        "--offline",
        action="store_true",
        help="Offline mode, only use local cache",
    )

    parser = argparse.ArgumentParser(
        prog="gpm",
        description="Gallt package manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  gpm init myapp\n"
            "  gpm install\n"
            "  gpm install stdx@^1.2.0 mathx\n"
            "  gpm list\n"
            "  gpm build\n"
            "  gpm build -o dist/app -- --optimization-level 3\n"
            "  gpm config set registry ./index.json\n"
            "  gpm fix-eol\n"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"gpm {GPM_VERSION}"
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_init = sub.add_parser("init", help="Initialize a new Gallt package")
    p_init.add_argument(
        "name",
        nargs="?",
        help="Package name (defaults to current directory name; any valid UTF-8 allowed)",
    )
    p_init.add_argument(
        "--force", action="store_true", help="Overwrite existing gallt.json"
    )
    p_init.add_argument(
        "--editor-config",
        action="store_true",
        help="Also generate .gitattributes and .vscode/settings.json",
    )
    p_init.set_defaults(func=cmd_init)

    p_install = sub.add_parser(
        "install",
        parents=[common],
        help="Install dependencies (reads gallt.json when no args given)",
    )
    p_install.add_argument("packages", nargs="*", help="Package name or name@range")
    p_install.add_argument(
        "--save-dev", action="store_true", help="Save to devDependencies"
    )
    p_install.add_argument(
        "--no-save", action="store_true", help="Do not modify gallt.json"
    )
    p_install.add_argument(
        "--production",
        action="store_true",
        help="Only install dependencies, skip devDependencies",
    )
    p_install.set_defaults(func=cmd_install)

    p_update = sub.add_parser(
        "update",
        parents=[common],
        help="Update dependencies to latest compatible versions",
    )
    p_update.add_argument("packages", nargs="*", help="Empty means update all")
    p_update.add_argument(
        "--no-save", action="store_true", help=argparse.SUPPRESS
    )
    p_update.add_argument(
        "--save-dev", action="store_true", help=argparse.SUPPRESS
    )
    p_update.add_argument(
        "--production",
        action="store_true",
        help="Only update dependencies, skip devDependencies",
    )
    p_update.set_defaults(func=cmd_update)

    p_remove = sub.add_parser(
        "remove", parents=[common], help="Remove dependencies"
    )
    p_remove.add_argument("packages", nargs="+", help="Package names to remove")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", parents=[common], help="List dependencies")
    p_list.set_defaults(func=cmd_list)

    p_search = sub.add_parser("search", parents=[common], help="Search packages")
    p_search.add_argument("query", help="Search keyword")
    p_search.set_defaults(func=cmd_search)

    p_info = sub.add_parser(
        "info", parents=[common], help="Show package details"
    )
    p_info.add_argument("package", help="Package name or name@range")
    p_info.add_argument(
        "-n",
        "--limit",
        type=int,
        default=20,
        help="Max versions to show (default 20)",
    )
    p_info.set_defaults(func=cmd_info)

    p_config = sub.add_parser(
        "config", help="View or modify global configuration"
    )
    p_config.add_argument(
        "action",
        choices=["list", "get", "set"],
        help="Action to perform",
    )
    p_config.add_argument(
        "key", nargs="?", help="Configuration key (e.g. registry)"
    )
    p_config.add_argument("value", nargs="?", help="Value to set")
    p_config.set_defaults(func=cmd_config)

    p_build = sub.add_parser("build", help="Compile entry file via sgc")
    p_build.add_argument(
        "-o",
        "--output",
        help="Output executable path (default build/<package>)",
    )
    p_build.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Extra args passed to sgc (after --)",
    )
    p_build.set_defaults(func=cmd_build)

    p_clean = sub.add_parser(
        "clean", help="Clean install directory and lock file"
    )
    p_clean.add_argument(
        "--all", action="store_true", help="Also clear global cache"
    )
    p_clean.add_argument(
        "--build", action="store_true", help="Also delete build/ directory"
    )
    p_clean.set_defaults(func=cmd_clean)

    p_fix = sub.add_parser(
        "fix-eol", help="Normalize .glt line endings to LF and remove BOM"
    )
    p_fix.add_argument(
        "--all",
        action="store_true",
        help="Also process files under gallt_modules/",
    )
    p_fix.set_defaults(func=cmd_fix_eol)

    p_doctor = sub.add_parser(
        "doctor",
        parents=[common],
        help="Check local environment and registry availability",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    print("Gallt Package Manager 0.1.0 Preview")
    print("Copyright (c) Yimingfeishou")
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except GpmError as exc:
        error(str(exc))
        return 1
    except KeyboardInterrupt:
        error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
