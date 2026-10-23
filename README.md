# GPM / Gallt Package Manager

## English

### Overview

GPM is the package manager and project toolchain entry for Gallt. It handles project scaffolding, dependency resolution and installation, lockfile generation, source-file normalization, and invoking `sgc` to compile the entry file. It is a single-file Python script with no third-party dependencies. This version is experimental.

Current version: `0.1.0 Preview`.

SGC repository address: https://github.com/yimingfeishou/SGC

### Layout

- `Manifest`: `gallt.json` parsing and serialization
- `SemVer` / `VersionRange`: version parsing and range matching
- `Resolver`: backtracking dependency resolution
- `Registry`: registry fetch, caching, offline fallback
- `extract_archive`: archive extraction with path-traversal guard
- `cmd_*`: subcommands (`init`, `install`, `update`, `remove`, `list`, `search`, `info`, `config`, `build`, `clean`, `fix-eol`, `doctor`)
- `_locate_sgc`: compiler lookup via `SGC` env var or `PATH`

### Run

Requires Python 3.8+ and a working `sgc`. The compiler is located through the `SGC` environment variable (defaults to `sgc` on `PATH`). Gallt standard library must sit next to the compiler or be pointed to by `SGC_STDLIB` / `SGC_SL`.

```text
python gpm.py doctor
python gpm.py init myapp --editor-config
python gpm.py install stdx@^1.2.0
python gpm.py build
```

### Usage examples

```text
python gpm.py init <name> [--force] [--editor-config]
python gpm.py install [pkg[@range]...] [--save-dev] [--no-save] [--production] [--offline]
python gpm.py update [pkg...] [--production]
python gpm.py remove <pkg...>
python gpm.py list
python gpm.py search <query>
python gpm.py info <pkg> [-n <limit>]
python gpm.py config list|get|set [key] [value]
python gpm.py build [-o <path>] [-- <extra sgc args>]
python gpm.py clean [--build] [--all]
python gpm.py fix-eol [--all]
python gpm.py doctor
python gpm.py --version
```

Common flags: `--registry <url>` (overrides `GPM_REGISTRY` and config), `--offline` (cache-only).

### Notes

- This is an early preview release. Behavior and on-disk formats may still change.
- Default registry is a local `./index.json` created by `gpm init`; configure a real registry with `gpm config set registry <path-or-url>`, `GPM_REGISTRY`, or `--registry`.
- Cache lives under `$GALLT_HOME/cache/` (default `~/.gallt/cache/`). `gpm clean --all` clears it.
- `gpm build` is a thin launcher for `sgc`; it does not perform incremental builds or run build scripts. Pass extra sgc flags after `--`.
- SGC requires UTF-8 without BOM and LF line endings. `gpm build` warns on violations; `gpm fix-eol` normalizes `.glt` files.
- If the compiler executable has a non-default name, set `SGC` to its path (absolute path recommended).

---

## 简体中文

### 简介

GPM 是 Gallt 的包管理器与项目工具链入口，负责项目脚手架、依赖解析与安装、锁文件生成、源文件规范化，以及调用 `sgc` 编译入口文件。单文件 Python 脚本，无第三方依赖。该版本是实验性的。

当前版本：`0.1.0 Preview`。

SGC 仓库地址: https://github.com/yimingfeishou/SGC

### 目录结构

- `Manifest`：`gallt.json` 的解析与序列化
- `SemVer` / `VersionRange`：版本解析与范围匹配
- `Resolver`：回溯式依赖解析
- `Registry`：registry 拉取、缓存与离线回退
- `extract_archive`：归档解压，带路径穿越防护
- `cmd_*`：各子命令（`init`、`install`、`update`、`remove`、`list`、`search`、`info`、`config`、`build`、`clean`、`fix-eol`、`doctor`）
- `_locate_sgc`：通过 `SGC` 环境变量或 `PATH` 定位编译器

### 运行

需要 Python 3.8+ 与可用的 `sgc`。编译器通过 `SGC` 环境变量定位（默认在 `PATH` 中查找 `sgc`）。Gallt 标准库需与编译器同级，或由 `SGC_STDLIB` / `SGC_SL` 指定。

```text
python gpm.py doctor
python gpm.py init myapp --editor-config
python gpm.py install stdx@^1.2.0
python gpm.py build
```

### 用法示例

```text
python gpm.py init <name> [--force] [--editor-config]
python gpm.py install [pkg[@range]...] [--save-dev] [--no-save] [--production] [--offline]
python gpm.py update [pkg...] [--production]
python gpm.py remove <pkg...>
python gpm.py list
python gpm.py search <query>
python gpm.py info <pkg> [-n <limit>]
python gpm.py config list|get|set [key] [value]
python gpm.py build [-o <path>] [-- <额外 sgc 参数>]
python gpm.py clean [--build] [--all]
python gpm.py fix-eol [--all]
python gpm.py doctor
python gpm.py --version
```

通用参数：`--registry <url>`（覆盖 `GPM_REGISTRY` 与配置文件）、`--offline`（仅用缓存）。

### 注意事项

- 当前为早期预览版本，行为与磁盘格式可能仍会调整。
- 默认 registry 是 `gpm init` 生成的本地 `./index.json`；可通过 `gpm config set registry <路径或 URL>`、`GPM_REGISTRY` 或 `--registry` 切换。
- 缓存位于 `$GALLT_HOME/cache/`（默认 `~/.gallt/cache/`），`gpm clean --all` 会清空。
- `gpm build` 是 `sgc` 的薄启动器，不做增量编译、不执行 build script；额外 sgc 参数写在 `--` 之后。
- SGC 要求 UTF-8 无 BOM、LF 换行。`gpm build` 会预检并告警，`gpm fix-eol` 可批量修复 `.glt` 文件。
- 若编译器可执行文件不是默认名，将 `SGC` 设为它的路径（建议使用绝对路径）。
