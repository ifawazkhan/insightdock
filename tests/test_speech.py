import shutil
import subprocess
from pathlib import Path

import pytest

from chatbot.speech import speech_controls_html, spoken_text


def test_read_whole_answer_without_markdown():
    answer = "There are **19 departments**.\n- Average salary is **100,000**.\nSee [details](https://example.com)."
    assert spoken_text(answer) == "There are 19 departments.\nAverage salary is 100,000.\nSee details."
    html = speech_controls_html(answer)
    assert "There are 19 departments." in html
    assert "Average salary is 100,000." in html
    assert "utterance.lang='en-US'" in html
    assert "selectVoice" in html
    assert "Stop reading" in html
    assert "synth.speak" in html


def test_answer_cannot_inject_script_or_html():
    html = speech_controls_html('</script><img src=x onerror="alert(1)"> & **hello**')
    assert '</script><img' not in html
    assert r"\u003c/script\u003e" in html
    assert r"\u0026" in html


def test_unsupported_browser_and_late_voices_handled():
    html = speech_controls_html("There are 19 departments.")
    assert "voiceschanged" in html
    assert "button.disabled=true" in html
    assert "localService" in html
    assert "pagehide" in html


def test_continuous_speech_avoids_sentence_restarts_and_legacy_voices():
    html = speech_controls_html("Average is 123.45 dollars. There are 19 departments. " * 30)
    assert "new SpeechSynthesisUtterance(answer)" in html
    assert "chunks" not in html
    assert "answer.match" not in html
    assert html.count("synth.speak(utterance)") == 1
    assert "preferredFemaleUS.test(v.name)" in html
    assert "US English default" not in html
    assert "if(voice)utterance.voice" not in html


def test_prefers_installed_macos_us_voices_and_discloses_voice_limitations():
    html = speech_controls_html("Hello.")
    assert "preferredFemaleUS" in html
    assert "Samantha|Flo|Ava|Allison" in html
    assert "female English (United States) voice" in html
    assert "Browser speech is device-generated and cannot guarantee a perfect accent." in html
    assert "voice.lang" in html


# Execute the generated browser JavaScript with deterministic speech/timer mocks.
# JavaScriptCore is included on macOS; Node is supported on other platforms.
@pytest.fixture(scope="module")
def javascript_runtime():
    node = shutil.which("node")
    jsc = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")
    if node:
        return [node, "-e"], "globalThis.print=console.log;"
    if jsc.is_file():
        return [str(jsc), "-e"], ""
    pytest.skip("Speech behavioral tests require Node or macOS JavaScriptCore")


BROWSER_MOCKS = r"""
function assert(value, message='Assertion failed'){if(!value)throw new Error(message);}
function target(){
    const listeners=new Map();
    return {listeners,addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);},
        removeEventListener(name,fn){listeners.get(name)?.delete(fn);},
        dispatchEvent(event){for(const fn of [...(listeners.get(event.type)||[])])fn(event);}};
}
globalThis.Event=class {constructor(type){this.type=type;}};
const timers=new Map();let nextTimer=0;
globalThis.setTimeout=(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,delay});return id;};
globalThis.clearTimeout=id=>timers.delete(id);
function timeout(){for(const {fn,delay} of [...timers.values()]){assert(delay===3000);fn();}}
const mockRoot={};const frame={isConnected:true};
const mockButton={innerHTML:'speaker',title:'',disabled:false,attributes:{},setAttribute(k,v){this.attributes[k]=v;}};
const mockStatus={textContent:''};
globalThis.document={documentElement:{style:{}},querySelector:s=>s==='button'?mockButton:mockStatus};
globalThis.parent=Object.assign(target(),{document:{querySelector:()=>mockRoot}});
globalThis.getComputedStyle=()=>({colorScheme:'light'});
let mutationCallback,disconnected=false;
globalThis.MutationObserver=class {constructor(fn){mutationCallback=fn;}observe(){}disconnect(){disconnected=true;}};
let voices=[],spoken=[],cancelCount=0;
const speech=Object.assign(target(),{getVoices:()=>voices,cancel(){cancelCount++;},speak(u){spoken.push(u);}});
globalThis.window=Object.assign(target(),{frameElement:frame,speechSynthesis:speech});
globalThis.SpeechSynthesisUtterance=class {constructor(text){this.text=text;}};
window.SpeechSynthesisUtterance=SpeechSynthesisUtterance;
function voice(name,lang='en-US',localService=true){return {name,lang,localService};}
function changed(){speech.dispatchEvent(new Event('voiceschanged'));}
function noWaitResources(){assert(timers.size===0,'timer leaked');assert(!speech.listeners.get('voiceschanged')?.size,'listener leaked');}
"""


SCENARIOS = {
        "strict_locale_and_installed_only": r"""
            assert(selectVoice([voice('Alex','en-GB'),voice('Samantha Enhanced','en-AU'),
                voice('Guy Natural','en-US',false),voice('Generic','en'),{name:'Unknown',lang:'en-US'}])===null);
            const installed=voice('Samantha','EN_us');
            assert(selectVoice([voice('Alex','en-GB'),installed])===installed);
            assert(selectVoice([voice('Eddy'),voice('Alex'),voice('Fred')])===null,'do not fall back to male/unknown voice');
        """,
        "female_us_preference_and_legacy": r"""
            const generic=voice('Alex'),samantha=voice('Samantha'),eddy=voice('Eddy (English (US))');
            assert(selectVoice([generic,samantha])===samantha,'Samantha must beat generic');
            assert(selectVoice([samantha,eddy])===samantha,'female US voice must be preferred');
            const usNatural=voice('Ava Natural');
            assert(selectVoice([eddy,usNatural])===usNatural,'female US voice must beat male voice');
            assert(selectVoice([voice('Flo (English (UK))','en-GB'),generic])===null,'UK locale rejected and male fallback refused');
            assert(selectVoice([voice('Fred'),generic,samantha])===samantha,'preferred voice beats male/unknown');
            const only=voice('Fred');assert(selectVoice([only])===null,'male-only choices are rejected');
        """,
        "wait_through_other_languages_and_cloud": r"""
            voices=[voice('British','en-GB')];const pending=mockButton.onclick();
            assert(spoken.length===0);assert(timers.size===1);
            voices=[voice('French','fr-FR')];changed();await Promise.resolve();
            assert(spoken.length===0&&timers.size===1);
            voices.push(voice('Guy Natural','en-US',false));changed();await Promise.resolve();
            assert(spoken.length===0&&timers.size===1);
            const us=voice('Samantha (English (US))');voices.push(us);changed();await pending;
            assert(spoken.length===1&&spoken[0].voice===us&&spoken[0].lang==='en-US');noWaitResources();
        """,
        "empty_list_waits_and_deadline_rechecks": r"""
            const pending=mockButton.onclick();assert(spoken.length===0);
            voices=[voice('Alex Enhanced')];timeout();await pending;
            assert(spoken.length===0&&!active&&mockStatus.textContent.includes('Samantha'));noWaitResources();
        """,
        "timeout_guidance_no_default_and_retry": r"""
            voices=[voice('British','en-GB'),voice('Guy Natural','en-US',false)];
            const pending=mockButton.onclick();timeout();await pending;
            assert(spoken.length===0);assert(!active);noWaitResources();
            assert(mockButton.attributes['aria-pressed']==='false');assert(!mockButton.disabled);
            assert(mockStatus.textContent.includes('No installed English (United States) voice'),mockStatus.textContent);
            assert(mockStatus.textContent.includes('On macOS')&&mockStatus.textContent.includes('Samantha'),mockStatus.textContent);
            assert(mockStatus.textContent.includes('reload')&&mockStatus.textContent.includes('cannot guarantee a perfect accent'),mockStatus.textContent);
            voices=[voice('Samantha (English (US))')];changed();assert(spoken.length===0);
            await mockButton.onclick();assert(spoken.length===1&&spoken[0].voice===voices[0]);
        """,
        "stop_cancels_loading": r"""
            const pending=mockButton.onclick();await mockButton.onclick();await pending;
            voices=[voice('Alex')];changed();timeout();await Promise.resolve();
            assert(spoken.length===0&&!active);assert(mockStatus.textContent==='Stopped');noWaitResources();
        """,
        "other_control_cancels_loading": r"""
            const pending=mockButton.onclick();parent.dispatchEvent(new Event('mysql-chat-speech-start'));await pending;
            voices=[voice('Alex')];changed();assert(spoken.length===0&&!active);noWaitResources();
        """,
        "pagehide_cancels_loading": r"""
            const pending=mockButton.onclick();window.dispatchEvent(new Event('pagehide'));await pending;
            voices=[voice('Alex')];changed();assert(spoken.length===0);assert(disconnected);noWaitResources();
            assert(!parent.listeners.get('mysql-chat-speech-start').size);
        """,
        "detached_frame_cancels_loading": r"""
            const pending=mockButton.onclick();frame.isConnected=false;mutationCallback();await pending;
            voices=[voice('Alex')];changed();assert(spoken.length===0);assert(disconnected);noWaitResources();
        """,
        "continuous_utterance_and_stale_callbacks": r"""
            voices=[voice('Samantha Enhanced')];await mockButton.onclick();const first=spoken[0];
            assert(spoken.length===1&&first.text===answer&&first.rate===1&&first.pitch===1);
            assert(window.currentUtterance===first);await mockButton.onclick();
            assert(cancelCount>=2&&window.currentUtterance===null);await mockButton.onclick();const second=spoken[1];
            first.onend();first.onerror({error:'canceled'});
            assert(active&&window.currentUtterance===second&&mockButton.attributes['aria-pressed']==='true');
            second.onend();assert(!active&&window.currentUtterance===null&&mockStatus.textContent==='');
            await mockButton.onclick();spoken[2].onerror({error:'audio-busy'});
            assert(!active&&mockStatus.textContent==='Try again'&&mockButton.title.includes('audio-busy'));
        """,
        "stopped_wait_cannot_start_new_request": r"""
            const first=mockButton.onclick();await mockButton.onclick();const second=mockButton.onclick();
            voices=[voice('Samantha Enhanced')];changed();await first;await second;
            assert(spoken.length===1&&active);noWaitResources();
        """,
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIOS)
def test_browser_speech_behavior(javascript_runtime, scenario):
    command, prefix = javascript_runtime
    html = speech_controls_html("Average is **123.45 dollars**. There are 19 departments. " * 30)
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    checks = "\n(async()=>{" + SCENARIOS[scenario] + r"""
        })().then(()=>print('speech checks passed')).catch(error=>{
            print(error.stack);if(typeof quit==='function')quit(1);else process.exitCode=1;
        });
        """
    result = subprocess.run(
        command + [prefix + BROWSER_MOCKS + script + checks],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "speech checks passed" in result.stdout, result.stdout + result.stderr