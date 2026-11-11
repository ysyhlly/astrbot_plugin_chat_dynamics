from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pytest
from astrbot_plugin_chat_dynamics.core.page_layout import SECTIONS, section_content_path, retire_legacy_page_entries

ROOT = Path(__file__).resolve().parents[1]


def entries(root):
    # AstrBot discovers only direct page directories containing index.html.
    return sorted(path.name for path in (root / 'pages').iterdir()
                  if path.is_dir() and (path / 'index.html').is_file())


def test_one_host_entry_retains_all_five_internal_sections():
    assert entries(ROOT) == ['app']
    for section in SECTIONS:
        assert (ROOT / 'pages' / 'app' / section / 'index.html').is_file()
        assert (ROOT / 'pages' / 'app' / section / 'app.js').is_file()


@pytest.mark.parametrize('entry', [
    '/api/v1/plugins/p/views/app/_t/fresh/?asset_token=fresh&theme=dark',
    '/api/v1/plugins/p/pages/app/_t/fresh/index.html?asset_token=fresh',
    '/api/plugin/page/content/p/app/?asset_token=fresh',
])
@pytest.mark.parametrize('section', SECTIONS)
def test_internal_navigation_keeps_app_scope_and_signed_token(entry, section):
    result = section_content_path(entry, section)
    assert urlsplit(result).path.endswith('/' + section + '/index.html')
    assert '/app/' in urlsplit(result).path
    assert parse_qs(urlsplit(result).query)['asset_token'] == ['fresh']
    assert urlsplit(result).query == urlsplit(entry).query


@pytest.mark.parametrize('section', ['../config', 'drafts', '/config', 'app'])
def test_unregistered_sections_cannot_escape_scope(section):
    with pytest.raises(ValueError):
        section_content_path('/api/v1/plugins/p/views/app/_t/token/', section)


def test_overlay_upgrade_preserves_old_indexes_and_is_idempotent(tmp_path):
    app = tmp_path / 'pages' / 'app'
    app.mkdir(parents=True)
    (app / 'index.html').write_text('new entry')
    (app / 'entry.js').write_text('entry script')
    for section in SECTIONS:
        (app / section).mkdir()
        (app / section / 'index.html').write_text('new section')
        (app / section / 'app.js').write_text('section script')
        old = tmp_path / 'pages' / section
        old.mkdir()
        (old / 'index.html').write_text('old-' + section)
        (old / 'app.js').write_text('preserved asset')
    assert len(entries(tmp_path)) == 6
    assert set(retire_legacy_page_entries(tmp_path)) == set(SECTIONS)
    assert entries(tmp_path) == ['app']
    backups = list((tmp_path / 'data' / 'page-entry-backups').glob('*.html'))
    assert {p.read_text() for p in backups} == {'old-' + section for section in SECTIONS}
    assert all((tmp_path / 'pages' / section / 'app.js').read_text() == 'preserved asset' for section in SECTIONS)
    assert retire_legacy_page_entries(tmp_path) == ()


def test_partial_install_does_not_retire_working_old_entries(tmp_path):
    old = tmp_path / 'pages' / 'config'
    old.mkdir(parents=True)
    (old / 'index.html').write_text('working settings')
    assert retire_legacy_page_entries(tmp_path) == ()
    assert entries(tmp_path) == ['config']
