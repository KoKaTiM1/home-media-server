"""Read-only browser checks using native OMV views and CLI-backed RPC fixtures.

Run with a Python environment containing Playwright and its Chromium browser.
No login credentials, browser sessions, or mutating RPCs are used.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

BASE='http://<SERVER_LAN_IP>'

async def rpc(route):
    payload=route.request.post_data_json
    service=payload.get('service',''); method=payload.get('method','')
    if method.startswith(('get','enumerate')):
        process=await asyncio.create_subprocess_exec(
            'omv-rpc','-u','admin',service,method,json.dumps(payload.get('params') or {}),
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,_=await process.communicate()
        if process.returncode:raise RuntimeError('Read-only RPC failed: '+service+'.'+method)
        try: response=json.loads(out)
        except ValueError: response=None
        await route.fulfill(json={'response':response,'error':None})
    else:
        # Native UI can save browser preferences; these checks never write them.
        await route.fulfill(json={'response':None,'error':None})

async def prepare(context):
    await context.add_init_script("sessionStorage.setItem('username','admin');sessionStorage.setItem('permissions',JSON.stringify({role:['admin']}));")
    await context.route('**/rpc.php',rpc)

async def desktop_layout(browser,enabled):
    context=await browser.new_context(viewport={'width':1440,'height':900})
    await prepare(context)
    if not enabled:
        async def empty(route):
            await route.fulfill(body='',content_type='text/css' if '.css' in route.request.url else 'application/javascript')
        await context.route('**/home-media-mobile.*',empty)
    page=await context.new_page()
    await page.goto(BASE+'/#/diagnostics/media-workers',wait_until='networkidle',timeout=60000)
    await page.wait_for_selector('.datatable-body-row',timeout=60000)
    await page.wait_for_timeout(600)
    assert await page.locator('.omv-phone-table-scroll').count()==0
    result=await page.evaluate("""() => [...document.querySelectorAll('ngx-datatable, .datatable-header-cell, .action-toolbar')].map(e=>{
        const c=getComputedStyle(e);return [e.tagName,Math.round(e.getBoundingClientRect().width),c.fontSize,c.padding,c.display,c.color];})""")
    await context.close()
    return result

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage'])
        context=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,device_scale_factor=1)
        await prepare(context)
        page=await context.new_page()
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        await page.goto(BASE+'/#/diagnostics/media-workers',wait_until='networkidle',timeout=60000)
        await page.wait_for_selector('ngx-datatable .datatable-body-row',timeout=60000)
        await page.wait_for_timeout(1500)
        print('Phone title:',await page.title(),flush=True)
        print('Phone layout:',await page.evaluate("""() => ({viewport:innerWidth, document:document.documentElement.scrollWidth,
            tables:[...document.querySelectorAll('.omv-phone-table-scroll')].map(e=>({width:e.clientWidth,scroll:e.scrollWidth})),
            headers:[...document.querySelectorAll('.datatable-header-cell')].map(e=>Math.round(e.getBoundingClientRect().width))})"""),flush=True)
        await page.screenshot(path='/root/omv-phone-tables.png',full_page=True)
        print('Page errors:',errors,flush=True)
        assert not errors
        assert await page.locator('.omv-phone-table-scroll').count()>0
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
        assert await page.locator('.omv-phone-table-scroll').first.evaluate('(e)=>e.scrollWidth>e.clientWidth')
        await page.locator('.omv-phone-table-scroll').first.evaluate('(e)=>e.scrollLeft=400')
        assert await page.locator('.omv-phone-table-scroll').first.evaluate('(e)=>e.scrollLeft>0')
        print('Phone table horizontal scrolling PASS',flush=True)
        for path,expected in (('/storage/shared-folders','AppData'),('/storage/filesystems','/dev/sda5')):
            await page.goto(BASE+'/#'+path,wait_until='networkidle',timeout=60000)
            await page.wait_for_function("text => [...document.querySelectorAll('.omv-phone-table-scroll .datatable-body-cell')].some(e=>e.textContent.includes(text))",arg=expected,timeout=60000)
            await page.wait_for_timeout(600)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
            print('Native phone view PASS:',path,flush=True)
            await page.screenshot(path='/root/omv-phone-'+path.rsplit('/',1)[1]+'.png',full_page=True)
        await page.set_viewport_size({'width':844,'height':390})
        await page.wait_for_timeout(600)
        assert await page.locator('.omv-phone-table-scroll').count()>0
        print('Phone landscape breakpoint PASS',flush=True)
        await page.set_viewport_size({'width':1440,'height':900})
        await page.wait_for_timeout(1200)
        assert await page.locator('.omv-phone-table-scroll').count()==0
        assert await page.locator('.omv-phone-table-hint').count()==0
        print('Return to desktop removes all wrappers/hints PASS',flush=True)
        await context.close()
        baseline=await desktop_layout(browser,False)
        enabled=await desktop_layout(browser,True)
        assert baseline==enabled,(baseline,enabled)
        print('Desktop geometry and styles match unmodified OMV PASS',flush=True)
        await browser.close()

if __name__=='__main__':asyncio.run(main())
