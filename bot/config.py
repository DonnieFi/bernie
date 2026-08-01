import json
import os
import logging

log = logging.getLogger(__name__)

# Dynamic root detection: prefer /opt/family-bot if it exists (host mode), fallback to /app (container mode)
ROOT_DIR = "/opt/family-bot" if os.path.exists("/opt/family-bot/config.json") else "/app"
_CONFIG_PATH = os.environ.get("CONFIG_PATH", f"{ROOT_DIR}/config.json")
DOCS_ROOT = f"{ROOT_DIR}/docs" if ROOT_DIR == "/opt/family-bot" else "/docs"
WEB_ROOT = f"{ROOT_DIR}/web" if ROOT_DIR == "/opt/family-bot" else "/web"

_last_mtime = 0.0
try:
    _last_mtime = os.path.getmtime(_CONFIG_PATH)
except Exception:
    pass


def load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        cfg = json.load(f)
    from config_validate import validate_config_core

    validate_config_core(cfg)
    
    # Keys injected from environment — never written back to config.json
    cfg["bernie_api_token"] = os.environ.get("BERNIE_API_TOKEN")
    cfg["unifi_key"] = os.environ.get("UNIFI_KEY")
    
    # Credentials path: prefer host path if exists, else fallback to standard container mount
    default_gmail_token = f"{ROOT_DIR}/credentials/gmail_token.json" if ROOT_DIR == "/opt/family-bot" else "/credentials/gmail_token.json"
    cfg["gmail_token_file"] = os.environ.get("GMAIL_TOKEN_FILE", default_gmail_token)

    return cfg

# Derived from the env-injected keys above — kept adjacent so additions stay in sync
_ENV_KEYS = {"bernie_api_token", "unifi_key", "gmail_token_file"}


config = load_config()

from zoneinfo import ZoneInfo


def task_tz() -> ZoneInfo:
    """Halifax (or configured) zone — refreshed on reload_config()."""
    return ZoneInfo(config.get("timezone", "America/Halifax"))


TASK_TZ = task_tz()


def reload_config():
    global TASK_TZ
    new_cfg = load_config()
    config.clear()
    config.update(new_cfg)
    TASK_TZ = task_tz()
    try:
        global _last_mtime
        _last_mtime = os.path.getmtime(_CONFIG_PATH)
    except Exception:
        pass
    # family-bot-mu2.3 / 5hy.1: shared hygiene scan (report-only; does not block reload)
    try:
        from config_validate import validate_config

        for finding in validate_config(config):
            level = log.error if finding.get("severity") == "error" else log.warning
            if finding.get("severity") == "info":
                level = log.info
            level("config_validate [%s] %s", finding.get("code"), finding.get("message"))
    except Exception as exc:
        log.debug("config_validate after reload failed: %s", exc)
    log.info("Config reloaded.")
    return config


def check_and_reload_config_if_modified() -> bool:
    global _last_mtime
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
        if mtime > _last_mtime:
            _last_mtime = mtime
            reload_config()
            log.info("Config file modification detected on disk. Reloaded config.")
            return True
    except Exception:
        pass
    return False


def deep_merge(target: dict, source: dict) -> dict:
    """Recursively merges source into target in-place."""
    for k, v in source.items():
        if k in target and isinstance(target[k], dict) and isinstance(v, dict):
            deep_merge(target[k], v)
        else:
            target[k] = v
    return target


async def save_config():
    """DEPRECATED: Use update_config instead to prevent clobbering.
    
    Delegates to update_config for safety.
    """
    log.warning("save_config() is deprecated. Please use update_config() instead.")
    safe = {k: v for k, v in config.items() if k not in _ENV_KEYS}
    await update_config(safe)


async def update_config(updates: dict):
    """Safely updates config keys by reading disk state first to prevent clobbering.
    
    Acquires an exclusive file lock, merges updates recursively, and performs an atomic write.
    """
    if not updates:
        log.debug("update_config called with empty updates, skipping write.")
        return

    import asyncio
    import fcntl
    
    def _apply_and_write():
        lock_path = _CONFIG_PATH + ".lock"
        # Acquire an exclusive lock using a separate lock file
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            
            # 1. Read the current configuration from disk
            try:
                with open(_CONFIG_PATH, "r") as f:
                    content = f.read()
            except FileNotFoundError:
                log.warning(f"Config file {_CONFIG_PATH} not found, starting with empty dictionary.")
                content = "{}"
            
            # 2. Parse the configuration, aborting on invalid JSON
            try:
                disk_cfg = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError as e:
                log.error(f"Corrupt config JSON on disk: {e}. Content: {content!r}")
                raise ValueError(f"Corrupt config.json: {e}") from e
                
            # 3. If disk read is empty/incomplete (bind-mount race), seed from in-memory config
            if not disk_cfg.get("timezone"):
                log.warning(
                    "config.json on disk looks incomplete (%d keys); merging from in-memory config",
                    len(disk_cfg),
                )
                disk_cfg = {k: v for k, v in config.items() if k not in _ENV_KEYS}

            # 4. Apply the updates using recursive deep merge
            deep_merge(disk_cfg, updates)
            safe = {k: v for k, v in disk_cfg.items() if k not in _ENV_KEYS}

            from config_validate import validate_config_core

            validate_config_core(safe)

            # 5. Write back — try atomic rename, fall back to in-place
            #    for Docker file-level bind mounts (EBUSY on os.replace).
            tmp_path = _CONFIG_PATH + ".tmp"
            with open(tmp_path, "w") as tmp_f:
                json.dump(safe, tmp_f, indent=2)
            try:
                os.replace(tmp_path, _CONFIG_PATH)
            except OSError as e:
                import errno
                if e.errno == errno.EBUSY:
                    log.warning("os.replace failed (bind mount?), writing in-place")
                    with open(_CONFIG_PATH, "w") as f:
                        json.dump(safe, f, indent=2)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                else:
                    raise
            
            return safe
        
    safe_disk_cfg = await asyncio.get_running_loop().run_in_executor(None, _apply_and_write)

    # 6. Sync our local in-memory dict with the updated disk state
    config.clear()
    config.update(safe_disk_cfg)
    try:
        global _last_mtime
        _last_mtime = os.path.getmtime(_CONFIG_PATH)
    except Exception:
        pass
    
    # 6. Re-inject env keys so they remain available in memory
    config["bernie_api_token"] = os.environ.get("BERNIE_API_TOKEN")
    config["unifi_key"] = os.environ.get("UNIFI_KEY")
    default_gmail_token = f"{ROOT_DIR}/credentials/gmail_token.json" if ROOT_DIR == "/opt/family-bot" else "/credentials/gmail_token.json"
    config["gmail_token_file"] = os.environ.get("GMAIL_TOKEN_FILE", default_gmail_token)
    
    log.info(f"Config updated with {list(updates.keys())} and synced from disk.")
