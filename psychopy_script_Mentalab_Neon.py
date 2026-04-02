# -*- coding: utf-8 -*-
"""
PsychoPy task for Mentalab Explore Pro + Pupil Labs Neon, recorded via Lab Recorder.

Approach #2b (Neon + PsychoPy LSL markers):
- Neon streams gaze + pupil (and Neon Events) to LSL.
- Mentalab Explore Desktop streams EEG (ExG/ORN/Marker) to LSL.
- PsychoPy sends task events to Neon (via the Neon PsychoPy plugin) AND publishes the same
  events as a PsychoPy LSL marker stream (PSY_MARKERS_TASK), so you can choose your preferred
  event source during analysis.

Task:
- 40 trials per condition (active/passive), shuffled
- Trial flow:
    Fixation 500 ms -> condition word
    ACTIVE: wait ≤2.0 s for space; play 500 ms tone immediately on press
            if no press by 2.0 s: auto-play tone (cause="timeout_no_press")
    PASSIVE: play 500 ms tone at random delay in [0, 2.0] s
    After tone: ITI 2.0 s (blank)

Events sent to Neon + LSL markers (JSON):
- condition_start, button_press, sound_onset, experiment_start/end
Press ESC to quit.
"""

import json
import logging
import os
import random
import time
from typing import Any, Dict, Optional, Tuple

from psychopy import core, event, prefs, sound, visual
from psychopy.hardware import keyboard
from psychopy.iohub import launchHubServer
from pylsl import StreamInfo, StreamOutlet

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

try:
    from pupil_labs.realtime_api.simple import Device as NeonRTDevice
except ImportError:
    NeonRTDevice = None

# ---------- PsychoPy Preferences ----------
# NOTE: PsychoPy chooses the audio backend at import time.
# Prefer sounddevice first; if it fails PsychoPy may fall back (often to PTB).
prefs.hardware["audioLib"] = ["sounddevice", "ptb", "pyo", "pygame"]

# Choose a specific output device to avoid playing on a monitor / virtual device.
# Override via env var, e.g. export PSY_AUDIO_DEVICE='MacBook Air Speakers'
prefs.hardware["audioDevice"] = os.environ.get(
    "PSY_AUDIO_DEVICE", "MacBook Air Speakers"
)

# ---------- Settings ----------
WIN_SIZE = [1920, 1080]
FULLSCREEN = False
N_TRIALS_PER_COND = 5
FIX_MS = 500
RESP_WINDOW = 2.0
SOUND_SECS = 0.500
ITI_SECS = 2.0
KEYS_ACTIVE = ["space"]
QUIT_KEY = "escape"
TONE_HZ = 1000
RANDOM_SEED = 42  # set None for fully random each run

# ---------- PsychoPy -> LSL marker stream (optional) ----------
USE_LSL_MARKERS = True
LSL_MARKER_STREAM_NAME = "PSY_MARKERS_TASK"
PRINT_MARKERS = True

# ---------- Neon (PsychoPy -> Neon events) ----------
NEON_COMPANION_IP = os.environ.get("NEON_COMPANION_IP", "192.168.1.102")
NEON_COMPANION_PORT = int(os.environ.get("NEON_COMPANION_PORT", "8080"))
USE_NEON_EVENTS = True
PRINT_EVENTS = True

# ---------- Global State ----------
neon_tracker = None
io = None
neon_rt = None
neon_offset_ns = 0
lsl_outlet = None


def setup_neon_rt():
    """Initialize Neon Real-Time API connection."""
    global neon_rt, neon_offset_ns

    if NeonRTDevice is None:
        logger.warning(
            "[NEON/RT] Real-Time API not available; explicit timestamp events disabled."
        )
        return

    try:
        neon_rt = NeonRTDevice(address=NEON_COMPANION_IP, port=NEON_COMPANION_PORT)
        logger.info(f"[NEON/RT] Connected to {NEON_COMPANION_IP}:{NEON_COMPANION_PORT}")

        est = neon_rt.estimate_time_offset()
        neon_offset_ns = round(est.time_offset_ms.mean * 1_000_000)
        logger.info(
            f"[NEON/RT] time_offset_ms.mean={est.time_offset_ms.mean:.3f} "
            f"-> offset_ns={neon_offset_ns:_d}"
        )
    except Exception as e:
        logger.error(
            f"[NEON/RT] Connection/offset estimation failed: {e!r}"
        )
        neon_rt = None
        neon_offset_ns = 0


def setup_neon_tracker(window: visual.Window):
    """Connect to Neon via the PsychoPy-Neon ioHub backend."""
    global neon_tracker, io

    if not USE_NEON_EVENTS:
        return None

    try:
        iohub_config = {
            "eyetracker.neon.EyeTracker": {
                "name": "tracker",
                "runtime_settings": {
                    "companion_address": NEON_COMPANION_IP,
                    "companion_port": NEON_COMPANION_PORT,
                },
            }
        }
        io = launchHubServer(window=window, **iohub_config)
        neon_tracker = io.devices.tracker
        return neon_tracker
    except Exception as e:
        logger.error(f"Could not connect to Neon ioHub: {e!r}")
        return None


def set_neon_recording(enabled: bool) -> Tuple[bool, Optional[str]]:
    """Start/stop Neon recording via Real-Time API."""
    global neon_rt
    if neon_rt is None:
        logger.warning(
            f"[NEON/RT] Recording {'START' if enabled else 'STOP'} skipped "
            "(neon_rt is None)"
        )
        return False, "neon_rt_none"

    try:
        if enabled:
            neon_rt.recording_start()
            logger.info("[NEON/RT] Recording START")
        else:
            neon_rt.recording_stop_and_save()
            logger.info("[NEON/RT] Recording STOP+SAVE")
        return True, None
    except Exception as e:
        logger.error(
            f"[NEON/RT] Failed to change recording state (enabled={enabled}): {e!r}"
        )
        return False, str(e)


def send_neon_event(name: str, **props: Any) -> Tuple[bool, Optional[str], bool]:
    """Send a task event to Neon using the Real-Time API with explicit timestamp."""
    global neon_rt, neon_offset_ns

    payload = {
        "name": name,
        "props": props,
        "pc_time": core.getTime(),
        "ts_unix": time.time(),
    }
    event_str = json.dumps(payload)

    if neon_rt is None:
        if PRINT_EVENTS:
            logger.info(f"[NEON EVENT - SKIPPED] {event_str}")
        return False, "neon_rt_none", False

    now_ns = time.time_ns()
    companion_ts_ns = int(now_ns - int(neon_offset_ns))

    try:
        neon_rt.send_event(event_str, event_timestamp_unix_ns=companion_ts_ns)
        if PRINT_EVENTS:
            logger.info(
                f"[NEON EVENT] companion_ts_ns={companion_ts_ns} | {event_str}"
            )
        return True, None, True
    except Exception as e:
        if PRINT_EVENTS:
            logger.error(f"[NEON EVENT - FAILED] {event_str} | Error: {e!r}")
        return False, str(e), False


def setup_lsl_outlet() -> Optional[StreamOutlet]:
    """Create a PsychoPy LSL marker outlet."""
    global lsl_outlet
    if not USE_LSL_MARKERS:
        return None

    try:
        info = StreamInfo(
            LSL_MARKER_STREAM_NAME,
            "Markers",
            1,
            0,
            "string",
            f"psychopy_markers_{os.getpid()}",
        )
        lsl_outlet = StreamOutlet(info)
        logger.info(
            f"Created LSL marker outlet: {info.name()} ({info.type()}) "
            f"src: {info.source_id()}"
        )
        return lsl_outlet
    except Exception as e:
        logger.error(f"Could not create LSL marker outlet: {e!r}")
        return None


def send_lsl_marker(name: str, **props: Any) -> Tuple[bool, Optional[str]]:
    """Send a structured (JSON) marker sample on the PsychoPy LSL marker outlet."""
    payload = {
        "name": name,
        "props": props,
        "pc_time": core.getTime(),
        "ts_unix": time.time(),
    }
    msg = json.dumps(payload)

    if lsl_outlet is None:
        if PRINT_MARKERS:
            logger.info(f"[LSL MARKER - SKIPPED] {name} {props}")
        return False, "outlet_none"

    try:
        lsl_outlet.push_sample([msg])
        if PRINT_MARKERS:
            logger.info(f"[LSL MARKER] {msg}")
        return True, None
    except Exception as e:
        logger.error(f"[LSL MARKER - FAILED] {msg} | Error: {e!r}")
        return False, str(e)


def emit_event(name: str, **props: Any):
    """Emit event to both LSL markers and Neon."""
    if USE_LSL_MARKERS:
        send_lsl_marker(name, **props)
    if USE_NEON_EVENTS:
        send_neon_event(name, **props)


def main():
    """Main experiment execution."""
    global neon_tracker, io, neon_rt, lsl_outlet

    if RANDOM_SEED is not None:
        random.seed(RANDOM_SEED)

    # ---------- Initialize Connections ----------
    setup_neon_rt()
    setup_lsl_outlet()

    # ---------- Setup Window and Stimuli ----------
    win = visual.Window(WIN_SIZE, fullscr=FULLSCREEN, units="pix")
    
    # Setup Neon ioHub
    setup_neon_tracker(win)
    if USE_NEON_EVENTS and neon_tracker is not None:
        logger.info(f"Connected to Neon at {NEON_COMPANION_IP}:{NEON_COMPANION_PORT}")
        set_neon_recording(True)

    fix = visual.TextStim(win, text="+", height=72)
    cond_text = visual.TextStim(win, text="", height=64)
    kb = keyboard.Keyboard()
    
    # Audio Setup
    tone = sound.Sound(value=TONE_HZ, secs=SOUND_SECS, stereo=False, hamming=True)
    try:
        logger.info(f"Audio backend in use: {getattr(sound, 'audioLib', 'unknown')}")
        logger.info(f"Requested audio device: {prefs.hardware.get('audioDevice')}")
        if hasattr(sound, "getDevices"):
            logger.info(f"Available audio devices: {list(sound.getDevices().keys())}")
    except Exception as e:
        logger.error(f"Audio diagnostics unavailable: {e!r}")

    # ---------- Pre-run WAIT screen ----------
    wait_msg = visual.TextStim(
        win,
        text=(
            "Waiting for Lab Recorder...\n\n"
            "1) In Neon Companion, enable LSL streaming.\n"
            "2) In Explore Desktop, enable LSL streaming.\n"
            "3) In Lab Recorder, confirm you can see:\n"
            "   - Explore_* streams\n"
            "   - Neon Companion_Neon Gaze\n"
            "   - Neon Companion_Neon Events\n"
            "   - PSY_MARKERS_TASK (optional)\n\n"
            "Start recording in Lab Recorder, then press SPACE to begin.\n"
            "(ESC to quit)"
        ),
        height=28,
        color="white",
        wrapWidth=1000,
    )

    logger.info("Testing audio… you should hear a beep now.")
    try:
        tone.setVolume(1.0)
        tone.play()
        core.wait(max(SOUND_SECS, 0.7) + 0.3)
        logger.info("Audio test done.")
    except Exception as e:
        logger.error(f"Audio playback failed: {e!r}")

    # Operator Wait Loop
    last_sync = core.getTime()
    kb.clearEvents()
    while True:
        wait_msg.draw()
        win.flip()
        
        now = core.getTime()
        if now - last_sync >= 0.5:
            emit_event("SYNC", phase="wait")
            last_sync = now
            
        keys = kb.getKeys(
            keyList=["space", "return", "enter", "escape"], 
            waitRelease=False, clear=False
        )
        if not keys:
            legacy = event.getKeys(keyList=["space", "return", "enter", "escape"])
            keys = [type("K", (), {"name": k}) for k in legacy]

        if any(k.name == "escape" for k in keys):
            emit_event("experiment_aborted_before_start")
            win.close()
            core.quit()
        if any(k.name in ("space", "return", "enter") for k in keys):
            emit_event("operator_start_pressed")
            break
        core.wait(0.05)

    # ---------- Run Trials ----------
    trials = ["active"] * N_TRIALS_PER_COND + ["passive"] * N_TRIALS_PER_COND
    random.shuffle(trials)
    
    emit_event("experiment_start")

    try:
        for ti, cond in enumerate(trials):
            # Fixation
            fix.draw()
            win.flip()
            core.wait(FIX_MS / 1000.0)

            # Condition cue
            cond_text.text = cond

            def on_flip_condition(ti=ti, cond=cond):
                emit_event("condition_start", trial_index=ti, condition=cond)

            win.callOnFlip(on_flip_condition)
            cond_text.draw()
            win.flip()

            kb.clearEvents()
            t_start = core.getTime()

            if cond == "active":
                pressed = False
                while (core.getTime() - t_start) < RESP_WINDOW:
                    keys = kb.getKeys(
                        keyList=KEYS_ACTIVE + [QUIT_KEY], 
                        waitRelease=False, clear=False
                    )
                    if keys:
                        if any(k.name == QUIT_KEY for k in keys):
                            raise KeyboardInterrupt
                        kpress = next((k for k in keys if k.name in KEYS_ACTIVE), None)
                        if kpress:
                            rt = core.getTime() - t_start
                            emit_event(
                                "button_press",
                                trial_index=ti,
                                condition=cond,
                                key=kpress.name,
                                rt=rt,
                            )
                            
                            t_before_play = core.getTime()
                            tone.play()
                            t_after_play = core.getTime()
                            sw_lat = (t_after_play - t_before_play) * 1000.0

                            emit_event(
                                "sound_onset_active",
                                trial_index=ti,
                                condition=cond,
                                cause="button_press",
                                software_latency_ms=sw_lat,
                            )
                            logger.info(f"[LATENCY] trial {ti:02d} | {sw_lat:.3f} ms")
                            pressed = True
                            break
                    cond_text.draw()
                    win.flip()
                    core.wait(0.001)
                
                if not pressed:
                    tone.play()
                    emit_event(
                        "sound_onset_active",
                        trial_index=ti,
                        condition=cond,
                        cause="timeout_no_press",
                    )

            else:  # PASSIVE
                passive_delay = random.uniform(0.0, RESP_WINDOW)
                while (core.getTime() - t_start) < passive_delay:
                    if kb.getKeys(keyList=[QUIT_KEY], waitRelease=False):
                        raise KeyboardInterrupt
                    cond_text.draw()
                    win.flip()
                    core.wait(0.001)
                tone.play()
                emit_event(
                    "sound_onset_passive",
                    trial_index=ti,
                    condition=cond,
                    scheduled_delay=passive_delay,
                )

            # Sustain cue during sound
            t_sound_local = core.getTime()
            while (core.getTime() - t_sound_local) < SOUND_SECS:
                cond_text.draw()
                win.flip()
                core.wait(0.001)

            # ITI
            win.flip()
            core.wait(ITI_SECS)

        emit_event("experiment_end")

    except KeyboardInterrupt:
        emit_event("experiment_aborted_by_user")
    finally:
        # Cleanup
        try:
            if USE_NEON_EVENTS:
                set_neon_recording(False)
        except Exception:
            pass
        try:
            if io is not None:
                io.quit()
        except Exception:
            pass
        win.close()
        try:
            if neon_rt is not None:
                neon_rt.close()
        except Exception:
            pass
        core.quit()


if __name__ == "__main__":
    main()
