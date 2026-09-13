const { chromium } = require('playwright-core');
(async () => {
 const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];
  page.on('pageerror', e => errors.push(e.message));
  const url = process.env.STUDIO_MCP_TEST_URL;
  if (!url) throw Error('Set STUDIO_MCP_TEST_URL to an isolated Studio instance');
  for (let i=0;i<30;i++) {
    try { if ((await page.request.get(url+'_stcore/health',{timeout:1000})).ok()) break; } catch {}
    await page.waitForTimeout(1000);
  }
  await page.goto(url, {waitUntil:'domcontentloaded'});
  await page.getByTestId('stSidebar').getByText('MCP',{exact:true}).waitFor({timeout:90000});
  await page.getByTestId('stSidebar').getByText('MCP',{exact:true}).click();
  await page.getByText('MCP Fixture — тестовый сервер',{exact:true}).waitFor({timeout:30000});
  const previous = await page.getByText('Последняя проверка:',{exact:false}).first().innerText();
  await page.getByRole('button',{name:'Проверить',exact:true}).first().click();
  await page.waitForFunction(previous => [...document.querySelectorAll('[data-testid=stCaptionContainer]')].some(e=>e.innerText.startsWith('Последняя проверка:') && e.innerText !== previous && !e.innerText.includes('Ещё не выполнялась')), previous);
  await page.getByText('🟢 Работает',{exact:true}).waitFor({timeout:30000});
  const checked = await page.getByText('Последняя проверка:',{exact:false}).first().innerText();
  await page.waitForTimeout(3500);
  if (checked !== await page.getByText('Последняя проверка:',{exact:false}).first().innerText()) throw Error('Rerun launched another probe');
  if (process.env.STUDIO_MCP_SCREENSHOT) await page.screenshot({path:process.env.STUDIO_MCP_SCREENSHOT,fullPage:true});
  await page.getByTestId('stSidebar').getByText('Tools',{exact:true}).click();
  await page.getByRole('button',{name:'MCP Fixture',exact:true}).waitFor({timeout:30000});
  if (process.env.STUDIO_MCP_TEST_CREATE_TOOL === '1') await page.getByRole('button',{name:'MCP Fixture',exact:true}).click();
  const expander=page.getByText(/^MCP Fixture \(/).first();
  if (await expander.count()) {
    await expander.click();
    await page.getByText('MCP-сервер: MCP Fixture',{exact:false}).first().waitFor({timeout:15000});
  }
  if (errors.length) throw Error('Browser errors: '+errors.length);
  console.log(JSON.stringify({sidebar:true,fixture_green:true,rerun_no_probe:true,tools_page:true,js_errors:errors.length,checked}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exit(1)});
