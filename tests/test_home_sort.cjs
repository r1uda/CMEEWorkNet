const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('app/static/app.js', 'utf8').split("const HOME_DEFAULT=")[1];
const events = {}, timers = new Map(); let timerId=0, hit=null;
const makeCard = id => ({dataset:{homeBlock:id},isConnected:true,classList:{add(){},remove(){}},getBoundingClientRect:()=>({left:0,top:id==='pending'?0:80,width:300,height:60})});
const first=makeCard('pending'),second=makeCard('next');let cards=[first,second];
const parent={insertBefore(card,target){cards=cards.filter(c=>c!==card);const index=target?cards.indexOf(target):cards.length;cards.splice(index,0,card)}};
for(const card of cards){card.parentElement=parent;Object.defineProperty(card,'nextSibling',{get:()=>cards[cards.indexOf(card)+1]||null})}
const handle={textContent:'待登记班次',closest:()=>first,setPointerCapture(){}};
const state={page:'home',homeSort:{owner:'m01',order:['pending','next']}};
const context=vm.createContext({S:state,myId:()=> 'm01',homeDOMOrder:()=>cards.map(c=>c.dataset.homeBlock),render(){},$(){return {textContent:''}},
  document:{addEventListener:(name,fn)=>events[name]=fn,createElement:()=>({style:{},remove(){}}),body:{appendChild(){}},elementFromPoint:()=>hit,querySelector:()=>null},
  window:{innerHeight:800,scrollBy(){}},setTimeout:fn=>{timers.set(++timerId,fn);return timerId},clearTimeout:id=>timers.delete(id),requestAnimationFrame:()=>1,cancelAnimationFrame(){}});
vm.runInContext('const HOME_DEFAULT='+source,context);
const down=()=>events.pointerdown({target:{closest:()=>handle},button:0,pointerId:1,clientX:40,clientY:30});
const move=(y)=>events.pointermove({pointerId:1,clientX:40,clientY:y,pointerType:'touch',preventDefault(){}});
const activate=()=>{const callbacks=[...timers.values()];timers.clear();callbacks.forEach(fn=>fn())};
down();assert.equal(vm.runInContext('homeDrag.active',context),false);
move(50);assert.equal(vm.runInContext('homeDrag',context),null);assert.equal(timers.size,0);
down();activate();assert.equal(vm.runInContext('homeDrag.active',context),true);
hit={closest:()=>second};move(130);vm.runInContext('moveHomeDrag(homeDrag)',context);
events.pointerup({pointerId:1});assert.deepEqual(cards.map(c=>c.dataset.homeBlock),['next','pending']);
assert.deepEqual(state.homeSort.order,['next','pending']);
down();activate();cards=[first,second];events.pointercancel({pointerId:1});
assert.deepEqual(state.homeSort.order,['next','pending']);assert.equal(vm.runInContext('homeDrag',context),null);
console.log('PASS: touch hold, early movement cancellation, reorder, pointer cancellation');
