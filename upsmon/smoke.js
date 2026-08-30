// Call every render function with realistic data and report anything that
// throws. A ReferenceError only shows up when its code path actually runs,
// which is exactly what a page like this makes easy to miss.
const fs = require('fs');
const made = {};
function mk(tag,id){ const n={ tagName:tag,id,className:'',style:{},dataset:{},type:'text',
  children:[],_text:'',_html:'',hidden:true,disabled:false,value:'',title:'',attrs:{},
  set textContent(v){this._text=String(v);},
  get textContent(){ return this._text || this.children.map(c=>c.textContent).join(' '); },
  set innerHTML(v){this._html=String(v); if(v==='')this.children=[];},
  get innerHTML(){return this._html;},
  appendChild(c){this.children.push(c);return c;}, removeChild(){},
  insertBefore(c){this.children.push(c);return c;},
  setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},
  removeAttribute(){},focus(){},addEventListener(e,f){(this._on=this._on||{})[e]=f;},
  getBoundingClientRect(){return{left:10,top:200,width:120,height:38,bottom:238};},
  offsetWidth:240,offsetHeight:60,
  classList:{_s:new Set(),toggle(c,o){o?this._s.add(c):this._s.delete(c);},
    add(c){this._s.add(c);},remove(c){this._s.delete(c);},contains(c){return this._s.has(c);}},
  querySelector(){return null;},querySelectorAll(){return [];},onclick:null };
  return n; }
global.document={getElementById:id=>made[id]||(made[id]=mk('div',id)),
  createElement:t=>mk(t),createTextNode:t=>{const x=mk('#text');x.textContent=t;return x;},
  querySelectorAll:()=>[],addEventListener:()=>{},hidden:false,body:{appendChild:c=>c}};
global.window={addEventListener:()=>{},innerWidth:1400};
global.location={hash:''}; global.history={replaceState:()=>{}};
global.setInterval=()=>1; global.clearInterval=()=>{}; global.clearTimeout=()=>{};
global.setTimeout=()=>1; global.requestAnimationFrame=f=>f();
global.confirm=()=>true;
global.fetch=()=>Promise.resolve({json:()=>Promise.resolve({ok:true,tests:[],events:[],outages:[],samples:[]})});

eval(fs.readFileSync('/tmp/dash.js','utf8') + `
global.__fns = { renderSnapshot, renderResetCard, renderWritable, renderCommands,
  renderAllVariables, renderLatestTest, renderTestSummary, renderTestChart,
  renderTestTable, renderTestButtons, drawChart, eventTable, setFavicon,
  commandButton, applyPowerFallback };
global.STATE = STATE;`);

const now = Math.floor(Date.now()/1000);
const vars = {
  'ups.status':'OL CHRG','battery.charge':'98','battery.runtime':'1500','ups.load':'14',
  'input.voltage':'234.0','output.voltage':'232.0','battery.voltage':'13.5',
  'battery.voltage.nominal':'12.0','device.model':'Back-UPS BX1200MI',
  'device.mfr':'American Power Conversion','device.serial':'9B2342A06103',
  'ups.realpower.nominal':'650','battery.type':'PbAc','ups.test.result':'Done and passed',
  'battery.charge.low':'20','battery.runtime.low':'120','ups.beeper.status':'disabled',
  'battery.mfr.date':'2026/08/29','input.transfer.low':'145','input.transfer.high':'295'
};
const snapshot = { generated: now, online:true, level:'ok', ups:'ups', version:'2.6.0',
  status_text:'on line (mains power), battery charging', vars, pin_required:true,
  descriptions:{'ups.status':'UPS status flags'},
  writable:{'battery.charge.low':'20'},
  thresholds:{charge_warn:50,charge_crit:25,load_warn:70,load_crit:90,
              runtime_warn_s:300,runtime_crit_s:120,battery_life_years:4},
  commands:[{name:'test.battery.start.quick',help:'Start a quick battery self test',dangerous:false},
            {name:'load.off',help:'Switch the outlets OFF immediately',dangerous:true}],
  battery_age:{source:'battery.mfr.date',raw:'2026/08/29',installed:'2026-08-29',days:1,years:0.0,life_years:4},
  allow_dangerous:false, outage:null };
const caps = { allow_dangerous:false, commands:snapshot.commands, fields:{
  'battery.charge.low':{value:'20',kind:'number',unit:'%',description:'Low battery threshold (percent)',max_length:10},
  'input.sensitivity':{value:'medium',kind:'select',options:['low','medium','high'],description:'Input sensitivity'},
  'battery.mfr.date':{value:'2026/08/29',kind:'date',format:'YYYY/MM/DD',description:'Battery date'}}};
const tests = [{id:1,started:now-300,finished:now-260,ups:'ups',command:'test.battery.start.quick',
  source:'dashboard',result:'Done and passed',passed:1,duration_s:40,on_battery_s:16,
  charge_start:99,charge_end:99,voltage_start:13.6,voltage_min:12.6},
  {id:2,started:now-90000,finished:now-89950,ups:'ups',command:'ups.self-test',source:'ups',
   result:'Done and passed',passed:1,duration_s:50,on_battery_s:12,
   charge_start:98,charge_end:98,voltage_start:13.5,voltage_min:13.1}];
const samples = Array.from({length:40},(_,i)=>({ts:now-(40-i)*60,status:'OL',charge:98,
  runtime:1500,load:14+(i%4),input_v:234,output_v:232,battery_v:13.5,realpower:null}));

STATE.snapshot = snapshot;
const cases = [
  ['renderSnapshot',     () => __fns.renderSnapshot(snapshot)],
  ['renderResetCard',    () => __fns.renderResetCard({samples:1200,events:4,tests:2,outages:1,db_bytes:4331520})],
  ['renderWritable',     () => __fns.renderWritable(caps)],
  ['renderCommands',     () => __fns.renderCommands(caps)],
  ['renderAllVariables', () => __fns.renderAllVariables(snapshot)],
  ['renderLatestTest',   () => __fns.renderLatestTest(tests[0])],
  ['renderLatestTest(none)', () => __fns.renderLatestTest(undefined)],
  ['renderTestSummary',  () => __fns.renderTestSummary(tests)],
  ['renderTestChart',    () => __fns.renderTestChart(tests)],
  ['renderTestTable',    () => __fns.renderTestTable(tests)],
  ['renderTestButtons',  () => __fns.renderTestButtons()],
  ['eventTable',         () => __fns.eventTable([{id:1,ts:now,ups:'ups',kind:'status',level:'info',detail:'OL'}])],
  ['drawChart',          () => __fns.drawChart(mk('div','probe'), {rows:samples,digits:0,
                                series:[{key:'charge',label:'Charge',color:'#3fb950'}]})],
  ['applyPowerFallback', () => { const s={rows:samples,series:[{key:'realpower',label:'P',color:'#000'}]};
                                 __fns.applyPowerFallback(s, samples); }],
  ['setFavicon',         () => __fns.setFavicon('warn')],
  ['commandButton',      () => __fns.commandButton(snapshot.commands[1], ()=>{})]
];
let failed = 0;
cases.forEach(([name, run]) => {
  try { run(); console.log('  ok    %s', name); }
  catch (e) { failed++; console.log('  FAIL  %s -> %s: %s', name, e.constructor.name, e.message); }
});
console.log(failed ? '\n  %d render function(s) threw' : '\n  every render function ran cleanly', failed);
