#!/usr/bin/env python3
"""Verify Persian subtitle + local video playback in real Firefox (headless)."""
import json
import os
import time

os.environ.pop('ALL_PROXY', None)
os.environ.pop('all_proxy', None)

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

opts = Options()
opts.add_argument('-headless')
# Allow autoplay in automation (real users' clicks are trusted gestures anyway)
opts.set_preference('media.autoplay.default', 0)
opts.set_preference('media.autoplay.blocking_policy', 0)
svc = Service(executable_path=os.path.expanduser('~/.local/bin/geckodriver'))
driver = webdriver.Firefox(options=opts, service=svc)
try:
    driver.get('http://localhost:8899/player.html')
    time.sleep(6)
    state = driver.execute_script("""
      const v = document.querySelector('#player');
      const t = v.textTracks[0];
      return {videoSrc: (v.currentSrc || '').split('/').pop(),
              readyState: v.readyState, tracks: v.textTracks.length,
              mode: t ? t.mode : null,
              cues: (t && t.cues) ? t.cues.length : null};
    """)
    print('FIREFOX STATE:', json.dumps(state, ensure_ascii=False))
    driver.execute_script("document.getElementById('playBtn').click()")
    time.sleep(4)
    state2 = driver.execute_script("""
      const v = document.querySelector('#player');
      return {paused: v.paused, rs: v.readyState, time: v.currentTime.toFixed(1)};
    """)
    print('AFTER PLAY:', json.dumps(state2, ensure_ascii=False))
    ok = state.get('cues') and not state2.get('paused')
    print('VERDICT:', 'PASS' if ok else 'FAIL')
finally:
    driver.quit()
