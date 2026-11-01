"""Filesystem locations only. No authority, I/O to brokers, or process control."""
import json
from pathlib import Path

def data_root(code_root):
    root=Path(code_root).resolve()
    config=root/'runtime-storage.json'
    if not config.exists():
        return root  # Existing source checkouts retain their current layout.
    try:
        value=json.loads(config.read_text(encoding='utf-8'))
        if set(value)!={'schema','data_root'} or value['schema']!='REAL_RUNTIME_STORAGE_V1':
            raise ValueError()
        target=Path(value['data_root'])
        if not target.is_absolute() or '..' in target.parts:
            raise ValueError()
        target=target.resolve()
        if target==root or target.is_relative_to(root) or root.is_relative_to(target):
            raise ValueError()
        return target
    except (OSError,ValueError,TypeError):
        raise ValueError('REAL_RUNTIME_STORAGE_INVALID') from None


def postgres_bin(code_root):
    root=Path(code_root).resolve()
    if (root/'runtime-storage.json').exists():
        data_root(root)  # Configuration validation; executable location stays fixed.
        return root/'vendor/postgresql/bin'
    return root/'.local/postgresql/pgsql/bin'
