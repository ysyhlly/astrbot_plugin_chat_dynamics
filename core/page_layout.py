"""One host-visible page containing all five internal sections."""
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

PAGE_ENTRY = 'app'
SECTIONS = ('console', 'config', 'memory', 'replay', 'learning')


def section_content_path(content_path: str, section: str) -> str:
    if section not in SECTIONS:
        raise ValueError('unsupported section')
    parts = urlsplit(content_path)
    if parts.scheme or parts.netloc or '/app/' not in parts.path:
        raise ValueError('invalid app entry')
    base = parts.path
    if base.endswith('/index.html'):
        base = base.rsplit('/', 1)[0] + '/'
    if not base.endswith('/'):
        raise ValueError('invalid app entry path')
    return urlunsplit(parts._replace(path=base + section + '/index.html'))


def retire_legacy_page_entries(plugin_root: Path) -> tuple[str, ...]:
    """An overlay upgrade may retain old indexes; preserve them outside discovery.

    Only the five previous entry files move. Data, configuration and all other
    assets stay in place. Normal Git/clean ZIP installs have nothing to migrate.
    """
    plugin_root = plugin_root.resolve()
    pages = plugin_root / 'pages'
    app = pages / PAGE_ENTRY
    if pages.is_symlink() or app.is_symlink() or any((app / name).is_symlink() for name in SECTIONS):
        return ()
    required = [app / 'index.html', app / 'entry.js',
                *(app / name / asset for name in SECTIONS for asset in ('index.html', 'app.js'))]
    if any(not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(plugin_root) for path in required):
        return ()
    legacy = [pages / name / 'index.html' for name in SECTIONS]
    legacy = [path for path in legacy if path.is_file()]
    if any(path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(plugin_root) for path in legacy):
        raise ValueError('legacy entry escapes plugin root')
    if not legacy:
        return ()
    backup = plugin_root / 'data' / 'page-entry-backups'
    if backup.is_symlink() or backup.parent.is_symlink() or not backup.resolve().is_relative_to(plugin_root):
        raise ValueError('page backup escapes plugin root')
    backup.mkdir(parents=True, exist_ok=True)
    moved = []
    for path in legacy:
        path.rename(backup / (path.parent.name + '-' + uuid4().hex + '.html'))
        moved.append(path.parent.name)
    return tuple(moved)
