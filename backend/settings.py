"""Application-local settings and data paths; explicit overrides isolate tests."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def app_directory():
    if os.getenv('ASSETGROVE_APP_DIR'):
        return Path(os.environ['ASSETGROVE_APP_DIR']).resolve()
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def settings_path():
    override = os.getenv('ASSETGROVE_DATA_DIR')
    return (Path(override) if override else app_directory()) / 'settings.json'


def read_settings():
    path = settings_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'设置文件无法读取：{path}') from exc
    if not isinstance(value, dict):
        raise ValueError('设置文件格式错误')
    return value


def write_settings(value):
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def data_directory():
    override = os.getenv('ASSETGROVE_DATA_DIR')
    if override:
        return Path(override).resolve()
    configured = read_settings().get('data_directory')
    return Path(configured).resolve() if configured else app_directory() / 'data'


def legacy_directory():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'AssetGrove'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'AssetGrove'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'AssetGrove'
