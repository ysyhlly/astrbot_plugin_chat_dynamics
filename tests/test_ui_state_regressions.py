"""Execute shared UI state transitions with Node, without a live host."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT, check=True)


def test_missing_selected_session_remains_visible():
    run_js(r'''
import fs from 'node:fs';
import assert from 'node:assert/strict';
const source = fs.readFileSync('pages/shared/shell.js', 'utf8');
const fill = new Function('escapeHtml', source.slice(source.indexOf('export function fillSessionSelect')).replace('export ', '') + '; return fillSessionSelect;')(String);
const select = {};
fill(select, [{session_key:'new'}], 'old');
assert.match(select.innerHTML, /value="old" selected/);
assert.match(select.innerHTML, /当前未活跃/);
fill(select, [{session_key:'old'}], 'old');
assert.equal((select.innerHTML.match(/value="old"/g) || []).length, 1);
''')


def test_cancelled_navigation_restores_button_and_honors_guard():
    run_js(r'''
import fs from 'node:fs';
import assert from 'node:assert/strict';
let requested = 0, assigned = 0;
globalThis.document = {getElementById: () => null};
globalThis.window = {location: {search:'', href:'https://example.test/current', assign: () => assigned++},
  ChatDynamicsBeforeNavigate: () => false,
  AstrBotPluginPage: {apiGet: async () => {requested++; return {content_path:'/next?asset_token=fresh'};}}};
const source = fs.readFileSync('pages/shared/plugin_nav.js','utf8');
const module = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const button = {textContent:'Next', disabled:false, setAttribute(k,v){this[k]=v}, removeAttribute(k){delete this[k]}};
await module.navigateToPluginPage('today', button);
assert.equal(requested, 1);
assert.equal(button.disabled, false);
assert.equal(button['aria-busy'], undefined);
window.ChatDynamicsBeforeNavigate = () => true;
await module.navigateToPluginPage('today', button);
assert.equal(assigned, 1);
assert.equal(button.disabled, false);
assert.equal(button['aria-busy'], undefined);
''')


def test_manners_busy_controls_block_all_mutual_operations():
    run_js(r'''
import fs from 'node:fs';
import assert from 'node:assert/strict';
const source = fs.readFileSync('pages/manners/app.js','utf8');
const controls = {btnRefresh:{}, btnSavePresence:{}, presenceRange:{}};
const chips = [{}, {}];
const body = source.slice(source.indexOf('function updateControls()'), source.indexOf('// Per-chip'));
const update = new Function('els','document','busy','loading','online', body + '; updateControls();');
for (const [busy, loading] of [[true,false],[false,true]]) {
  update(controls, {querySelectorAll:()=>chips}, busy, loading, true);
  assert.ok([...Object.values(controls), ...chips].every(node=>node.disabled));
}
update(controls, {querySelectorAll:()=>chips}, false, false, true);
assert.ok([...Object.values(controls), ...chips].every(node=>!node.disabled));
''')


def test_console_presence_save_refreshes_with_operation_token():
    run_js(r'''
import fs from 'node:fs';
import assert from 'node:assert/strict';
const source = fs.readFileSync('pages/console/app.js', 'utf8');
const body = source.slice(source.indexOf('async function applyPresenceKnob()'), source.indexOf('async function loadNotebookLite()'));
let posted = false, refreshed = false, ended = false;
const apply = new Function('els','overviewOnline','operationInFlight','beginOperation','apiPost','refresh','endOperation','friendlyError', body + ';return applyPresenceKnob;')(
  {presenceSelect:{value:'lively'}, presenceNote:{}}, true, false,
  kind => {assert.equal(kind,'presence');return 7;},
  async (endpoint, payload) => {assert.equal(payload.config.presence_knob,'lively');posted=true;},
  async options => {assert.equal(posted,true);assert.equal(options.operationToken,7);refreshed=true;},
  (token, kind) => {assert.equal(token,7);assert.equal(kind,'presence');ended=true;}, String);
await apply();
assert.equal(refreshed,true);
assert.equal(ended,true);
''')
