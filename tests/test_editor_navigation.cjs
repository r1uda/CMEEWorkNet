const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const handlers = {};
const form = {checkValidity:()=>true,reportValidity:()=>true};
const context = vm.createContext({
  console, crypto:require('node:crypto').webcrypto, structuredClone, URLSearchParams,
  setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,
  location:{hash:'#register'},history:{replaceState(){}},
  CustomEvent:class CustomEvent{constructor(type,init={}){this.type=type;this.detail=init.detail}},
  document:{styleSheets:[],documentElement:{dataset:{}},addEventListener(){},dispatchEvent(){return true},querySelector:s=>s==='#layout-switch'?{addEventListener(){}}:null,querySelectorAll:()=>[]},
  window:{addEventListener:(name,fn)=>handlers[name]=fn,scrollTo(){}},
  form,handlers,assert,
});
vm.runInContext(fs.readFileSync('app/static/app.js','utf8').replace(/^init\(\);\r?$/m,''),context);
vm.runInContext(`
  document.querySelector=s=>s==='#record-form'&&S.page==='register'?form:null;
  collectEditor=()=>structuredClone(S.editor);
  render=()=>{};closeModal=()=>{};reload=async()=>{};toast=()=>{};
  S.data={me:{id:'m1',caps:['can_register']},records:[],shifts:[],server_time:'2026-10-31T12:00:00+08:00'};
  S.params={};S.page='register';S.editor=initEditor();
  S.editor.work_mode='worked';S.editor.tasks=[{text:'写了一半',status:'pending'},{text:'',status:'pending'}];
  S.editor.notes='不能丢失';S.changeVersion=1;dirty();
`,context);
(async()=>{
  await vm.runInContext(`(async()=>{
    location.hash='#assistant';await handlers.hashchange();
    assert.equal(S.page,'assistant');assert.equal(S.editor,null);
    location.hash='#register';await handlers.hashchange();S.editor=restoreEditor();
    assert.equal(S.editor.work_mode,'worked');assert.equal(S.editor.tasks.length,2);
    assert.equal(S.editor.tasks[1].text,'');assert.equal(S.editor.notes,'不能丢失');assert.equal(S.dirty,true);
    S.editor.tasks.pop();S.changeVersion++;dirty();
    let resolve;api=()=>new Promise(r=>resolve=r);
    const saving=saveEditor('draft',true);
    location.hash='#schedule';await handlers.hashchange();
    resolve({id:'saved1',version:1,state:'draft',tasks:[{id:'t1',assignee_id:'m1'}]});
    await saving;
    assert.equal(location.hash,'#schedule');assert.equal(S.page,'schedule');assert.equal(S.editor,null);
    location.hash='#register';await handlers.hashchange();S.editor=restoreEditor();
    assert.equal(S.editor.id,'saved1');assert.equal(S.editor.tasks[0].text,'写了一半');
    assert.equal(S.dirty,false);
    S.data.me.id='m2';S.editor=null;assert.equal(restoreEditor().id,null);
    S.data.me.id='m1';S.editor=restoreEditor();
    api=async()=>({id:'saved1',version:2,state:'submitted',tasks:[{id:'t1'}]});
    await saveEditor('submitted');
    assert.equal(editorDrafts.size,0);assert.equal(location.hash,'records?id=saved1');
  })()`,context);
  console.log('PASS: incomplete form navigation, restore, save/navigation race, account isolation, submit cleanup');
})().catch(e=>{console.error(e);process.exitCode=1});
