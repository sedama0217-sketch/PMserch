#!/usr/bin/env python3
"""
selector_inspector.py

指定ページを Playwright で取得し、商品ブロックと思われる要素群の構造を抽出して
候補セレクタとDOMの抜粋をログに出力します。
"""
import os
import json
from playwright.sync_api import sync_playwright

def load_cfg(path="config.json"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"url": os.environ.get("TARGET_URL", ""), "selectors": {}}

def short(s, n=400):
    return s if len(s) <= n else s[:n] + " ... [truncated]"

def guess_item_selectors(page):
    candidates = [
        "ul.product-list li",
        ".product-list .product-item",
        ".products .product",
        ".product-box",
        ".product-item",
        ".product-card",
        ".list .item",
        ".product-list li",
        ".list-product .item"
    ]
    found = []
    for c in candidates:
        try:
            els = page.query_selector_all(c)
            if els and len(els) >= 1:
                found.append((c, len(els)))
        except Exception:
            pass
    return found

def inspect(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        guesses = guess_item_selectors(page)
        print("GUESS_ITEM_SELECTORS:", guesses)
        if guesses:
            top = sorted(guesses, key=lambda x: -x[1])[0][0]
            print(f"TOP_SELECTOR: {top}")
            els = page.query_selector_all(top)[:6]
            for i, el in enumerate(els):
                print(f"--- ITEM {i+1} ---")
                try:
                    url_el = el.query_selector("a")
                    img_el = el.query_selector("img")
                    headings = el.query_selector_all("h1,h2,h3,h4,.title,.product-title,.name,.product-name")
                    name_text = headings[0].inner_text().strip() if headings else el.inner_text().strip()[:200]
                    href = url_el.get_attribute("href") if url_el else None
                    img = img_el.get_attribute("src") if img_el else None
                    print("NAME(excerpt):", short(name_text, 200))
                    print("HREF:", href)
                    print("IMG:", img)
                    inner = el.inner_html()
                    print("INNER_HTML_EXCERPT:", short(inner, 1200))
                except Exception as e:
                    print("Error extracting element:", e)
        else:
            print("No candidate item selectors found from heuristics.")
        print("PAGE_TITLE:", page.title())
        browser.close()

if __name__ == '__main__':
    cfg = load_cfg()
    url = cfg.get("url") or os.environ.get("TARGET_URL")
    if not url:
        print("No URL provided. Set TARGET_URL env or config.json url.")
        exit(2)
    print("Inspecting URL:", url)
    inspect(url)
