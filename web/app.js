'use strict';
const $=id=>document.getElementById(id);
let state=null,identity=null,poll=null,toastTimer,quickSeconds=3600,customUntil=false,windowKind='once',googleScript=null;
const busy=new Set();
const dayNames=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
function note(id,text,tone=''){const node=$(id);node.textContent=text;node.className='feedback '+tone;}
function toast(text,error=false){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').className=error?'error':'';toastTimer=setTimeout(()=>$('toast').textContent='',error?8000:4000);}
async function request(path,method='GET',body){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),70000);
 try{
  const response=await fetch(path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json',...(identity?.csrf?{'X-CSRF-Token':identity.csrf}:{})},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',signal:controller.signal});
  let data;try{data=await response.json();}catch{throw Error('The app is updating. Try again in a moment.');}
  if(!response.ok){if(response.status===401&&path.startsWith('/api/'))showLogin();throw Error(data.error||'That did not work. Please try again.');}
  return data;
 }catch(error){if(error.name==='AbortError')throw Error('This is taking longer than expected. Refresh before trying again.');if(error instanceof TypeError)throw Error('Could not reach Entry. Check your connection.');throw error;}finally{clearTimeout(timer);}
}
async function action(id,feedback,pending,fn){
 if(busy.has(id))return;const button=$(id),label=button.textContent;busy.add(id);button.disabled=true;button.setAttribute('aria-busy','true');if(!button.classList.contains('switch'))button.textContent=pending;if(feedback)note(feedback,pending,'progress');
 try{await fn();}catch(error){if(feedback)note(feedback,error.message,'error');toast(error.message,true);}finally{busy.delete(id);button.disabled=false;button.removeAttribute('aria-busy');if(!button.classList.contains('switch'))button.textContent=label;}
}
function zone(){return state?.settings.timezone||'Europe/London';}
function format(timestamp,options={}){return new Intl.DateTimeFormat('en-GB',{timeZone:zone(),...options}).format(new Date(timestamp*1000));}
function time(timestamp){return format(timestamp,{hour:'2-digit',minute:'2-digit'});}
function date(timestamp){return format(timestamp,{day:'numeric',month:'short'});}
function localInput(timestamp){
 const parts=new Intl.DateTimeFormat('en-GB',{timeZone:zone(),year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(timestamp*1000));
 const value=type=>parts.find(p=>p.type===type).value;return `${value('year')}-${value('month')}-${value('day')}T${value('hour')}:${value('minute')}`;
}
function windowText(window){
 if(window.kind==='once')return {title:`${time(window.start)} – ${time(window.end)}`,detail:date(window.start)+(date(window.start)===date(window.end)?'':` → ${date(window.end)}`)};
 const days=window.days.length===7?'Every day':window.days.map(d=>dayNames[d]).join(', ');
 return {title:`${window.start} – ${window.end}`,detail:days+(window.end<window.start?' · overnight':'')};
}
function render(s){
 state=s;const connected=s.connection==='listening';const entry=s.entry;
 $('app-view').hidden=false;$('login-view').hidden=true;$('settings-open').hidden=false;$('network-warning').hidden=true;
 $('entry-title').textContent=entry.active?'On':'Off';$('entry-toggle').setAttribute('aria-checked',String(entry.active));
 $('entry-subtitle').textContent=entry.active?`Until ${time(entry.until)}${date(entry.until)!==date(Date.now()/1000)?' on '+date(entry.until):''}`:entry.paused?'Your saved windows are paused.':entry.next_start?`Next on ${date(entry.next_start)} at ${time(entry.next_start)}`:'Only when you want it.';
 $('paused-note').hidden=!(entry.paused&&s.settings.windows.some(w=>w.enabled));
 $('entrance-name').textContent=s.devices.find(d=>String(d.id)===String(s.settings.device_id))?.name||s.devices[0]?.name||'Front entrance';
 $('connection-dot').className='dot '+(connected?'good':'error');
 $('connection-label').textContent=connected?'Connected to Ring':s.activity==='connecting'||s.connection==='connecting'?'Connecting to Ring…':s.authenticated?'Ring needs reconnecting':'Connect your Ring account in Settings';
 $('ring-detail').textContent=connected?'Your Ring session is saved and connected.':s.connection_error||(s.authenticated?'Your session is saved. Reconnect without signing in again.':'Sign in to Ring to connect your entrance.');
 $('timezone-label').textContent=zone()==='Europe/London'?'London time · adjusts for clock changes':zone().replaceAll('_',' ');
 $('window-zone').textContent=$('timezone-label').textContent;
 if(!Array.from($('timezone').options).some(o=>o.value===zone())){const option=document.createElement('option');option.value=zone();option.textContent=zone();$('timezone').append(option);}
 if(!$('settings-dialog').open)$('timezone').value=zone();
 renderWindows(s.settings.windows);
 $('events').replaceChildren();
 const visibleEvents=s.events.filter(e=>['unlock_acknowledged','failed'].includes(e.status)).slice(0,5);
 if(!visibleEvents.length){const text=document.createElement('p');text.className='empty';text.textContent='A little quiet here. Your recent entries will appear here.';$('events').append(text);}
 for(const event of visibleEvents){const row=document.createElement('div');row.className='event';const label=document.createElement('span');label.textContent=event.status==='unlock_acknowledged'?'Let in':'Unlock request failed';if(event.status==='failed')label.className='failure';const when=document.createElement('time');when.textContent=`${date(event.at)}, ${time(event.at)}`;row.append(label,when);$('events').append(row);}
 $('user-label').textContent=identity?.name?`Just for ${identity.name}`:'Private access';
 $('google-status').textContent=s.google.linked?`Connected · ${s.google.email}`:s.google.configured?'Setup saved. Sign in with Google below to finish linking your account.':'Replace the setup key with your Google account.';
 if(!$('google-email').value)$('google-email').value=s.google.email||'';
 if(!$('google-client').value)$('google-client').value=s.google.client_id||'';
 $('google-origins').textContent=Array.from(new Set([location.origin,'https://entry.damonfalck.me'])).join('\n');
}
function renderWindows(windows){
 $('windows').replaceChildren();const visible=windows.filter(w=>w.kind!=='once'||w.end>Date.now()/1000);
 if(!visible.length){const empty=document.createElement('p');empty.className='empty';empty.textContent='Set a time for visitors, or a weekly rhythm.';$('windows').append(empty);return;}
 for(const window of visible){
  const row=document.createElement('div');row.className='window-row';const copy=document.createElement('div');copy.className='window-copy';const title=document.createElement('strong'),detail=document.createElement('span');const text=windowText(window);title.textContent=text.title;detail.textContent=text.detail;copy.append(title,detail);
  const toggle=document.createElement('input');toggle.type='checkbox';toggle.className='mini-toggle';toggle.checked=window.enabled;toggle.setAttribute('aria-label',`${window.enabled?'Disable':'Enable'} ${text.detail} ${text.title}`);
  toggle.addEventListener('change',async()=>{toggle.disabled=true;try{await request('/api/windows/'+window.id,'PATCH',{enabled:toggle.checked});await refresh();toast(toggle.checked?'Window enabled.':'Window disabled.');}catch(error){toggle.checked=!toggle.checked;toast(error.message,true);}finally{toggle.disabled=false;}});
  const remove=document.createElement('button');remove.className='delete';remove.type='button';remove.textContent='×';remove.setAttribute('aria-label',`Remove ${text.detail} ${text.title}`);
  remove.addEventListener('click',async()=>{remove.disabled=true;try{await request('/api/windows/'+window.id,'DELETE');await refresh();toast('Window removed.');}catch(error){toast(error.message,true);remove.disabled=false;}});
  row.append(copy,toggle,remove);$('windows').append(row);
 }
}
async function refresh(){const s=await request('/api/status');render(s);return s;}
function startPolling(){clearInterval(poll);poll=setInterval(()=>{if(!document.hidden&&!busy.size)refresh().catch(()=>$('network-warning').hidden=false);},5000);}
async function enter(){identity=await request('/auth/me');if(!identity.signed_in){await showLogin();return;}await refresh();startPolling();}
async function loadGoogle(){if(window.google?.accounts?.id)return;if(!googleScript)googleScript=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://accounts.google.com/gsi/client';script.async=true;script.onload=resolve;script.onerror=()=>reject(Error('Google could not load. Check your connection and reload.'));document.head.append(script);});await googleScript;}
async function googleButton(target){
 const config=await request('/auth/config');
 if(!config.google_ready)return false;
 await loadGoogle();
 window.google.accounts.id.initialize({client_id:config.client_id,nonce:config.nonce,auto_select:false,callback:async response=>{
  try{note('login-feedback','Signing in…','progress');await request('/auth/google','POST',{credential:response.credential});identity=await request('/auth/me');$('settings-dialog').close();await enter();toast('Signed in with Google.');}
  catch(error){note('login-feedback',error.message,'error');toast(error.message,true);}
 }});
 $(target).replaceChildren();window.google.accounts.id.renderButton($(target),{type:'standard',theme:'outline',size:'large',shape:'pill',text:'continue_with',width:280});return true;
}
async function showLogin(){
 clearInterval(poll);identity=null;$('app-view').hidden=true;$('settings-open').hidden=true;$('login-view').hidden=false;
 const config=await request('/auth/config');$('setup-access').hidden=!config.allow_setup_key;$('google-setup-note').hidden=config.google_ready;
 if(config.google_ready)await googleButton('google-button');else $('setup-access').open=true;
}
$('key-form').addEventListener('submit',event=>{event.preventDefault();action('key-submit','key-feedback','Opening…',async()=>{await request('/auth/key','POST',{token:$('key').value.trim()});$('key').value='';await enter();});});
$('entry-toggle').addEventListener('click',()=>{if(state?.entry.active){action('entry-toggle',null,'',async()=>{await request('/api/disarm','POST',{});await refresh();toast('Auto-entry is off. Saved windows are paused.');});}else{note('quick-feedback','');$('until').value=localInput(Date.now()/1000+3600);$('quick-dialog').showModal();}});
$('resume').addEventListener('click',()=>action('resume',null,'Resuming…',async()=>{await request('/api/resume','POST',{});await refresh();toast('Saved windows resumed.');}));
for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>$(button.dataset.close).close());
for(const button of document.querySelectorAll('[data-seconds]'))button.addEventListener('click',()=>{quickSeconds=Number(button.dataset.seconds);customUntil=false;$('until-field').hidden=true;for(const b of document.querySelectorAll('[data-seconds]'))b.classList.toggle('selected',b===button);});
$('custom-until').addEventListener('click',()=>{customUntil=true;$('until-field').hidden=false;for(const b of document.querySelectorAll('[data-seconds]'))b.classList.remove('selected');$('until').focus();});
$('quick-form').addEventListener('submit',event=>{event.preventDefault();action('quick-submit','quick-feedback','Turning on…',async()=>{await request('/api/entry','POST',customUntil?{until:$('until').value}:{seconds:quickSeconds});await refresh();$('quick-dialog').close();toast('Auto-entry is on.');});});
function setKind(kind){windowKind=kind;$('once-fields').hidden=kind!=='once';$('weekly-fields').hidden=kind!=='weekly';$('once-tab').classList.toggle('selected',kind==='once');$('weekly-tab').classList.toggle('selected',kind==='weekly');$('once-start').required=kind==='once';$('once-end').required=kind==='once';}
$('once-tab').addEventListener('click',()=>setKind('once'));$('weekly-tab').addEventListener('click',()=>setKind('weekly'));
for(const b of document.querySelectorAll('[data-day]')){b.setAttribute('aria-label',dayNames[Number(b.dataset.day)]);b.addEventListener('click',()=>b.setAttribute('aria-pressed',String(b.getAttribute('aria-pressed')!=='true')));}
$('window-add').addEventListener('click',()=>{setKind('once');note('window-feedback','');const start=Math.ceil(Date.now()/300000)*300;$('once-start').value=localInput(start);$('once-end').value=localInput(start+7200);$('window-dialog').showModal();});
$('window-form').addEventListener('submit',event=>{event.preventDefault();action('window-submit','window-feedback','Saving…',async()=>{const body=windowKind==='once'?{kind:'once',start:$('once-start').value,end:$('once-end').value}:{kind:'weekly',days:Array.from(document.querySelectorAll('[data-day][aria-pressed=true]')).map(b=>Number(b.dataset.day)),start:$('weekly-start').value,end:$('weekly-end').value};await request('/api/windows','POST',body);await refresh();$('window-dialog').close();toast(state.entry.paused?'Window saved. Resume windows when you are ready.':'Window saved.');});});
$('settings-open').addEventListener('click',()=>{$('timezone').value=zone();$('settings-dialog').showModal();});
$('timezone-form').addEventListener('submit',event=>{event.preventDefault();action('timezone-submit','settings-feedback','Saving…',async()=>{await request('/api/preferences','PUT',{timezone:$('timezone').value});await refresh();note('settings-feedback','Timezone saved.');toast('Timezone saved.');});});
$('google-form').addEventListener('submit',event=>{event.preventDefault();action('google-submit','google-feedback','Saving…',async()=>{await request('/api/preferences','PUT',{google_client_id:$('google-client').value,google_email:$('google-email').value});await refresh();await googleButton('google-link-button');note('google-feedback','Setup saved. Use the Google button above to link your account.');});});
$('ring-reconnect').addEventListener('click',()=>action('ring-reconnect','ring-feedback','Connecting…',async()=>{const result=await request('/api/ring/reconnect','POST',{});await refresh();if(!result.connected)throw Error(result.message);note('ring-feedback','Ring is connected.');}));
$('ring-form').addEventListener('submit',event=>{event.preventDefault();action('ring-submit','ring-login-feedback','Signing in…',async()=>{const result=await request('/api/ring/login','POST',{email:$('ring-email').value,password:$('ring-password').value,code:$('ring-code').value});if(result.needs_2fa){$('ring-code-field').hidden=false;$('ring-code').required=true;$('ring-code').focus();note('ring-login-feedback','Enter the verification code from Ring, then sign in again.');return;}$('ring-password').value='';$('ring-code').value='';await refresh();if(!result.connected)throw Error(result.message);note('ring-login-feedback','Ring is connected.');});});
$('signout').addEventListener('click',()=>action('signout','settings-feedback','Signing out…',async()=>{await request('/auth/logout','POST',{});$('settings-dialog').close();await showLogin();toast('Signed out. Your entry windows have not changed.');}));
(async()=>{try{const old=sessionStorage.getItem('entrance-key');if(old){try{await request('/auth/key','POST',{token:old});}catch{}sessionStorage.removeItem('entrance-key');}await enter();}catch(error){$('login-view').hidden=false;note('login-feedback',error.message,'error');$('setup-access').open=true;}})();
