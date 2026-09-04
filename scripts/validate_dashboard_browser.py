#!/usr/bin/env python3
"""Browser acceptance for the real synthetic ProductWorkspace output.

Default mode navigates the actual loopback-served site and local CSS/JS.
--inline is a narrower DOM-render check for sandboxes that prohibit loopback
navigation; its result explicitly does not claim a served-site check.
No model calls, credentials, customer data or existing runs are used.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
from pathlib import Path
import re
import threading
from typing import Any

from build_dashboard_demo import build_demo


def inline_local_assets(html: str, root: Path) -> str:
    def read(ref: str) -> str:
        path = (root / ref).resolve(strict=True)
        path.relative_to(root.resolve())
        if not path.is_file():
            raise ValueError("asset is not a regular file")
        return path.read_text(encoding="utf-8")

    def css(match: re.Match[str]) -> str:
        href = re.search(r'href="([^"]+)"', match.group(0))
        if href is None:
            raise ValueError("stylesheet has no reference")
        return '<style>' + read(href.group(1)) + '</style>'

    html = re.sub(r'<link\b[^>]*rel="stylesheet"[^>]*>', css, html)
    return re.sub(r'<script\b[^>]*src="([^"]+)"[^>]*>\s*</script>',
                  lambda match: '<script>' + read(match.group(1)) + '</script>', html)


def validate(output: Path, *, inline: bool = False, chromium: str | None = None) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    if output.exists():
        raise FileExistsError("use a new output directory; existing evidence is not overwritten")
    output.mkdir(parents=True)
    result = build_demo(output / 'run')
    site = Path(result['html']).parent
    before = {p.relative_to(site).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in site.rglob('*') if p.is_file()}

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors: list[str] = []
    checks: dict[str, Any] = {'mode': 'inline_dom' if inline else 'served_site',
                              'synthetic': True, 'model_calls': 0}
    try:
        with sync_playwright() as playwright:
            kwargs: dict[str, Any] = {'headless': True}
            if chromium:
                kwargs['executable_path'] = chromium
            browser = playwright.chromium.launch(**kwargs)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            if inline:
                page.set_content(inline_local_assets((site/'index.html').read_text(), site), wait_until='load')
            else:
                page.goto(f'http://127.0.0.1:{server.server_port}/index.html', wait_until='networkidle')
            assert page.title() == 'Operations cockpit'
            assert page.locator('.overview-kpi').count() == 4
            assert page.locator('.overview-surface').count() == 9
            assert page.locator('.scatter-point').count() == 12
            assert page.locator('.area-fill').count() >= 1
            assert page.locator('svg').count() >= 5
            assert page.locator('main').inner_text().find('Synthetic demonstration') >= 0
            assert page.evaluate('document.documentElement.scrollWidth <= 1440')
            page.screenshot(path=str(output/'dashboard-desktop.png'), full_page=True)
            checks['kpi_cards'] = 4
            checks['business_views'] = 13
            checks['scatter_points'] = 12

            # Filtering acts on rendered views, without modifying accepted data.
            search = page.locator('[data-runtime-search]').first
            search.fill('Delivery time and order value')
            assert page.locator('[data-runtime-card]:visible').count() == 1
            search.fill('')
            assert page.locator('[data-runtime-card]:visible').count() == 13
            domain = page.locator('select[data-runtime-domain]').first
            choices = domain.locator('option').evaluate_all('(items) => items.map(x => x.value).filter(Boolean)')
            if choices:
                domain.select_option(choices[0])
                assert 0 < page.locator('[data-runtime-card]:visible').count() < 13
                page.locator('[data-runtime-clear]').first.click()
            checks['search_and_domain_filter'] = 'passed'

            # A donut legend must actually hide SVG marks and must not affect
            # the unrelated pie chart, even if both have their first series.
            toggle = page.locator('[data-series-toggle]').filter(has_text='Enterprise').first
            key = toggle.get_attribute('data-series-toggle')
            before_marks = page.locator('[data-series-key]').evaluate_all(
                '(items) => items.map(x => ({key:x.getAttribute("data-series-key"), hidden:getComputedStyle(x).display === "none"}))')
            toggle.click()
            after_marks = page.locator('[data-series-key]').evaluate_all(
                '(items) => items.map(x => ({key:x.getAttribute("data-series-key"), hidden:getComputedStyle(x).display === "none"}))')
            target = [x for x in after_marks if x['key'] == key]
            assert target and all(x['hidden'] for x in target), 'legend did not hide its actual chart marks'
            assert [x for x in before_marks if x['key'] != key] == [x for x in after_marks if x['key'] != key]
            toggle.click()
            checks['legend_isolation_and_svg_visibility'] = 'passed'

            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= 390')
            page.screenshot(path=str(output/'dashboard-mobile.png'), full_page=True)
            checks['mobile_horizontal_overflow'] = False
            if not inline:
                # Real navigation, local assets and reachable evidence/domain pages.
                for path in sorted(site.rglob('*.html')):
                    relative = path.relative_to(site).as_posix()
                    response = page.goto(f'http://127.0.0.1:{server.server_port}/{relative}', wait_until='networkidle')
                    assert response and response.status == 200, relative
                    assert page.locator('main').count() == 1, relative
                checks['served_pages'] = len(list(site.rglob('*.html')))
            browser.close()
        assert not errors, errors
        assert before == {p.relative_to(site).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in site.rglob('*') if p.is_file()}
        checks.update({'status': 'passed', 'javascript_errors': errors,
                       'site_bytes_unchanged': True, 'candidate_hash': result['candidate_hash']})
    except Exception as exc:
        checks.update({'status': 'failed', 'javascript_errors': errors, 'error': str(exc)})
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        (output/'browser-validation.json').write_text(json.dumps(checks, indent=2)+'\n')
    return checks


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inline', action='store_true')
    parser.add_argument('--chromium')
    args = parser.parse_args()
    print(json.dumps(validate(args.output.resolve(), inline=args.inline, chromium=args.chromium), indent=2))
