const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8').replace(/^init\(\);\r?$/m,'');
const listeners={};
class CustomEvent{constructor(type,init={}){this.type=type;this.detail=init.detail}}
const context={assert,CustomEvent,document:{styleSheets:[],documentElement:{dataset:{}},querySelector:s=>s==='#layout-switch'?{addEventListener(){}}:null,querySelectorAll:()=>[],addEventListener(type,fn){(listeners[type]=listeners[type]||[]).push(fn)},dispatchEvent(event){(listeners[event.type]||[]).forEach(fn=>fn(event));return true}},window:{innerWidth:1280,addEventListener(){}},setInterval(){},setTimeout(){},clearTimeout(){},URLSearchParams,location:{hash:'#register'},navigator:{}};
vm.runInNewContext(source+`
S.boot={logged_in:true,demo:true};
S.data={me:{id:'m01',name:'测试成员',caps:['can_register','can_manage_members']},records:[],shifts:[],members:[],slots:[{name:'一二节',start:'08:00',end:'10:00'},{name:'三四节',start:'10:00',end:'12:00'},{name:'五六节',start:'14:00',end:'16:00'},{name:'七八节',start:'16:00',end:'18:00'}],server_time:'2026-10-30T20:00:00+08:00'};
for(const slot of [0,1,2,3]){
  S.data.shifts=[{id:'shift',date:'2026-10-30',slot,member_id:'m01'}];S.params={shift:'shift'};
  const r=initEditor();assert.equal(r.tasks.length,[0,3].includes(slot)?1:0);
  if(r.tasks.length)assert.equal(r.tasks[0].text,'日常卫生');
}
S.data.records=[{id:'record',author_id:'m01',shift_id:'shift',state:'submitted',work_mode:'no_work',tasks:[]}];
S.params={id:'record'};assert.equal(initEditor().tasks.length,0,'Existing no-work records must not gain hygiene tasks');
S.data.records[0].author_id='m02';assert.equal(initEditor(),null);
S.params.admin='1';assert.equal(initEditor().adminEditing,true);
S.data.me.caps=['can_register'];assert.equal(initEditor(),null);
assert.match(workloadPage(),/仅管理员/);
S.data.messages=[];S.data.settings={};S.page='records';S.params={};S.data.records=[];
assert.doesNotMatch(shell(records()),/href="#(?:manage|workload)"|data-action="record-import"|管理员修改/);
assert.match(manage(),/没有管理权限/);
S.data.me.caps=['can_register','can_manage_members'];
assert.match(shell(records()),/data-action="record-import"/);
assert.match(shell(records()),/href="#workload"/);

// 布局判定（手机 / 电脑 / 自动）
document.documentElement.dataset.layout='mobile';assert.equal(isMobileLayout(),true);
document.documentElement.dataset.layout='desktop';assert.equal(isMobileLayout(),false);
document.documentElement.dataset.layout='auto';window.innerWidth=430;assert.equal(isMobileLayout(),true);
window.innerWidth=1280;assert.equal(isMobileLayout(),false);

// 记录页只渲染一种形态
S.data.records=[{id:'r1',author_id:'m01',date:'2026-10-30',slot:null,state:'submitted',tasks:[],author_name:'测试'}];
document.documentElement.dataset.layout='desktop';
let tableHtml=recordsTable(S.data.records);
assert.match(tableHtml,/records-table-wrap/);assert.doesNotMatch(tableHtml,/records-card/);
document.documentElement.dataset.layout='mobile';
let cardHtml=recordsTable(S.data.records);
assert.match(cardHtml,/record-card/);assert.doesNotMatch(cardHtml,/records-table-wrap/);
document.documentElement.dataset.layout='auto';window.innerWidth=1280;

// 隐藏偏好
S.data.me.home_order=['notices','tasks'];
S.data.me.home_hidden=['recent','notices'];
assert.deepEqual([...homeHidden()],['recent','notices']);
assert.equal(homeOrder()[0],'notices');assert.equal(homeOrder().length,6);

// 未保存时禁止发起交接
S.data.handoffs=[];S.data.members=[];S.data.me.id='m01';
const handoffTask={id:'t1',status:'pending',assignee_id:'m01'};const submitted={state:'submitted'};
S.dirty=false;assert.equal(canHandoff(),true);
assert.doesNotMatch(taskHandoffPanel(handoffTask,submitted,0),/disabled/);
S.dirty=true;assert.equal(canHandoff(),false);
assert.match(taskHandoffPanel(handoffTask,submitted,0),/disabled/);
assert.match(taskHandoffPanel(handoffTask,submitted,0),/handoff-lock/);
S.dirty=false;
`,context);
console.log('Frontend checks passed: fixed first/last slots, existing no-work preservation, admin editor and stats visibility.');
