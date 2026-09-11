"""Synthetic DOM/protocol tests. No browser app, live API, credentials or model."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


APP = Path(__file__).resolve().parents[2] / "qingtian_engine" / "static" / "app.js"

HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8').replace(
  'refresh({force: true, quiet: true}).finally(connectEventStream);', '');
const group = process.argv[2], passed = [];
class Element {
  constructor(tag='div', root=false) {
    this.tagName=tag.toUpperCase(); this.root=root; this.children=[]; this.parentNode=null;
    this.dataset={}; this.attributes={}; this.listeners={}; this.className=''; this._text='';
    this.style={setProperty(){}}; this.value=''; this.open=false; this.disabled=false;
    this.classList={add:(x)=>{this.className+=' '+x;},remove:()=>{},toggle:()=>{},
      contains:(x)=>this.className.split(/\s+/).includes(x)};
  }
  get isConnected(){return this.root || !!this.parentNode?.isConnected;}
  set textContent(value){this._text=String(value??''); for(const c of this.children)c.parentNode=null; this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  append(...nodes){for(let node of nodes){if(typeof node==='string'){const t=new Element('text');t.textContent=node;node=t;}node.parentNode=this;this.children.push(node);}}
  replaceChildren(...nodes){this.textContent='';this.append(...nodes);}
  setAttribute(k,v){this.attributes[k]=String(v);}
  getAttribute(k){return this.attributes[k]??null;}
  removeAttribute(k){delete this.attributes[k];}
  addEventListener(k,fn){(this.listeners[k]??=[]).push(fn);}
  async emit(k,extra={}){const event={target:this,currentTarget:this,preventDefault(){},stopPropagation(){},...extra}; for(const fn of this.listeners[k]||[])await fn(event);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){const result=[];for(const c of this.children){if(selector.startsWith('.')?c.className.split(/\s+/).includes(selector.slice(1)):c.tagName===selector.toUpperCase())result.push(c);result.push(...c.querySelectorAll(selector));}return result;}
  showModal(){this.open=true;}
  close(){this.open=false;for(const fn of this.listeners.close||[])fn();}
  focus(){} scrollIntoView(){}
}
function env(){
  const nodes=new Map(), storage=new Map(), streams=[];
  const doc={querySelector:(s)=>{if(!nodes.has(s))nodes.set(s,new Element('div',true));return nodes.get(s);},
    querySelectorAll:()=>[],createElement:(tag)=>new Element(tag),createTextNode:(text)=>{const n=new Element('text');n.textContent=text;return n;},
    createDocumentFragment:()=>new Element('fragment'),createRange:()=>({selectNodeContents(){}}),addEventListener(){}};
  class Stream{constructor(url){this.url=url;this.listeners={};streams.push(this);}addEventListener(k,f){this.listeners[k]=f;}close(){this.closed=true;}emit(k,p,id){this.listeners[k]({data:JSON.stringify(p),lastEventId:id});}}
  const win={matchMedia:()=>({matches:true}),location:{href:''},confirm:()=>false,
    setTimeout:()=>1,setInterval:()=>1,addEventListener(){},getSelection:()=>({removeAllRanges(){},addRange(){}})};
  const c=vm.createContext({document:doc,window:win,sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
    navigator:{clipboard:{writeText:async()=>{}}},EventSource:Stream,FormData:class{},URL,Date,JSON,console,
    performance:{now:()=>0},requestAnimationFrame:()=>{},setTimeout:()=>1,clearTimeout(){},
    fetch:async()=>{throw Error('No real network allowed');},alert(){}});
  win.EventSource=Stream;vm.runInContext(source,c);return {c,doc,win,nodes,storage,streams};
}
const ID1='11111111-1111-4111-8111-111111111111',ID2='22222222-2222-4222-8222-222222222222';
const base=(extra={})=>({id:'fixture-task',title:'Synthetic task',state:'WAITING',execution_mode:'external',runs:[],evidence:[],events:[],...extra});
const bound=(id=ID1)=>base({evidence:[{kind:'thread',value:id,verified:1,created_at:'2026-09-10T01:00:00Z'}]});
const user=()=>base({action_owner_kind:'user',action_owner:'Fixture owner',action_text:'Provide the fixture approval scope.\nThen attach the synthetic report.',action_due:null,action_sensitive:0,action_version:'fixture-v1'});
const value=(c,code)=>vm.runInContext(code,c);
const button=(node,klass)=>node.querySelector('.'+klass);
async function check(name,fn){await fn();passed.push(name);}
(async()=>{
if(group==='bindings'){
 await check('only structured IDs, never task prose or owner role',()=>{const {c}=env();assert.equal(c.taskConversations(base({title:ID1,owner_session:ID1,events:[{summary:ID1}]})).choices.length,0);assert.equal(c.conversationUrl('javascript:alert(1)'),'');assert.equal(c.conversationUrl(ID1+'?resume=true'),'');});
 await check('unique verified external binding',()=>{const {c}=env();assert.equal(c.taskConversations(bound()).selected,ID1);});
 await check('ambiguous bindings require selection',()=>{const {c}=env();const t=bound();t.evidence.push({kind:'thread',value:ID2,verified:1});assert.equal(c.taskConversations(t).selected,'');assert.equal(c.taskConversations(t).choices.length,2);});
 await check('pending invalid newer registration never defaults old owner',()=>{const {c}=env();const t=bound();t.evidence.push({kind:'thread',value:'invalid',verified:0,created_at:'2026-09-10T02:00:00Z'});const b=c.taskConversations(t);assert.equal(b.selected,'');assert(b.choices.every(x=>!x.current));});
 await check('canceled/imported entries history-only',()=>{const {c}=env();assert.equal(c.taskConversations({...bound(),state:'CANCELED'}).selected,'');assert.equal(c.taskConversations({...bound(),imported_from:'synthetic-archive'}).selected,'');});
 await check('same-attempt different run bindings are ambiguous',()=>{const {c}=env();const t=base({execution_mode:'managed',runs:[{attempt:2,status:'RUNNING',session_id:ID1},{attempt:2,status:'RUNNING',session_id:ID2}]});assert.equal(c.taskConversations(t).selected,'');});
 await check('view rereads same task and navigates without mutation',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;const t=bound(),calls=[];c.api=async(p,o)=>{calls.push([p,o]);return t;};c.renderTaskConversation(t);const host=doc.querySelector('#detailConversation'),open=button(host,'conversation-open');assert.equal(open.tagName,'BUTTON');assert.equal(open.getAttribute('href'),null);await open.emit('click');assert.equal(win.location.href,'codex://threads/'+ID1);assert.equal(calls.length,1);assert.equal(calls[0][1],undefined);});
 await check('changed binding cancels navigation and rerenders',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;c.api=async()=>bound(ID2);c.renderTaskConversation(bound());await button(doc.querySelector('#detailConversation'),'conversation-open').emit('click');assert.equal(win.location.href,'');assert(doc.querySelector('#detailConversation').textContent.includes('绑定已变化'));});
 await check('wrong task reply cannot navigate',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;c.api=async()=>({...bound(),id:'different-task'});c.renderTaskConversation(bound());await button(doc.querySelector('#detailConversation'),'conversation-open').emit('click');assert.equal(win.location.href,'');});
 await check('close during reread cancels navigation',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;let resolve;c.api=()=>new Promise(r=>resolve=r);c.renderTaskConversation(bound());const pending=button(doc.querySelector('#detailConversation'),'conversation-open').emit('click');doc.querySelector('#detailDialog').close();resolve(bound());await pending;assert.equal(win.location.href,'');});
 await check('clipboard denial leaves selectable ID and no navigation',async()=>{const {c,doc,win}=env();c.navigator.clipboard.writeText=async()=>{throw Error('denied');};c.renderTaskConversation(bound());const host=doc.querySelector('#detailConversation');await host.querySelectorAll('button').find(x=>x.textContent==='复制会话 ID').emit('click');assert(host.textContent.includes('手动复制'));assert(host.textContent.includes(ID1));assert.equal(win.location.href,'');});
}
if(group==='actions'){
 await check('concrete raw requirements beat stale projections',()=>{const {c}=env();const t={...user(),human_action:{owner_kind:'user',text:'STALE'},display_action:'GENERIC',operator_status:{next_action:'GENERIC NEXT'}};assert(c.humanActionSummary(t).includes('Provide the fixture'));assert(!c.humanActionSummary(t).includes('STALE'));const card=c.interventionCard(t);assert(card.textContent.includes(t.action_text));assert(card.textContent.includes('系统提示'));assert(!card.textContent.includes('我已完成，重新验证'));});
 await check('external concrete requirements beat generic health',()=>{const {c}=env();const t=base({human_action:{owner_kind:'external',owner:'Fixture vendor',text:'Install the fixture callback'},display_action:'heartbeat stale'});assert.equal(c.humanActionSummary(t),'等待 Fixture vendor：Install the fixture callback');});
 await check('cleared raw ownership never revives stale dashboard action',()=>{const {c}=env();const t={...user(),action_owner_kind:'none',action_text:'',human_action:{owner_kind:'user',text:'STALE'}};assert.equal(c.humanAction(t).owner_kind,'none');assert(!c.humanActionSummary(t).includes('STALE'));});
 await check('paused/planned/canceled tasks stay non-actionable',()=>{const {c}=env();for(const state of ['PAUSED','PLAN_ONLY','CANCELED']){const t={...user(),state};assert.equal(c.humanAction(t).owner_kind,'none');assert(c.humanActionSummary(t).includes('无需你处理'));}});
 await check('approval, info, external operation copy is explicit only',()=>{const {c}=env();for(const kind of ['approve','provide-info','external-operation'])assert.notEqual(c.humanAction({...user(),action_kind:kind}).kind,'unspecified');assert.equal(c.humanAction(user()).kind,'unspecified');assert(c.actionInstruction(c.humanAction({...user(),action_kind:'approve'})).includes('不授予'));});
 await check('fresh detail, not old dashboard, renders action requirements',async()=>{const {c,doc}=env();value(c,'dashboard='+JSON.stringify({tasks:[{...user(),human_action:{owner_kind:'user',text:'STALE'},display_action:'STALE'}]}));c.api=async()=>({...user(),action_text:'Fresh detail requirement'});await c.openDetail('fixture-task');const body=doc.querySelector('#detailBody');assert(body.textContent.includes('Fresh detail requirement'));assert(!body.textContent.includes('STALE'));});
 await check('reading action detail never writes or grants permission',async()=>{const {c}=env();const calls=[];c.api=async(p,o)=>{calls.push([p,o]);return user();};await c.openDetail('fixture-task');assert.equal(calls.length,1);assert.equal(calls[0][1],undefined);});
 await check('cancel acknowledgment preserves task',async()=>{const {c,doc}=env();doc.querySelector('#detailDialog').open=true;const calls=[];c.api=async(p,o)=>{calls.push([p,o]);return user();};const host=c.renderActionRequirement(user());doc.querySelector('#detailBody').append(host);await host.querySelector('button').emit('click');assert.equal(calls.length,1);assert(host.textContent.includes('已取消'));});
 await check('changed action never submits stale statement',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;win.confirm=()=>true;let posts=0;c.api=async(p,o)=>{if(o)posts++;return {...user(),action_text:'Changed requirement'};};const host=c.renderActionRequirement(user());doc.querySelector('#detailBody').append(host);await host.querySelector('button').emit('click');assert.equal(posts,0);assert(host.textContent.includes('已变化'));});
 await check('external and synthetic detail cannot complete a user action',()=>{const {c}=env();assert.equal(c.renderActionRequirement({...user(),action_owner_kind:'external'}).querySelector('button'),null);value(c,'engineReadOnly=true');assert(c.renderActionRequirement(user()).querySelector('button').disabled);});
 await check('explicit acknowledgment sends only the required CAS version',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;const calls=[];let confirmation='';win.confirm=(text)=>{confirmation=text;return true;};c.api=async(p,o)=>{calls.push([p,o]);return user();};c.refresh=async()=>{};c.openDetail=async()=>{};const host=c.renderActionRequirement(user());doc.querySelector('#detailBody').append(host);await host.querySelector('button').emit('click');assert.equal(calls.length,2);assert.equal(calls[1][1].method,'POST');assert.deepEqual(JSON.parse(calls[1][1].body),{expected_action_version:'fixture-v1'});assert(confirmation.includes('不授予权限'));assert(!calls[1][1].body.includes(user().action_text));});
 await check('missing version and sensitive actions explain the safe route',()=>{const {c}=env();for(const t of [{...user(),action_version:null},{...user(),action_sensitive:1}]){const host=c.renderActionRequirement(t);assert(host.querySelector('button').disabled);assert(host.textContent.includes('渠道'));}});
 await check('409 refreshes current requirements without claiming a write',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;win.confirm=()=>true;let notice='';c.api=async(p,o)=>{if(o){const error=Error('changed');error.status=409;throw error;}return user();};c.openDetail=async(id,n)=>{assert.equal(id,user().id);notice=n;};const host=c.renderActionRequirement(user());doc.querySelector('#detailBody').append(host);await host.querySelector('button').emit('click');assert(notice.includes('未写入'));assert(notice.includes('重新阅读'));});
 await check('uncertain POST leaves declaration disabled until reread',async()=>{const {c,doc,win}=env();doc.querySelector('#detailDialog').open=true;win.confirm=()=>true;c.api=async(p,o)=>{if(o)throw Error('connection lost');return user();};const host=c.renderActionRequirement(user());doc.querySelector('#detailBody').append(host);await host.querySelector('button').emit('click');assert(host.querySelector('button').disabled);assert(host.textContent.includes('勿重复提交'));});
}
if(group==='cursor'){
 await check('REST snapshot does not advance SSE cursor',()=>{const {c,streams,storage}=env();c.rememberVersion(420);c.connectEventStream();assert(streams[0].url.endsWith('lastEventId=0'));assert(!storage.has('qingtian-event-cursor'));});
 await check('420 synthetic events consume every delivered batch',()=>{const {c,streams}=env();const delivered=[];c.applyDashboard=(d,o)=>{delivered.push(...o.changes.map(x=>x.id));c.rememberVersion(d.version);};c.rememberVersion(420);c.connectEventStream();for(let start=1;start<=420;start+=50){const end=Math.min(start+49,420);const changes=Array.from({length:end-start+1},(_,i)=>({id:start+i}));streams[0].emit('dashboard',{version:420,dashboard:{version:420},changes,cursor:end,has_more:end<420},String(end));}assert.equal(delivered.length,420);assert.equal(new Set(delivered).size,420);assert.equal(value(c,'lastCursor'),420);});
 await check('event ID is authoritative over payload cursor',()=>{const {c,streams}=env();c.applyDashboard=()=>{};c.connectEventStream();streams[0].emit('snapshot',{version:999,dashboard:{version:999},cursor:400,changes:[]},'17');assert.equal(value(c,'lastCursor'),17);});
 await check('reset rewinds delivered cursor, not to full snapshot',()=>{const {c,streams}=env();c.applyDashboard=()=>{};c.rememberCursor(1000);c.connectEventStream();streams[0].emit('snapshot',{version:20,dashboard:{version:20},cursor:5,reset:true,changes:[]},'5');assert.equal(value(c,'lastCursor'),5);});
 await check('bad frame never advances consumed cursor',()=>{const {c,streams}=env();c.applyDashboard=()=>{};c.rememberCursor(7);c.connectEventStream();streams[0].emit('snapshot',{version:20,dashboard:{version:21},cursor:10,changes:[]},'10');assert.equal(value(c,'lastCursor'),7);});
 await check('render failure does not acknowledge unconsumed events',()=>{const {c,streams}=env();c.applyDashboard=()=>{throw Error('render');};c.connectEventStream();streams[0].emit('dashboard',{version:100,dashboard:{version:100},cursor:50,changes:[{id:50}]},'50');assert.equal(value(c,'lastCursor'),0);});
}
if(group==='onboarding'){
 await check('guide command display and copy cannot execute',async()=>{const {c,doc}=env();let copied='';c.navigator.clipboard.writeText=async(v)=>{copied=v;};c.renderManagerEntry({status:'partial',error_code:'scan_incomplete',onboarding:{action_steps:[{title:'Read only',detail:'Inspect the selected entry',command:'qingtian manager-entry inspect --thread-id FIXTURE_ID'}]}});const host=doc.querySelector('#managerEntry');assert(host.textContent.includes('scan_incomplete'));assert(host.textContent.includes('工作流是不同验收层级'));await host.querySelectorAll('button').find(x=>x.textContent==='复制命令（不执行）').emit('click');assert(copied.includes('inspect'));});
 await check('missing guide uses honest manual instructions',()=>{const {c,doc}=env();c.renderManagerEntry({status:'not_initialized'});const host=doc.querySelector('#managerEntry');assert(host.textContent.includes('manager-entry guide'));assert(host.textContent.includes('不要删除绑定或重复创建'));assert(!host.textContent.includes('开箱成功'));});
}
assert(passed.length>0);console.log(JSON.stringify({group,passed,count:passed.length,network_calls:0,model_turns:0}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


class DashboardJourneyTests(unittest.TestCase):
    def test_browser_helper_imports_under_formal_module_and_script_entrypoints(self):
        import sys
        root = APP.parents[2]
        for prefix, module in ((str(root), "tests.atlas.test_dashboard_journey"),
                               (str(Path(__file__).parent), "test_dashboard_journey")):
            with self.subTest(module=module):
                code = "import sys,importlib;sys.path.insert(0," + repr(prefix) + ");m=importlib.import_module(" + repr(module) + ");assert callable(m.evolution_browser_checker())"
                result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def run_group(self, group):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required for synthetic DOM checks")
        result = subprocess.run(
            [node, "-e", HARNESS, str(APP), group],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertGreater(receipt["count"], 0)
        print(json.dumps(receipt, ensure_ascii=True))

    def test_structured_session_navigation(self):
        self.run_group("bindings")

    def test_action_requirements_and_acknowledgment(self):
        self.run_group("actions")

    def test_sse_consumed_cursor_contract(self):
        self.run_group("cursor")

    def test_readonly_onboarding_guidance(self):
        self.run_group("onboarding")


def evolution_browser_checker():
    # CI/documentation run this as -m tests.atlas.test_dashboard_journey;
    # developers may also invoke the file directly. Neither entry may depend
    # on a coincidental tests/atlas PYTHONPATH injection.
    if __package__:
        from .browser_evolution import check_evolution_views
    else:
        from browser_evolution import check_evolution_views
    return check_evolution_views


def serve_browser_fixture(receipt_dir):
    """CI/browser entry: actual handlers and SQLite, no model/process dispatch.

    Prints one JSON address, accepts update/change/sensitive/pause/stop on stdin. All test data
    lives in its own temporary tree. This is not a synthetic JSON HTTP server.
    """
    import hashlib
    import os
    import sys
    import tempfile
    import threading
    import time
    from unittest.mock import patch
    from qingtian_engine.db import Database
    from qingtian_engine.intake import IntakeService
    from qingtian_engine.runner import RunManager
    from qingtian_engine.server import ControlPlaneHandler, LoopbackThreadingHTTPServer
    from qingtian_engine.service import ControlPlane

    receipt_dir = Path(receipt_dir).resolve()
    receipt_dir.mkdir(parents=True, exist_ok=True)
    ledger = {"actual_engine_handler": "qingtian_engine.server.ControlPlaneHandler",
              "actual_service": "qingtian_engine.service.ControlPlane", "sqlite_isolated": True,
              "model_calls": 0, "process_dispatch_attempts": 0, "requests": [], "mutations": []}
    repo = APP.parents[2]
    ledger["source_hashes_at_start"] = {
        name: hashlib.sha256((repo / "qingtian_engine" / name).read_bytes()).hexdigest()
        for name in ("service.py", "server.py", "runner.py", "db.py", "static/app.js")
    }

    def forbid_process(*_args, **_kwargs):
        ledger["process_dispatch_attempts"] += 1
        raise AssertionError("Browser fixture must never launch a process/model")

    with tempfile.TemporaryDirectory(prefix="qingtian-browser-engine-") as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        codex_home = root / "codex-home"
        codex_home.mkdir()
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home),
                                     "QINGTIAN_RECOVERY_ENABLED": "0",
                                     "QINGTIAN_INTAKE_PLANNER": "off"}), patch("subprocess.Popen", forbid_process):
            service = ControlPlane(Database(root / "control.sqlite3"), manager_entry_workspace=workspace)
            first = service.create_task(
                "合成资料提交要求", idempotency_key="browser-fixture-information", state="WAITING",
                blocking_reason="等待合成资料", action_owner_kind="user", action_owner="合成主责",
                action_text="请提供合成验收环境名称和测试范围。\n通过指定工单附上合成报告；不要上传凭据。\n下一步：主责核对资料后安排内部复核。",
            )
            second = service.create_task(
                "合成范围审批要求", idempotency_key="browser-fixture-approval", state="WAITING",
                blocking_reason="等待明确授权", action_owner_kind="user", action_owner="合成审批主责",
                action_text="请在原授权渠道明确合成变更的范围、窗口和预算边界。\n这里的处理声明不是批准；不执行迁移或部署。",
            )
            third = service.create_task(
                "合成外部回调等待", idempotency_key="browser-fixture-external", state="WAITING",
                blocking_reason="等待外部回调", action_owner_kind="external", action_owner="合成供应方",
                action_text="等待合成供应方配置回调地址，并在指定渠道回报结果。",
            )
            service.add_evidence(first["id"], "thread", "11111111-1111-4111-8111-111111111111", verified=True)
            ledger["fixture_task_ids"] = [first["id"], second["id"], third["id"]]

            class FixtureHandler(ControlPlaneHandler):
                def do_GET(self):
                    ledger["requests"].append({"method": "GET", "path": self.path})
                    # This HTTP fixture has no scheduler watchdog loop. Keep
                    # only its explicit fixture liveness stamp current.
                    FixtureHandler.watchdog_health["_last_success_monotonic"] = time.monotonic()
                    super().do_GET()

                def do_POST(self):
                    ledger["requests"].append({"method": "POST", "path": self.path})
                    super().do_POST()

            FixtureHandler.service = service
            class SyntheticAdmissionManager(RunManager):
                def validate_execution_parameters(self, task, resume=False):
                    if task["title"] != "Synthetic browser admission" or resume:
                        return super().validate_execution_parameters(task, resume)
                    return None

                def dispatch(self, task_id, prompt, resume=False, **kwargs):
                    task = service.get_task(task_id)
                    if task["title"] != "Synthetic browser admission" or resume or kwargs:
                        raise AssertionError("Only the dedicated synthetic admission fixture may queue")
                    run_id = "synthetic-browser-run"
                    service.db.execute("INSERT INTO runs(id,task_id,attempt,adapter,command_summary,status,created_at,model,reasoning,speed) VALUES(?,?,1,'synthetic-browser','no process/model','QUEUED','2001-01-01T00:00:00Z',?,?,?)",
                                       (run_id, task_id, task["model"], task["reasoning"], task["speed"]))
                    service.db.execute("UPDATE tasks SET state='QUEUED' WHERE id=?", (task_id,))
                    ledger["synthetic_admission_runs"] = ledger.get("synthetic_admission_runs", 0) + 1
                    return {"id": run_id}
            FixtureHandler.manager = SyntheticAdmissionManager(service, root)
            FixtureHandler.intakes = IntakeService(service, root)
            FixtureHandler.coordinator = None
            FixtureHandler.engine_mode = "manual"
            FixtureHandler.synthetic_tour = False
            FixtureHandler.workspace = str(workspace)
            FixtureHandler.watchdog_health = {"_last_success_monotonic": time.monotonic(),
                                             "last_success_at": "2026-09-10T00:00:00+00:00",
                                             "consecutive_errors": 0, "last_error_type": ""}
            server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            address = {"url": "http://127.0.0.1:{}".format(server.server_port),
                       "data_dir": str(root), "workspace": str(workspace),
                       "fixture_task_ids": ledger["fixture_task_ids"], "actual_engine": True}
            (receipt_dir / "server.json").write_text(json.dumps(address) + "\n")
            print(json.dumps(address), flush=True)
            try:
                for line in sys.stdin:
                    command = line.strip()
                    if command == "stop":
                        break
                    if command == "update":
                        service.set_human_action(first["id"], "user", owner="合成主责",
                                                 text="更新后的真实数据库要求：请补充合成设备型号。\n下一步：通过指定工单回报，不授权执行。")
                    elif command == "change":
                        service.set_human_action(first["id"], "user", owner="合成复核主责",
                                                 text="CAS 并发更新后的要求：请先核对合成设备型号与版本。")
                    elif command == "sensitive":
                        service.set_human_action(first["id"], "user", owner="合成安全主责",
                                                 text="请通过指定安全 Secret 入口处理合成凭据，由主责核验；不要提交真实秘密。", sensitive=True)
                    elif command == "pause":
                        service.transition(second["id"], "PAUSED", force=True, producer="browser-fixture",
                                           summary="合成旅程明确暂停，不派发执行")
                    else:
                        print(json.dumps({"error": "Only update, change, sensitive, pause, stop are supported"}), flush=True)
                        continue
                    current = service.dashboard_payload()
                    item = {"command": command, "version": current["version"],
                            "action_summary": current["action_summary"],
                            "first_action": service.get_task(first["id"])["action_text"],
                            "second_state": service.get_task(second["id"])["state"]}
                    ledger["mutations"].append(item)
                    print(json.dumps(item, ensure_ascii=False), flush=True)
            except KeyboardInterrupt:
                pass
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)
                ledger["server_thread_stopped"] = not worker.is_alive()
                ledger["final_action_summary"] = service.dashboard_payload()["action_summary"]
    ledger["temporary_tree_removed"] = True
    (receipt_dir / "engine-browser-ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"stopped": True, "temporary_tree_removed": True}), flush=True)


def run_browser_e2e(receipt_dir):
    """Mandatory real Chromium + current engine mode; missing dependencies fail."""
    import hashlib
    import os
    import queue
    import sys
    import tempfile
    import threading
    import time
    import traceback

    receipt_dir = Path(receipt_dir).resolve()
    receipt_dir.mkdir(parents=True, exist_ok=True)
    summary = {"schema_version": 1, "scope": "current-engine-browser-e2e", "status": "failed",
               "model_called": False, "native_destination_opened": False, "steps": [],
               "python": sys.version.split()[0], "stage": "dependency_preflight"}
    process = None
    browser = None
    stderr = None
    temporary = None
    lines = queue.Queue()
    output = []
    requests = []
    responses = []
    sse_frames = []
    errors = []
    try:
        # Unlike ordinary unit discovery, this mode must never skip a browser.
        from playwright.sync_api import sync_playwright, expect
        from importlib.metadata import version as dependency_version
        summary["playwright_version"] = dependency_version("playwright")

        summary["stage"] = "browser_launch"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            summary["browser_version"] = browser.version
            temporary = tempfile.TemporaryDirectory(prefix="qingtian-browser-driver-")
            isolated = Path(temporary.name).resolve()
            home = isolated / "home"
            home.mkdir()
            env = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP"}}
            env.update(HOME=str(home), CODEX_HOME=str(home / "codex"),
                       QINGTIAN_RECOVERY_ENABLED="0", QINGTIAN_INTAKE_PLANNER="off",
                       PYTHONUNBUFFERED="1")
            stderr = (receipt_dir / "server-stderr.log").open("w")
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.atlas.test_dashboard_journey", "--serve-fixture", str(receipt_dir)],
                cwd=str(APP.parents[2]), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr, text=True,
            )

            def collect():
                for line in process.stdout:
                    output.append(line)
                    try:
                        lines.put(json.loads(line))
                    except ValueError:
                        pass

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            summary["stage"] = "engine_startup"
            address = lines.get(timeout=15)
            if not address.get("actual_engine") or not address.get("url"):
                raise AssertionError("Actual engine fixture did not start")
            url = address["url"]
            summary["instance"] = address
            if int(url.rsplit(":", 1)[-1]) in {8765, 8766}:
                raise AssertionError("Reserved service port selected")
            ids = address["fixture_task_ids"]

            def command(name):
                process.stdin.write(name + "\n")
                process.stdin.flush()
                result = lines.get(timeout=8)
                if result.get("command") != name:
                    raise AssertionError("Fixture mutation acknowledgment mismatch")
                return result

            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(15000)
            cdp = page.context.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.on("Network.eventSourceMessageReceived", lambda event: sse_frames.append(event))
            page.on("request", lambda request: requests.append({"method": request.method, "url": request.url}))
            page.on("response", lambda response: responses.append({"status": response.status, "url": response.url}))
            page.on("pageerror", lambda error: errors.append(str(error)))
            # Test data and resources stay on this one owned loopback origin.
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort())
            summary["stage"] = "legacy_storage_first_snapshot"
            initial_snapshot = page.request.get(url + "/api/dashboard").json()
            boundary = browser.new_page(viewport={"width": 1440, "height": 1000})
            boundary.set_default_timeout(15000)
            boundary_cdp = boundary.context.new_cdp_session(boundary)
            boundary_cdp.send("Network.enable")
            boundary_cdp.on("Network.eventSourceMessageReceived", lambda event: sse_frames.append({**event, "test_page": "storage_boundary"}))
            boundary.on("request", lambda request: requests.append({"method": request.method, "url": request.url, "test_page": "storage_boundary"}))
            boundary.add_init_script("""if (!sessionStorage.getItem('qingtian-test-storage-seeded')) {
              sessionStorage.setItem('qingtian-event-version', %s);
              sessionStorage.setItem('qingtian-event-cursor', '1');
              sessionStorage.setItem('qingtian-test-storage-seeded', '1');
            }""" % json.dumps(str(initial_snapshot["version"])))
            # Abort REST only on this page: the first visible board must come
            # from an unmodified real SSE snapshot, not a JSON response mock.
            boundary.route("**/*", lambda route: route.abort() if route.request.url == url + "/api/dashboard"
                           or not route.request.url.startswith(url + "/") else route.continue_())
            boundary.goto(url, wait_until="domcontentloaded")
            expect(boundary.locator(".task-card")).to_have_count(3)
            expect(boundary.locator("#board")).to_contain_text("请提供合成验收环境名称")
            first_boundary = boundary.evaluate("({cursor: lastCursor, version: lastVersion, rendered: dashboard !== null})")
            if not first_boundary["rendered"] or first_boundary["version"] != initial_snapshot["version"]:
                raise AssertionError("Legacy stored version suppressed the first real snapshot")
            boundary.reload(wait_until="domcontentloaded")
            expect(boundary.locator(".task-card")).to_have_count(3)
            second_boundary = boundary.evaluate("({cursor: lastCursor, version: lastVersion, rendered: dashboard !== null})")
            boundary_requests = [r["url"] for r in requests if r.get("test_page") == "storage_boundary" and "/api/events/stream?" in r["url"]]
            if not second_boundary["rendered"] or "lastEventId=" + str(first_boundary["cursor"]) not in boundary_requests[-1]:
                raise AssertionError("Reload with persisted cursor left an empty snapshot")
            summary["storage_boundary"] = {"rest_dashboard_blocked": True, "legacy_version_seed": initial_snapshot["version"],
                                           "first": first_boundary, "reload": second_boundary}
            summary["steps"].append("real_sse_first_snapshot_and_reload_ignore_legacy_snapshot_cursor")
            boundary.close()
            summary["stage"] = "initial_browser_journey"
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#taskViewButton").click()
            expect(page.locator("#interventionCount")).to_have_text("2")
            expect(page.locator("#runtimeMode")).to_contain_text("手动派发")
            expect(page.locator("#health")).to_have_text("实时")
            first_card = page.locator('.task-card[data-task-id="' + ids[0] + '"]')
            expect(first_card).to_contain_text("请提供合成验收环境名称")
            expect(page.locator('.intervention-card[data-task-id="' + ids[0] + '"]')).to_contain_text("不要上传凭据")
            page.screenshot(path=str(receipt_dir / "desktop-overview.png"), full_page=True, animations="disabled")
            summary["steps"].append("actual_sqlite_tasks_http_sse_desktop")

            first_card.click()
            expect(page.locator("#detailBody")).to_contain_text("请提供合成验收环境名称")
            expect(page.get_by_role("button", name="打开执行会话", exact=True)).to_be_enabled()
            expected_link = page.evaluate("conversationUrl('11111111-1111-4111-8111-111111111111')")
            if expected_link != "codex://threads/11111111-1111-4111-8111-111111111111":
                raise AssertionError("Structured navigation URL mismatch")
            summary["generated_navigation_url"] = expected_link
            summary["steps"].append("structured_binding_shown_destination_not_opened")
            page.screenshot(path=str(receipt_dir / "desktop-detail.png"), full_page=True, animations="disabled")
            page.get_by_role("button", name="关闭任务详情", exact=True).click()

            summary["stage"] = "sqlite_update_sse_refresh"
            command("update")
            expect(first_card).to_contain_text("更新后的真实数据库要求")
            expect(page.locator('.intervention-card[data-task-id="' + ids[0] + '"]')).to_contain_text("合成设备型号")
            first_card.click()
            expect(page.locator("#detailBody")).to_contain_text("更新后的真实数据库要求")
            summary["steps"].append("real_state_write_card_intervention_detail_refresh")

            summary["stage"] = "atomic_stale_action_conflict"
            def conflict_dialog(dialog):
                command("change")
                dialog.accept()
            page.once("dialog", conflict_dialog)
            page.get_by_role("button", name="提交处理声明，进入复核", exact=True).click()
            expect(page.locator("#detailBody")).to_contain_text("本次声明未写入")
            expect(page.locator("#detailBody")).to_contain_text("CAS 并发更新后的要求")
            detail = page.request.get(url + "/api/tasks/" + ids[0]).json()
            if detail["state"] != "WAITING" or detail["action_owner_kind"] != "user":
                raise AssertionError("409 changed action/state")
            if not any(r["status"] == 409 and r["url"].endswith("/complete-human-action") for r in responses):
                raise AssertionError("No real CAS 409 response observed")
            summary["steps"].append("real_cas_409_no_write_and_latest_requirement_visible")

            summary["stage"] = "explicit_handling_statement"
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="提交处理声明，进入复核", exact=True).click()
            expect(page.locator("#detailBody")).to_contain_text("无需你处理")
            detail = page.request.get(url + "/api/tasks/" + ids[0]).json()
            if detail["state"] != "VERIFYING" or detail["action_owner_kind"] != "none":
                raise AssertionError("Handling statement did not enter pending verification")
            summary["steps"].append("real_statement_enters_verifying_not_done_or_dispatch")
            page.get_by_role("button", name="关闭任务详情", exact=True).click()
            command("pause")
            expect(page.locator('.task-card[data-task-id="' + ids[1] + '"]')).to_contain_text("已暂停")
            expect(page.locator("#interventionCount")).to_have_text("0")
            summary["steps"].append("explicit_pause_updates_board_without_execution")

            summary["stage"] = "reconnect_and_mobile"
            consumed = page.evaluate("Number(sessionStorage.getItem('qingtian-event-cursor'))")
            page.reload(wait_until="domcontentloaded")
            page.locator("#taskViewButton").click()
            expect(page.locator("#health")).to_have_text("实时")
            expect(page.locator("#interventionCount")).to_have_text("0")
            stream_requests = [r["url"] for r in requests if "/api/events/stream?" in r["url"]]
            if not stream_requests or "lastEventId=" + str(consumed) not in stream_requests[-1]:
                raise AssertionError("Reconnect did not use the consumed-frame cursor")
            summary["steps"].append("real_sse_reconnect_uses_consumed_cursor")
            summary["stage"] = "render_failure_cursor_transaction"
            before_fault = page.evaluate("({cursor: lastCursor, stored: sessionStorage.getItem('qingtian-event-cursor'), version: lastVersion, snapshot: dashboard.version})")
            page.evaluate("""() => {
              const node = document.querySelector('#board');
              window.__qingtianFault = {node, parent: node.parentNode, next: node.nextSibling};
              node.remove();
            }""")
            try:
                command("update")
                expect(page.locator("#health")).to_have_attribute("class", "pill connection-status is-reconnecting")
                failed_render = page.evaluate("({cursor: lastCursor, stored: sessionStorage.getItem('qingtian-event-cursor'), version: lastVersion, snapshot: dashboard.version})")
                summary["render_fault"] = {"injection": "temporarily detached real board DOM; unmodified real SSE payload",
                                           "before": before_fault, "after_failure": failed_render}
                if failed_render["cursor"] != before_fault["cursor"] or failed_render["stored"] != before_fault["stored"]:
                    raise AssertionError("Failed rendering advanced the persistent event cursor")
                if failed_render["version"] != before_fault["version"] or failed_render["snapshot"] != before_fault["snapshot"]:
                    raise AssertionError("Failed rendering committed snapshot state before same-version retry")
            finally:
                page.evaluate("""() => {
                  const saved = window.__qingtianFault;
                  saved.parent.insertBefore(saved.node, saved.next);
                  delete window.__qingtianFault;
                }""")
            expect(first_card).to_contain_text("更新后的真实数据库要求")
            if page.evaluate("lastCursor") <= before_fault["cursor"]:
                raise AssertionError("Recovered snapshot did not commit its consumed cursor")
            summary["render_fault"]["recovered"] = page.evaluate("({cursor: lastCursor, stored: sessionStorage.getItem('qingtian-event-cursor'), version: lastVersion, snapshot: dashboard.version})")
            summary["steps"].append("real_sse_render_fault_preserves_cursor_and_rebuilds_same_snapshot")

            summary["stage"] = "injected_parse_failure"
            before_parse = page.evaluate("sessionStorage.getItem('qingtian-event-cursor')")
            page.evaluate("eventSource.dispatchEvent(new MessageEvent('dashboard', {data: '{', lastEventId: String(lastCursor + 999)}))")
            after_parse = page.evaluate("sessionStorage.getItem('qingtian-event-cursor')")
            if after_parse != before_parse:
                raise AssertionError("Malformed injected frame advanced the persistent cursor")
            summary["parse_fault"] = {"injection": "synthetic malformed MessageEvent into real browser listener; recovery uses real SSE",
                                      "before": before_parse, "after": after_parse}
            expect(page.locator("#health")).to_have_text("实时")
            summary["steps"].append("injected_parse_error_preserves_cursor_real_stream_recovers")
            summary["stage"] = "reconnect_and_mobile"
            command("sensitive")
            expect(page.locator("#interventionCount")).to_have_text("1")
            expect(first_card).to_contain_text("指定安全")
            first_card.click()
            expect(page.get_by_role("button", name="提交处理声明，进入复核", exact=True)).to_be_disabled()
            expect(page.locator("#detailBody")).to_contain_text("指定安全渠道")
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#detailConversation")).to_be_visible()
            summary["mobile_layout"] = page.evaluate("""() => ({
              innerWidth: window.innerWidth,
              scrollWidth: document.documentElement.scrollWidth,
              overflowing: [...document.querySelectorAll('body *')].map(node => {
                const box = node.getBoundingClientRect();
                return {tag: node.tagName, id: node.id, className: String(node.className),
                  left: box.left, right: box.right, width: box.width,
                  scrollWidth: node.scrollWidth, clientWidth: node.clientWidth};
              }).filter(box => box.width && (box.left < -1 || box.right > window.innerWidth + 1))
                .slice(0, 80)
            })""")
            page.screenshot(path=str(receipt_dir / "mobile-sensitive-detail.png"), full_page=True, animations="disabled")
            if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
                raise AssertionError("Mobile page has unexpected document-level overflow")
            summary["steps"].append("sensitive_route_explained_mobile_readable")
            page.get_by_role("button", name="关闭任务详情", exact=True).click()
            page.get_by_role("button", name="接入大管家", exact=True).click()
            expect(page.locator("#managerEntry")).to_contain_text("inspect")
            expect(page.locator("#managerEntry")).to_contain_text("工作流")
            # The real stream can replace command children during animation.
            # Resolve and scroll the child atomically from the stable host;
            # retain the actual viewport assertion instead of ignoring it.
            page.locator("#managerEntry").evaluate("""host => {
              const commands = host.querySelectorAll('.manager-command');
              if (!commands.length) throw new Error('No onboarding command');
              commands[commands.length - 1].scrollIntoView({block: 'center'});
            }""")
            expect(page.locator("#managerEntry .manager-command").last).to_be_in_viewport()
            page.screenshot(path=str(receipt_dir / "mobile-onboarding.png"), full_page=True, animations="disabled")
            summary["steps"].append("real_readonly_onboarding_projection_in_browser")
            summary["stage"] = "onboarding_interaction_stability"
            page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=url)
            command_text = page.locator("#managerEntry .manager-command").last.text_content()
            page.get_by_role("button", name="复制命令（不执行）", exact=True).last.click()
            expect(page.locator("#managerEntry .conversation-feedback").last).to_contain_text("命令已复制")
            copied = page.evaluate("async expected => (await navigator.clipboard.readText()) === expected", command_text)
            if not copied:
                raise AssertionError("Command clipboard content did not match the displayed command")
            before_interaction = page.locator("#managerEntry").evaluate("""host => {
              const code = [...host.querySelectorAll('.manager-command')].at(-1);
              const button = code.parentElement.querySelector('button');
              const range = document.createRange(); range.selectNodeContents(code);
              const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
              button.focus({preventScroll: true});
              window.__managerInteraction = {code, button, host};
              host.removeAttribute('data-test-frame-received');
              return {focused: document.activeElement === button, selected: selection.toString(),
                      hostScroll: host.scrollTop, dialogScroll: host.closest('dialog').scrollTop,
                      version: lastVersion, cursor: lastCursor};
            }""")
            page.screenshot(path=str(receipt_dir / "mobile-onboarding-interaction-before.png"), full_page=True, animations="disabled")
            # Reconnect the actual EventSource without changing engine data.
            # Observe the server's real frame after the production listener.
            page.evaluate("""() => {
              closeEventStream(); connectEventStream();
              for (const kind of ['snapshot', 'dashboard']) eventSource.addEventListener(kind, event => {
                const frame = JSON.parse(event.data);
                window.__managerInteraction.frame = {version: frame.version, cursor: frame.cursor};
                document.querySelector('#managerEntry').setAttribute('data-test-frame-received', 'yes');
              }, {once: true});
            }""")
            expect(page.locator("#managerEntry")).to_have_attribute("data-test-frame-received", "yes")
            after_interaction = page.locator("#managerEntry").evaluate("""host => ({
              focused: document.activeElement === window.__managerInteraction.button,
              originalCodeConnected: window.__managerInteraction.code.isConnected,
              originalButtonConnected: window.__managerInteraction.button.isConnected,
              selected: window.getSelection().toString(), hostScroll: host.scrollTop,
              dialogScroll: host.closest('dialog').scrollTop,
              feedback: [...host.querySelectorAll('.conversation-feedback')].at(-1).textContent,
              frame: window.__managerInteraction.frame
            })""")
            summary["onboarding_interaction"] = {"clipboard_matches_displayed_command": copied,
                                                  "before": before_interaction, "after": after_interaction}
            page.screenshot(path=str(receipt_dir / "mobile-onboarding-interaction-after.png"), full_page=True, animations="disabled")
            if after_interaction["frame"]["version"] != before_interaction["version"]:
                raise AssertionError("Onboarding stability probe did not receive the same snapshot version")
            if (not before_interaction["focused"] or not after_interaction["focused"]
                    or not after_interaction["originalCodeConnected"] or not after_interaction["originalButtonConnected"]
                    or after_interaction["selected"] != before_interaction["selected"]
                    or abs(after_interaction["hostScroll"] - before_interaction["hostScroll"]) > 1
                    or abs(after_interaction["dialogScroll"] - before_interaction["dialogScroll"]) > 1
                    or "命令已复制" not in after_interaction["feedback"]):
                raise AssertionError("Unchanged real SSE interrupted onboarding selection, focus, feedback, or scroll")
            # Explicit browser-only projection probe, not an account binding:
            # identical titles with changed copy targets must refresh closures.
            changed_id = "22222222-2222-4222-8222-222222222222"
            changed_command = "qingtian manager-entry inspect --thread-id " + changed_id
            projection = page.evaluate("""change => {
              closeEventStream();
              const entry = JSON.parse(JSON.stringify(dashboard.manager_entry));
              entry.thread_id = change.id;
              const commands = entry.onboarding.action_steps.filter(step => typeof step.command === 'string' && step.command);
              commands[commands.length - 1].command = change.command;
              renderManagerEntry(entry);
              return {oldButtonConnected: window.__managerInteraction.button.isConnected,
                      feedback: [...document.querySelectorAll('#managerEntry .conversation-feedback')].map(node => node.textContent)};
            }""", {"id": changed_id, "command": changed_command})
            expect(page.locator("#managerEntry .manager-command").last).to_have_text(changed_command)
            if projection["oldButtonConnected"] or any(projection["feedback"]):
                raise AssertionError("Changed copy target retained obsolete nodes or success feedback")
            page.get_by_role("button", name="复制命令（不执行）", exact=True).last.click()
            expect(page.locator("#managerEntry .manager-onboarding .conversation-feedback").last).to_contain_text("命令已复制")
            changed_command_copied = page.evaluate("async expected => (await navigator.clipboard.readText()) === expected", changed_command)
            page.get_by_role("button", name="复制已记录入口 ID", exact=True).click()
            expect(page.locator("#managerEntry .conversation-feedback").last).to_contain_text("已复制本地记录的 ID")
            changed_id_copied = page.evaluate("async expected => (await navigator.clipboard.readText()) === expected", changed_id)
            if not changed_command_copied or not changed_id_copied:
                raise AssertionError("Changed onboarding callback copied an obsolete target")
            summary["onboarding_interaction"]["changed_projection"] = {
                "injection": "browser-only alternate inert command and synthetic ID; no server/account binding write",
                "old_button_detached": not projection["oldButtonConnected"], "old_success_feedback_cleared": not any(projection["feedback"]),
                "new_command_copied": changed_command_copied, "new_id_copied": changed_id_copied,
                "permission_surface": "copy-only; no execution or authority-changing control",
            }
            page.evaluate("renderManagerEntry(dashboard.manager_entry); connectEventStream();")
            expect(page.locator("#managerEntry .manager-command").last).to_have_text(command_text)
            summary["steps"].append("real_same_snapshot_sse_preserves_copy_selection_focus_and_scroll")
            check_evolution_views = evolution_browser_checker()
            summary["evolution_driver_sha256"] = hashlib.sha256(Path(__file__).with_name("browser_evolution.py").read_bytes()).hexdigest()
            check_evolution_views(page, url, receipt_dir, summary, expect)
            network_frames = []
            delivered_ids = []
            for frame in sse_frames:
                if frame.get("eventName") not in {"snapshot", "dashboard"}:
                    continue
                payload = json.loads(frame["data"])
                if int(frame["eventId"]) != payload["cursor"] or payload["version"] != payload["dashboard"]["version"]:
                    raise AssertionError("Real SSE event ID, cursor, or snapshot version contract diverged")
                change_ids = [change["id"] for change in payload.get("changes", [])]
                if change_ids and (change_ids != sorted(set(change_ids)) or change_ids[-1] != payload["cursor"]):
                    raise AssertionError("Real SSE changes were not ordered through the delivered cursor")
                delivered_ids.extend(change_ids)
                network_frames.append({"cursor": payload["cursor"], "version": payload["version"],
                                       "change_ids": change_ids, "has_more": payload.get("has_more"),
                                       "reset": payload.get("reset")})
            if not network_frames or not delivered_ids:
                raise AssertionError("No real network changes captured")
            summary["sse_delivery"] = {"frames": network_frames, "distinct_delivered_change_ids": sorted(set(delivered_ids)),
                                       "consumer_contract": "current dashboard snapshot projection, not per-change business execution"}
            summary["steps"].append("real_network_frame_ids_cursor_and_snapshot_contract_recorded")
            if errors:
                raise AssertionError("Browser errors: " + "; ".join(errors))
            summary.update(status="passed", stage="complete", step_count=len(summary["steps"]))
            (receipt_dir / "browser-requests.json").write_text(json.dumps(requests, indent=2) + "\n")
            (receipt_dir / "browser-responses.json").write_text(json.dumps(responses, indent=2) + "\n")
            browser.close()
            browser = None
    except Exception as exc:
        summary.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        (receipt_dir / "failure.log").write_text(traceback.format_exc())
    finally:
        for filename, items in (("browser-requests.json", requests),
                                ("browser-responses.json", responses),
                                ("browser-sse-frames.json", sse_frames),
                                ("browser-errors.json", errors)):
            (receipt_dir / filename).write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if process is not None:
            try:
                if process.poll() is None:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                process.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            summary["fixture_exit_code"] = process.returncode
            (receipt_dir / "server-stdout.log").write_text("".join(output))
            ledger_path = receipt_dir / "engine-browser-ledger.json"
            if ledger_path.exists():
                ledger = json.loads(ledger_path.read_text())
                summary["source_hashes"] = ledger["source_hashes_at_start"]
                summary["cleanup"] = {k: ledger.get(k) for k in ("server_thread_stopped", "temporary_tree_removed", "process_dispatch_attempts")}
                if ledger.get("process_dispatch_attempts") != 0 or not ledger.get("temporary_tree_removed"):
                    summary.update(status="failed", cleanup_error="Unsafe or incomplete fixture cleanup")
            else:
                summary.update(status="failed", cleanup_error="Fixture ledger missing")
        if stderr is not None:
            stderr.close()
        if temporary is not None:
            temporary.cleanup()
        summary["test_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (receipt_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--serve-fixture":
        serve_browser_fixture(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "--browser-e2e":
        raise SystemExit(run_browser_e2e(sys.argv[2]))
    else:
        unittest.main()
