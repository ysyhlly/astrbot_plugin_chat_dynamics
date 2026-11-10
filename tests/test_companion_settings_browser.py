import pytest
from .test_config_simple_browser import setup_config
from .test_ui_theme_browser import browser as browser, page_server as page_server, ROOT


@pytest.mark.parametrize("width,theme", [(1366, "day"), (390, "night")])
def test_read_only_status_refresh_preserves_draft_and_handles_missing_interfaces(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 940}) as context:
        setup_config(context, theme)
        context.add_init_script("""(() => {
          const api=window.AstrBotPluginPage,get=api.apiGet;
          window.__integrationReads=0;
          api.apiGet=async(name,params)=>{
            if(name!=='integrations')return get(name,params);
            window.__integrationReads++;
            if(window.__integrationFail)throw new Error('offline');
            return {ok:true,data:{schema_version:1,blocking:['autonomous_blocking'],companions:[
              {id:'autonomous_blocking',name:'自主屏蔽',role:'成员策略',settings:'策略在自主屏蔽中设置',required:true,state:'missing',config_group:'boundaries'},
              {id:'livingmemory',name:'LivingMemory',role:'长期记忆',settings:'召回模型在 LivingMemory 中设置',state:'partial',version:'2.10.0',config_group:'memory'},
              {id:'portrayal',name:'<img src=x onerror=alert(1)>',role:'成员画像',settings:'只读',state:'ready',config_group:'memory'}]}};
          };
        })();""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html?ui={theme}")
        page.locator("#integrationSummary").filter(has_text="1 / 3").wait_for(state="attached")
        page.locator("#integrationPanel > summary").click()
        page.locator('[data-config-key="takeover_groups"]').fill("123,456")
        page.locator("#btnIntegrationRefresh").click()
        page.wait_for_function("window.__integrationReads===2")
        assert page.locator('[data-config-key="takeover_groups"]').input_value() == "123,456"
        assert page.evaluate("window.__savedConfig===undefined")
        assert "阻止角色回复" in page.locator('[data-integration="autonomous_blocking"]').inner_text()
        assert "需兼容接口" in page.locator('[data-integration="livingmemory"]').inner_text()
        assert page.locator("#integrationCards img").count() == 0
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / f"companion-settings-{width}-{theme}.png"), full_page=True)
        page.locator('#integrationPanel').screenshot(path=str(ROOT / 'output' / f'companion-panel-{width}-{theme}.png'))
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        page.evaluate("window.__integrationFail=true")
        page.locator("#btnIntegrationRefresh").click()
        page.locator("#integrationNote").filter(has_text="读取失败").wait_for()
        assert page.locator('[data-config-key="takeover_groups"]').input_value() == "123,456"
        assert not page.locator("#btnConfigSave").is_disabled()
        page.locator('[data-integration="autonomous_blocking"] button').click()
        assert page.locator('[data-config-key="autonomous_blocking_required"]').is_visible()
        assert not errors
