#!/usr/bin/env node
/* Record the unmodified product UI; editorial text is composited afterwards. */
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const {spawn} = require('child_process');
const {chromium} = require('playwright');

const flags = Object.fromEntries(process.argv.slice(2).map((v,i,a)=>v.startsWith('--')?[v.slice(2),a[i+1]]:[]).filter(x=>x.length));
if (!flags.output) throw Error('--output is required (a new private run directory)');
const output = path.resolve(flags.output);
if (fs.existsSync(path.join(output, 'timeline.jsonl'))) throw Error('Refusing to overwrite a previous recording; choose a new output directory');
fs.mkdirSync(output, {recursive:true});
const pace = Number(flags.pace || 1);
const python = flags.python || process.env.PYTHON || 'python3';
const delay = ms => new Promise(r=>setTimeout(r,ms));
const bridge = spawn(python, [path.join(__dirname,'engine_bridge.py'),'--output',output], {stdio:['pipe','pipe','pipe']});
const errors = fs.createWriteStream(path.join(output,'bridge-stderr.log'));
bridge.stderr.pipe(errors);
let pending = [], buffered = [];
readline.createInterface({input:bridge.stdout}).on('line',line=>{
  const value=JSON.parse(line); if(pending.length) pending.shift()(value); else buffered.push(value);
});
const receive=()=>buffered.length?Promise.resolve(buffered.shift()):new Promise(r=>pending.push(r));
const commands=[],chapters=[],shots=[],cameras=[],sse=[],externalRequests=[],browserErrors=[];
let browser,context,page,aliases={},rawStart;
const elapsed=()=> (Date.now()-rawStart)/1000;
async function command(op,tasks=[],extra={}){
  const request={op,tasks,...extra};
  bridge.stdin.write(JSON.stringify(request)+'\n');
  const reply=await receive();
  if(!reply.ok) throw Error(JSON.stringify(reply));
  aliases=reply.aliases;
  commands.push({raw_seconds:elapsed(),request,entry:reply.entry});
  if(page && op!=='finish'){
    // Wait for the *native* SSE projection, not a page reload or DOM mutation.
    await page.waitForFunction(expected=>{
      return Object.entries(expected).every(([alias,state])=>{
        const card=[...document.querySelectorAll('[data-task-id]')].find(n=>n.querySelector('h3')?.textContent.startsWith(alias+' ·'));
        return card && card.closest('[data-state]')?.dataset.state===state.display;
      });
    },reply.entry.states,{timeout:12000});
  }
  return reply;
}
const ids=(a,b)=>Array.from({length:b-a+1},(_,i)=>'SC'+String(a+i).padStart(2,'0'));
async function hold(seconds){await delay(seconds*1000*pace);}
function chapter(number,title,subtitle,focus='wide'){
  const at=elapsed();
  if(chapters.length) chapters[chapters.length-1].end=at;
  chapters.push({number,title,subtitle,focus,start:at});
  console.log(JSON.stringify({chapter:number,title,raw_seconds:at.toFixed(2)}));
}
async function overview(){
  if(cameras.length && !cameras[cameras.length-1].end) cameras[cameras.length-1].end=elapsed();
  if(await page.locator('#detailDialog').evaluate(n=>n.open)) await page.locator('.close-detail').click();
  await page.locator('#search').fill('');
  await page.evaluate(()=>window.scrollTo({top:0,behavior:'smooth'}));
  await page.mouse.move(2160,80,{steps:12});
  await hold(.8);
}
async function detail(alias,technical=false){
  await overview();
  const card=page.locator(`#board [data-task-id="${aliases[alias]}"]`);
  await card.scrollIntoViewIfNeeded();
  await card.click();
  if(technical) await page.getByText('查看技术详情与证据',{exact:true}).click();
  await page.mouse.move(1810,120,{steps:12});
  const bounds=await page.locator('#detailDialog').boundingBox();
  cameras.push({start:elapsed(),kind:'detail',alias,technical,dialog_bounds:bounds,crop:{w:1140,h:530,x:550,y:0}});
  await hold(1);
}
async function screenshot(name,fullPage=false){
  const file=path.join(output,name+'.png');
  await page.screenshot({path:file,fullPage});
  shots.push({name,file,raw_seconds:elapsed()});
}
async function start(tasks){await command('start',tasks);await hold(1.4);}
async function finish(tasks){
  await command('verify',tasks);await hold(1.4);
  await command('fixture',tasks,{valid:true});
  await command('reconcile');await hold(1.8);
}
(async()=>{
  const ready=await receive();
  if(!ready.ready) throw Error('Bridge did not start');
  fs.writeFileSync(path.join(output,'instance.json'),JSON.stringify(ready,null,2));
  const executablePath=flags.chromium || process.env.SHOWCASE_CHROMIUM;
  browser=await chromium.launch({headless:true,...(executablePath?{executablePath}:{}),args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run']});
  context=await browser.newContext({viewport:{width:2240,height:1040},deviceScaleFactor:1,locale:'zh-CN',timezoneId:'Asia/Shanghai',
    recordVideo:{dir:path.join(output,'raw'),size:{width:2240,height:1040}}});
  await context.route('**/*',route=>{
    if(new URL(route.request().url()).origin!==ready.base){externalRequests.push(route.request().url());return route.abort();}
    return route.continue();
  });
  rawStart=Date.now();
  page=await context.newPage();
  page.on('pageerror',e=>browserErrors.push(String(e)));
  const cdp=await context.newCDPSession(page);await cdp.send('Network.enable');
  cdp.on('Network.eventSourceMessageReceived',ev=>sse.push({raw_seconds:elapsed(),eventName:ev.eventName,eventId:ev.eventId,data:ev.data}));
  await page.goto(ready.base,{waitUntil:'domcontentloaded'});
  await page.locator('#health.is-live').waitFor({timeout:15000});
  await page.evaluate(()=>document.fonts.ready);
  chapter('01','需求汇入同一处','32 条合成需求；只找大管家。由真实服务创建，不是修改页面状态。');
  await hold(2);
  for(let a=1;a<=25;a+=8){await command('create',ids(a,Math.min(a+7,32)));await hold(1.7);}
  await screenshot('01-intake');await hold(5);

  chapter('02','先规划，再决定执行','仅规划是授权策略的展示类别；依赖未完成时，下游保持等待。');
  await command('plan',[...ids(1,6),...ids(9,28)]);await hold(2);
  await command('dependency',['SC09'],{upstream:'SC01'});await hold(3);
  await detail('SC29');await hold(5);await overview();
  await start(ids(1,6));await hold(3);

  chapter('03','并发推进，各自留痕','多个本地 fixture 并行展示；心跳来自本地编排器，没有调用模型。');
  await start(ids(10,20));await start(ids(21,28));await hold(3);
  await finish(ids(1,3));await hold(2);
  await start(['SC09']);await hold(3);
  await screenshot('03-parallel');

  chapter('04','等待有原因，也有负责人','外部资料、用户决策分开显示；等待不是盲跑。所有信号均为合成。');
  await command('wait',ids(9,12),{kind:'external'});await hold(3);
  await command('wait',ids(13,16),{kind:'user'});await hold(3);
  await detail('SC10');await hold(7);await overview();await hold(3);
  await screenshot('04-waiting');

  chapter('05','只把必要决策交给你','需求发起人通过大管家确认；本片不演示真实宿主跨会话通信。');
  await detail('SC13');await hold(6);await overview();
  await command('authorize',['SC13','SC16']);await start(['SC13','SC16']);
  await finish(['SC13']);await command('verify',['SC16']);await hold(4);

  chapter('06','中断后，明确授权才续接','额度 / 环境停止信号仅为模拟；没有消耗额度，也不声称恢复真实额度。');
  await command('wait',ids(17,20),{kind:'recovery'});await hold(3);
  await detail('SC17');await hold(6);await overview();
  await command('authorize',['SC17','SC18']);await start(['SC17','SC18']);await hold(3);
  await finish(['SC17']);await hold(2);

  chapter('07','主动暂停，不等于普通等待','SC21、SC23 保持暂停；只有 SC22 收到明确的合成恢复授权。');
  await command('wait',ids(21,23),{kind:'pause'});await hold(4);
  await detail('SC21');await hold(5);await overview();
  await command('authorize',['SC22']);await start(['SC22']);await hold(4);

  chapter('08','没有证据，不能完成','真实 DONE 门禁拒绝缺证据请求；仅本地 JSON 合成验收，不等于业务 QA。');
  await command('verify',['SC24','SC25','SC26','SC27','SC28','SC06']);await hold(3);
  await command('gate_reject',['SC25']);
  await detail('SC25',true);await hold(7);await overview();
  await command('fixture',['SC25'],{valid:true});await command('reconcile');await hold(4);

  chapter('09','验收失败，返修再送验','SC24 实际只生成 8/10 项：校验失败；补齐后重验，证据链继续保留。');
  await command('fixture',['SC24','SC26','SC27'],{valid:false});
  await command('gate_reject',['SC24','SC26']);await hold(3);
  await command('repair',['SC24','SC27']);await hold(4);
  await command('verify',['SC24','SC27']);await hold(3);
  await command('fixture',['SC24'],{valid:true});await command('reconcile');
  await detail('SC24',true);await hold(7);await screenshot('09-evidence');await overview();

  chapter('10','完成与取消，都有来路','补齐资料后收口；重复、过期需求取消并留痕，不伪装成已完成。');
  await command('authorize',['SC09']);await start(['SC09']);await finish(['SC04','SC05','SC09']);
  await command('cancel',['SC31','SC32']);await hold(4);
  await detail('SC31');await hold(5);await overview();await hold(3);

  chapter('11','32 条需求，8 类去向','真实服务记录 → HTTP / SSE → 原生看板。手动编排；未调用模型、未执行生产业务。');
  await command('snapshot');await hold(5);
  await screenshot('actual-board-1080-source');
  await screenshot('actual-board-complete',true);
  // Native browser scroll reveals the remainder of the real long board.
  await page.mouse.wheel(0,660);await hold(5);
  await page.mouse.wheel(0,500);await hold(4);
  await overview();await hold(7);
  const end=elapsed();chapters[chapters.length-1].end=end;
  await command('finish');
  const video=page.video();await context.close();await browser.close();
  const raw=await video.path();
  fs.writeFileSync(path.join(output,'recording.json'),JSON.stringify({raw,raw_start_unix_ms:rawStart,raw_end_seconds:end,pace,
    disclosure:'真实引擎 · 合成示例 · 非生产执行',viewport:{width:2240,height:1040},chapters,shots,cameras,commands,
    sse_count:sse.length,external_requests_blocked:externalRequests,browser_errors:browserErrors,
    timing_note:'Node monotonic wall clock aligned at page creation; video timestamps may differ by < 1 second.'},null,2));
  fs.writeFileSync(path.join(output,'sse-events.json'),JSON.stringify(sse,null,2));
  if(browserErrors.length || externalRequests.length || !sse.length) throw Error('Browser/SSE verification failed');
  console.log(JSON.stringify({complete:true,output,raw,duration:end,sse_count:sse.length}));
})().catch(async error=>{
  fs.writeFileSync(path.join(output,'FAILED.txt'),error.stack+'\n');console.error(error);
  if(context) await context.close().catch(()=>{});if(browser) await browser.close().catch(()=>{});
  bridge.stdin.end();process.exitCode=1;
});
