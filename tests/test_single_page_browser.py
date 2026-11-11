import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from .test_ui_theme_browser import ROOT, browser as browser, setup


@pytest.fixture
def scoped_page_server():
    prefix = '/api/v1/plugins/astrbot_plugin_chat_dynamics/views/app/_t/'

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if not re.match(re.escape(prefix) + r'(entry-token|fresh)/', self.path):
                self.send_error(403)
                return
            self.path = '/' + self.path[len(prefix):].split('/', 1)[1]
            super().do_GET()

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / 'pages' / 'app'), **kwargs)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}' + prefix
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('width', [390, 1366])
def test_single_entry_opens_console_and_all_internal_sections(browser, scoped_page_server, width):
    with browser.new_context(viewport={'width': width, 'height': 960}) as context:
        setup(context, {'ui': 'night'})
        context.add_init_script("""(() => {
          const api=window.AstrBotPluginPage,get=api.apiGet;
          api.apiGet=async(name,params)=>name!=='page_nav'?get(name,params):
            {ok:true,data:{content_path:'/api/v1/plugins/astrbot_plugin_chat_dynamics/views/app/_t/fresh/'+params.page+'/index.html?asset_token=fresh'}};
        })();""")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('response', lambda response: errors.append(response.url) if response.status >= 400 else None)
        page.goto(f'{scoped_page_server}entry-token/index.html?asset_token=entry-token&ui=night')
        page.wait_for_url('**/console/index.html?**')
        assert 'asset_token=entry-token' in page.url
        assert 'ui=night' in page.url
        for section in ('config', 'learning', 'memory', 'replay', 'console'):
            page.locator(f'[data-nav-page="{section}"]').click()
            page.wait_for_url(f'**/{section}/index.html?**')
            page.locator('#pluginSideNav [aria-current="page"]').wait_for()
            page.wait_for_load_state('networkidle')
            assert '/views/app/_t/fresh/' in page.url
            assert page.locator('#pluginSideNav [aria-current="page"]').count() == 1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert not errors
