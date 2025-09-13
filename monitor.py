#!/usr/bin/env python3
"""
monitor.py

Popmart などのページを監視して、売り切れ->入荷 または 新着 を検出して Discord に通知します。
"""
import json
import os
import time
from datetime import datetime
from typing import Dict, Any, List, Optional
import logging

import requests
from bs4 import BeautifulSoup

# Playwright optional import
try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:
    HAVE_PLAYWRIGHT = False

LOG = logging.getLogger("popmart-monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_config(path: str = "config.json") -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any], path: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"items": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def send_discord_webhook(webhook_url: str, content: Optional[str] = None, embeds: Optional[List[dict]] = None):
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    headers = {"Content-Type": "application/json"}
    resp = requests.post(webhook_url, json=payload, headers=headers, timeout=15)
    try:
        resp.raise_for_status()
    except Exception as e:
        LOG.error("Failed to send webhook: %s %s", resp.status_code, resp.text)
        raise
    return resp


def fetch_with_requests(url: str, headers: Optional[dict] = None) -> str:
    h = headers or {
        "User-Agent": "Mozilla/5.0 (compatible; PopmartMonitor/1.0; +https://github.com/)"
    }
    r = requests.get(url, headers=h, timeout=30)
    r.raise_for_status()
    return r.text


def parse_with_bs4(html: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for el in soup.select(cfg["selectors"]["item_selector"]):
        name = None
        url = None
        image = None
        stock_text = None
        try:
            s = cfg["selectors"]
            if s.get("name_selector"):
                el_name = el.select_one(s["name_selector"])
                name = el_name.get_text(strip=True) if el_name else None
            if s.get("url_selector"):
                el_url = el.select_one(s["url_selector"])
                if el_url:
                    url = el_url.get("href") or el_url.get("data-href") or el_url.get("data-url")
            if s.get("image_selector"):
                el_img = el.select_one(s["image_selector"])
                if el_img:
                    image = el_img.get("src") or el_img.get("data-src")
            if s.get("stock_selector"):
                el_stock = el.select_one(s["stock_selector"])
                stock_text = el_stock.get_text(strip=True) if el_stock else None
        except Exception:
            LOG.exception("parse error for element")
        if not name and not url:
            continue
        items.append({
            "id": url or name,
            "name": name or "unknown",
            "url": url,
            "image": image,
            "stock_text": stock_text
        })
    return items


def parse_with_playwright(url: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not HAVE_PLAYWRIGHT:
        raise RuntimeError("Playwright is not available. Install with 'pip install playwright' and run 'playwright install'.")
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; PopmartMonitor/1.0; +https://github.com/)")
        LOG.info("Navigating to %s", url)
        page.goto(url, wait_until="networkidle", timeout=60000)
        extra_wait = cfg.get("wait_for_selector")
        if extra_wait:
            try:
                page.wait_for_selector(extra_wait, timeout=15000)
            except Exception:
                LOG.debug("wait_for_selector timeout or not found: %s", extra_wait)
        els = page.query_selector_all(cfg["selectors"]["item_selector"])
        LOG.info("Found %d elements with selector %s", len(els), cfg["selectors"]["item_selector"])
        for el in els:
            try:
                s = cfg["selectors"]
                name = None
                url_v = None
                image = None
                stock_text = None
                if s.get("name_selector"):
                    n = el.query_selector(s["name_selector"])
                    name = n.inner_text().strip() if n else None
                if s.get("url_selector"):
                    u = el.query_selector(s["url_selector"])
                    if u:
                        url_v = u.get_attribute("href") or u.get_attribute("data-href") or u.get_attribute("data-url")
                if s.get("image_selector"):
                    im = el.query_selector(s["image_selector"])
                    if im:
                        image = im.get_attribute("src") or im.get_attribute("data-src")
                if s.get("stock_selector"):
                    st = el.query_selector(s["stock_selector"])
                    stock_text = st.inner_text().strip() if st else None
                if not name and not url_v:
                    continue
                results.append({
                    "id": url_v or name,
                    "name": name or "unknown",
                    "url": url_v,
                    "image": image,
                    "stock_text": stock_text
                })
            except Exception:
                LOG.exception("error parsing element with playwright")
        browser.close()
    return results


def is_in_stock(item: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    text = (item.get("stock_text") or "").lower()
    sold_out_patterns = [p.lower() for p in cfg.get("sold_out_patterns", ["sold out", "売り切れ", "欠品"])]
    in_stock_patterns = [p.lower() for p in cfg.get("in_stock_patterns", ["add to cart", "カートに入れる", "在庫あり", "在庫"])]

    for p in in_stock_patterns:
        if p in text:
            return True
    for p in sold_out_patterns:
        if p in text:
            return False
    if text.strip() == "":
        return cfg.get("assume_in_stock_if_no_label", False)
    return False


def build_discord_embed(item: Dict[str, Any], cfg: Dict[str, Any], reason: str) -> dict:
    title = item.get("name") or "New item"
    url = item.get("url") or cfg.get("url")
    image = item.get("image")
    desc = f"{reason}"
    embed = {
        "title": title,
        "url": url,
        "description": desc,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fields": [
            {"name": "検出日時", "value": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "inline": True},
            {"name": "状態", "value": reason, "inline": True},
        ]
    }
    if image:
        embed["image"] = {"url": image}
    return embed


def main():
    cfg = load_config("config.json")
    state_path = cfg.get("state_file", "state.json")
    state = load_state(state_path)
    prev_items: Dict[str, Any] = state.get("items", {})

    try:
        if cfg.get("use_requests", False):
            LOG.info("Using requests + bs4 fetch")
            html = fetch_with_requests(cfg["url"])
            items = parse_with_bs4(html, cfg)
        else:
            LOG.info("Using Playwright fetch")
            items = parse_with_playwright(cfg["url"], cfg)
    except Exception as e:
        LOG.exception("Failed to fetch or parse the page: %s", e)
        return

    LOG.info("Parsed %d items", len(items))

    webhook = cfg.get("discord_webhook_url") or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        LOG.error("Discord webhook URL not configured. Set discord_webhook_url in config.json or DISCORD_WEBHOOK_URL env var.")
        return

    new_state_items = {}
    notifications = []
    for it in items:
        item_id = it.get("id") or it.get("name")
        in_stock = is_in_stock(it, cfg)
        prev = prev_items.get(item_id)
        prev_in_stock = prev.get("in_stock") if prev else None
        reason = None
        if prev:
            if prev_in_stock is False and in_stock:
                reason = "売り切れ → 入荷 (restock)"
        else:
            if in_stock and cfg.get("notify_new_in_stock", True):
                reason = "新着入荷 (new & in stock)"
            elif cfg.get("notify_new", False):
                reason = "新着 (new item)"

        new_state_items[item_id] = {
            "name": it.get("name"),
            "url": it.get("url"),
            "image": it.get("image"),
            "stock_text": it.get("stock_text"),
            "in_stock": in_stock,
            "last_seen": datetime.utcnow().isoformat() + "Z"
        }

        if reason:
            embed = build_discord_embed(it, cfg, reason)
            notifications.append((it, embed))

    state["items"] = new_state_items
    state["last_checked"] = datetime.utcnow().isoformat() + "Z"
    save_state(state, state_path)
    LOG.info("State saved to %s", state_path)

    for it, embed in notifications:
        try:
            content = cfg.get("mention_role")
            send_discord_webhook(webhook, content=content, embeds=[embed])
            LOG.info("Sent notification for %s", it.get("name"))
            time.sleep(1)
        except Exception:
            LOG.exception("Failed to send notification for %s", it.get("name"))

    LOG.info("Done. Notifications sent: %d", len(notifications))


if __name__ == "__main__":
    main()
