"""Click-to-speak answer control. No server credentials or extra AI requests."""

import json
import re


def spoken_text(text: str) -> str:
    """Read the full answer naturally without speaking Markdown punctuation."""
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|>\s*)", "", text)
    return re.sub(r"[*_`~]", "", text).strip()


def speech_controls_html(text: str) -> str:
    payload = json.dumps(spoken_text(text)).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return '''<!doctype html><html><head><style>
body{margin:0;background:transparent;font:10px system-ui;color:light-dark(#475569,#94a3b8)}
button{width:34px;height:34px;padding:6px;display:grid;place-items:center;border:0;
border-radius:8px;background:transparent;cursor:pointer;color:inherit}
button:hover{background:light-dark(#eef2ff,#252b4a);color:light-dark(#4f46e5,#818cf8)}
button:focus-visible{outline:2px solid light-dark(#4f46e5,#818cf8)}svg{width:21px;height:21px}
#status{display:block;line-height:12px;overflow-wrap:anywhere}</style></head><body>
<button type="button" aria-label="Read answer aloud" title="Read answer aloud · installed English (United States) voice" aria-pressed="false">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
<path d="m11 5-6 4H2v6h3l6 4zM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14"/></svg></button>
<span id="status" role="status" aria-live="polite"></span>
<script>const answer=''' + payload + ''';
const button=document.querySelector('button'),status=document.querySelector('#status');
const original=button.innerHTML;let active=false,generation=0,cancelVoiceWait=null;
const root=parent.document.querySelector('.stApp');
const hostingFrame=window.frameElement;
const syncTheme=()=>{document.documentElement.style.colorScheme=getComputedStyle(root).colorScheme;};
syncTheme();const observer=new MutationObserver(()=>{if(hostingFrame&&!hostingFrame.isConnected){cleanup();return;}syncTheme();});
observer.observe(root,{attributes:true,attributeFilter:['class','style'],childList:true,subtree:true});
const synth=window.speechSynthesis;
function reset(){active=false;window.currentUtterance=null;button.innerHTML=original;button.setAttribute('aria-pressed','false');button.setAttribute('aria-label','Read answer aloud');button.title='Read answer aloud · installed English (United States) voice';}
function stop(){generation++;if(cancelVoiceWait)cancelVoiceWait();if(active&&synth)synth.cancel();reset();}
const stopOther=()=>{if(active){stop();status.textContent='';}};
parent.addEventListener('mysql-chat-speech-start',stopOther);
function cleanup(){stop();observer.disconnect();parent.removeEventListener('mysql-chat-speech-start',stopOther);}
function selectVoice(voices){
  // Only explicitly installed US English voices: never a cloud or default voice.
  const us=voices.filter(v=>/^en[-_]US$/i.test(v.lang)&&v.localService===true);
  // Voice APIs do not expose reliable gender metadata. Require a familiar female
  // US voice name rather than silently falling back to a male or unknown voice.
    const preferredFemaleUS=/\\b(?:Samantha|Flo|Ava|Allison|Susan|Zoe|Nicky|Jenny|Aria|Michelle|Joanna|Salli|Kendra|Kimberly|Ivy)\\b/i;
  const quality=/natural|neural|enhanced|premium|high[ -]quality/i;
  const female=us.filter(v=>preferredFemaleUS.test(v.name));
  const score=v=>quality.test(v.name)?1:0;
  return female.reduce((best,v)=>!best||score(v)>score(best)?v:best,null);
}
async function availableVoice(token){
  const voice=selectVoice(synth.getVoices());if(voice)return voice;
  return new Promise(resolve=>{
    let settled=false;
    const done=voice=>{if(settled)return;settled=true;clearTimeout(timer);synth.removeEventListener('voiceschanged',changed);
      if(cancelVoiceWait===cancel)cancelVoiceWait=null;resolve(voice);};
    const cancel=()=>done(null);
    const changed=()=>{if(token!==generation){cancel();return;}const voice=selectVoice(synth.getVoices());if(voice)done(voice);};
    // Other languages (or cloud voices) may arrive first. Keep waiting for local en-US.
    const timer=setTimeout(()=>done(selectVoice(synth.getVoices())),3000);
    cancelVoiceWait=cancel;synth.addEventListener('voiceschanged',changed);changed();
  });
}
function unavailable(){
  reset();
  const instructions='No installed English (United States) voice is available to this browser. On macOS, open System Settings → Accessibility → Read & Speak (or Spoken Content) → System voice, choose a female English (United States) voice such as Samantha, Flo, Ava, or Allison if available, and download it. Then reload this page. Browser speech is device-generated and cannot guarantee a perfect accent.';
  status.textContent=instructions;button.title=instructions;
}
if(!synth||!window.SpeechSynthesisUtterance){button.disabled=true;button.title='Speech is unavailable in this browser';status.textContent='Unavailable';}
else button.onclick=async()=>{
  if(active){stop();status.textContent='Stopped';return;}
  parent.dispatchEvent(new Event('mysql-chat-speech-start'));
  synth.cancel();const token=++generation;active=true;status.textContent='Loading';
  button.setAttribute('aria-pressed','true');button.setAttribute('aria-label','Stop reading');button.title='Stop reading';
  button.innerHTML='<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
  const voice=await availableVoice(token);if(token!==generation)return;
  if(!voice){unavailable();return;}
  // One continuous utterance preserves prosody, decimals and natural sentence transitions.
  // Do not split/restart playback at every sentence or arbitrary character boundary.
  const utterance=new SpeechSynthesisUtterance(answer);utterance.lang='en-US';utterance.rate=1;utterance.pitch=1;
    utterance.voice=voice;utterance.onend=()=>{if(token!==generation)return;reset();status.textContent='';};
    utterance.onerror=event=>{if(token!==generation)return;reset();status.textContent='Try again';button.title='Speech failed: '+event.error;};
    // Keep the utterance alive while the browser reads it.
    window.currentUtterance=utterance;
  status.textContent='';button.title='Stop reading · '+voice.name+' · '+voice.lang;synth.speak(utterance);
};
window.addEventListener('pagehide',cleanup);
</script></body></html>'''