"""Authoritative source for Discord slash command names + descriptions.

Extracted from source via AST (using inspect to locate files in any layout, e.g. container /app or host)
so it stays in sync with the actual @tree.command registrations
(per non-goal of not modifying slash commands themselves).
"""
from __future__ import annotations
import ast
import inspect
from pathlib import Path

def _extract_commands_from_file(path: Path) -> list[dict]:
    cmds: list[dict] = []
    try:
        src = path.read_text()
        tree = ast.parse(src, filename=str(path))
    except Exception:
        return cmds

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_tree_cmd = False
            is_bus_cmd = False
            if isinstance(func, ast.Attribute) and func.attr == 'command':
                if isinstance(func.value, ast.Name):
                    if func.value.id == 'tree':
                        is_tree_cmd = True
                    elif func.value.id == 'bus_group':
                        is_bus_cmd = True
                elif isinstance(func.value, ast.Attribute) and func.value.attr == 'command':
                    # chained like @bus_group.command
                    if isinstance(func.value.value, ast.Name) and func.value.value.id == 'bus_group':
                        is_bus_cmd = True
            if not (is_tree_cmd or is_bus_cmd):
                continue

            name = None
            desc = ''
            for kw in getattr(node, 'keywords', []):
                if kw.arg == 'name':
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        name = kw.value.value
                elif kw.arg == 'description':
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        desc = kw.value.value
            if name:
                if is_bus_cmd:
                    display = f"bus {name}"
                else:
                    display = name
                cmds.append({'name': display, 'description': desc})
    return cmds


def _slash_package_files() -> list[Path]:
    """Handler source files under bot/slash/ (peeled 8lx.3)."""
    files: list[Path] = []
    try:
        import slash as slash_pkg
        pkg_dir = Path(inspect.getfile(slash_pkg)).resolve().parent
    except Exception:
        pkg_dir = Path(__file__).resolve().parent / "slash"
        if not pkg_dir.is_dir():
            # container layout fallbacks
            for cand in (Path("/app/slash"), Path(__file__).resolve().parent.parent / "bot" / "slash"):
                if cand.is_dir():
                    pkg_dir = cand
                    break
    if not pkg_dir.is_dir():
        return files
    # 1od: real modules (*_cmds.py) hold @tree.command; legacy *_src.py if present
    for p in sorted(pkg_dir.glob("*_cmds.py")):
        files.append(p)
    if not files:
        for p in sorted(pkg_dir.glob("*_src.py")):
            files.append(p)
    if not files:
        for p in sorted(pkg_dir.glob("*.py")):
            if p.name == "__init__.py":
                continue
            files.append(p)
    return files


def get_all_slash_commands() -> list[dict]:
    """Return list of {'name': display_name, 'description': ...} for all non-exempt commands.
    Uses inspect to find the actual source files of 'bot', 'slash/*', and 'transit_discord'
    so it works in container (/app) or host layout.
    """
    files: list[Path] = []

    # Peeled slash handlers (primary after 8lx.3)
    files.extend(_slash_package_files())

    # Legacy / residual @tree.command on bot.py (should be empty after full peel)
    try:
        import bot
        bot_file = inspect.getsourcefile(bot)
        if bot_file:
            files.append(Path(bot_file))
    except Exception:
        pass
    try:
        import transit_discord
        t_file = inspect.getsourcefile(transit_discord)
        if t_file:
            files.append(Path(t_file))
    except Exception:
        pass

    # Fallbacks if inspect didn't find (e.g. not imported yet).
    # Prefer *_cmds.py (1od peel); keep *_src.py for legacy trees only.
    if not any(f.name.endswith("_cmds.py") or f.name.endswith("_src.py") for f in files):
        root = Path(__file__).resolve().parent.parent
        for cand in [
            root / 'bot' / 'slash',
            root / 'app' / 'slash',
            Path('/app/slash'),
        ]:
            if cand.is_dir():
                for p in sorted(cand.glob("*_cmds.py")):
                    files.append(p)
                if not any(f.name.endswith("_cmds.py") for f in files):
                    for p in sorted(cand.glob("*_src.py")):
                        files.append(p)
                break
    if not any(f.name == "bot.py" for f in files):
        root = Path(__file__).resolve().parent.parent
        for cand in [
            root / 'bot' / 'bot.py',
            root / 'app' / 'bot.py',
            Path('/app/bot.py'),
        ]:
            if cand.exists():
                files.append(cand)
                break
    if not any(f.name == "transit_discord.py" for f in files):
        root = Path(__file__).resolve().parent.parent
        for cand in [
            root / 'bot' / 'transit_discord.py',
            root / 'app' / 'transit_discord.py',
            Path('/app/transit_discord.py'),
        ]:
            if cand.exists():
                files.append(cand)
                break

    all_cmds: list[dict] = []
    seen_paths: set[Path] = set()
    for f in files:
        if not f or not f.exists():
            continue
        rp = f.resolve()
        if rp in seen_paths:
            continue
        seen_paths.add(rp)
        all_cmds.extend(_extract_commands_from_file(f))

    # Dedup and filter exempt
    seen = set()
    result = []
    for c in all_cmds:
        n = c['name']
        if n == 'shadow_mode' or n in seen:
            continue
        seen.add(n)
        result.append(c)

    # Ensure bus group entry if subs present
    has_bus_sub = any(c['name'].startswith('bus ') for c in result)
    if has_bus_sub and not any(c['name'] == 'bus' for c in result):
        result.append({'name': 'bus', 'description': 'Halifax Transit live GPS subcommands: help|route|near|track|stop'})

    # Sort for determinism (group bus first-ish)
    result.sort(key=lambda x: (0 if x['name'].startswith('bus') else 1, x['name']))
    return result

def list_slash_command_names() -> list[str]:
    return [c['name'] for c in get_all_slash_commands()]
