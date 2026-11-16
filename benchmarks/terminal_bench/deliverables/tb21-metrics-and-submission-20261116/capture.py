"""Capture actual local reports and remote pages, without changing their content."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--proxy')
    args = parser.parse_args()
    if '/' in args.name or args.name.startswith('.'):
        raise ValueError('Use a simple artifact basename')
    with sync_playwright() as p:
        options = dict(headless=True,args=['--no-sandbox'])
        if args.proxy:
            options['proxy']={'server':args.proxy,'bypass':'127.0.0.1,localhost'}
        browser = p.chromium.launch(**options)
        page = browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1)
        response=page.goto(args.url,wait_until='domcontentloaded',timeout=90000)
        page.wait_for_load_state('load',timeout=90000)
        page.evaluate('document.fonts.ready')
        page.wait_for_function('document.body && document.body.innerText.length > 200', timeout=60000)
        if 'hub.harborframework.com/jobs/' in args.url:
            page.wait_for_function("!document.body.innerText.includes('Loading results')", timeout=60000)
        page.screenshot(path=str(ROOT/(args.name+'.png')),full_page=True)
        page.screenshot(path=str(ROOT/(args.name+'-viewport.png')),full_page=False)
        receipt=dict(requested_url=args.url,final_url=page.url,http_status=response.status if response else None,
                     title=page.title(),captured_at=datetime.now(timezone.utc).isoformat(),
                     screenshot=args.name+'.png',viewport_screenshot=args.name+'-viewport.png',
                     page_content_modified=False)
        (ROOT/(args.name+'.capture.json')).write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt))
        browser.close()


if __name__=='__main__':
    main()
