#!/usr/bin/env python3
"""E2E verification of Fistream v1 in real Firefox (headless)."""
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
opts.set_preference('media.autoplay.default', 0)
svc = Service(executable_path=os.path.expanduser('~/.local/bin/geckodriver'))
driver = webdriver.Firefox(options=opts, service=svc)
BASE = 'http://localhost:8000'
results = []


def check(name, cond, extra=''):
    results.append((name, bool(cond), extra))
    print(('PASS' if cond else 'FAIL'), name, extra)


try:
    # 1. Home rows
    driver.get(BASE + '/')
    time.sleep(3)
    rows = [h.text for h in driver.find_elements('css selector', '.row h2')]
    cards = len(driver.find_elements('css selector', '.card'))
    check('home rows', len(rows) >= 6, str(rows[:3]))
    check('home cards >= 80', cards >= 80, f'{cards} cards')

    # 2. Browse pages
    driver.get(BASE + '/browse/movie')
    time.sleep(2)
    check('browse movie pills',
          len(driver.find_elements('css selector', '.pill')) >= 10)
    check('browse movie grid',
          len(driver.find_elements('css selector', '.card')) >= 20)

    # 3. Watch movie: sources + play + subs status
    driver.get(BASE + '/watch/movie/tt0137523')
    time.sleep(6)
    srcs = len(driver.find_elements('css selector', '.srcbtn'))
    check('watch sources >=4', srcs >= 4, f'{srcs} buttons')
    st = driver.find_element('id', 'substatus').text
    check('subs resolved', 'بررسی' not in st and len(st) > 5, st[:40])
    driver.execute_script("document.getElementById('playBtn').click()")
    time.sleep(5)
    check('player mounts', driver.execute_script(
        "return document.getElementById('stage').classList.contains('show')"))
    active = driver.execute_script(
        "return (document.querySelector('.srcbtn.active')||{}).textContent")
    check('source marked active', bool(active), str(active))

    # switch source
    driver.execute_script(
        "document.querySelectorAll('.srcbtn')[1].click()")
    time.sleep(3)
    iframe_src = driver.execute_script(
        "return (document.querySelector('#stage iframe')||{}).src||''")
    check('source switch works', 'videasy' in iframe_src, iframe_src[:60])

    # 4. Series page
    driver.get(BASE + '/watch/series/tt0903747?s=2&e=5')
    time.sleep(6)
    seas = driver.execute_script(
        "return Array.from(document.querySelectorAll('.sebtn')).map(b=>b.textContent)")
    eps = len(driver.find_elements('css selector', '.epbtn'))
    check('series seasons listed', len(seas) >= 3, str(seas))
    check('series episodes listed', eps >= 5, f'{eps} eps')
    st = driver.find_element('id', 'substatus').text
    check('series subs resolved', 'بررسی' not in st, st[:40])

    # episode navigation changes URL params
    driver.execute_script(
        "document.querySelectorAll('.epbtn')[1].click()")
    time.sleep(2)
    check('episode nav', 'e=' in driver.current_url,
          driver.current_url[-25:])

    # 5. Continue watching appears after visits
    driver.get(BASE + '/')
    time.sleep(2)
    body = driver.find_element('tag name', 'body').text
    check('continue watching row', 'ادامه تماشا' in body)

finally:
    driver.quit()

fails = [r for r in results if not r[1]]
print(f"\n=== {len(results)-len(fails)}/{len(results)} PASSED ===")
