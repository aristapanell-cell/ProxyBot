import requests
import re
import json
import hashlib
import time
import os
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Set, Tuple, Optional
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1002325683219"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

CHANNELS = [
    "https://t.me/s/hddify",
    "https://t.me/s/best_internet_iran",
    "https://t.me/s/times_config",
    "https://t.me/s/ProxyAnonymous",
    "https://t.me/s/JavidanNet",
    "https://t.me/s/ShadowProxy66",
    "https://t.me/s/BestProxyTel1",
    "https://t.me/s/proxyir01",
    "https://t.me/s/proxymtprotoir",
    "https://t.me/s/iRoProxy",
    "https://t.me/s/IPCF_Proxy",
    "https://t.me/s/proxy_bolt",
    "https://t.me/s/proxyskyy",
    "https://t.me/s/ProxySkull"
]

IPV4 = r'(?:25[0-5]|2[0-4]\d|1?\d?\d)'

PROXY_PATTERNS = [
    rf'(mtproto://[^\s<>"\'()]+)',
    rf'(https?://t\.me/proxy\?[^\s<>"\'()]+)',
    rf'(https?://t\.me/socks\?[^\s<>"\'()]+)',
    rf'(tg://proxy\?[^\s<>"\'()]+)',
    rf'(tg://socks\?[^\s<>"\'()]+)',
    rf'(socks5://[^\s<>"\'()]+)',
    rf'((?:{IPV4}\.){{3}}{IPV4}:\d{{1,5}}:[a-fA-F0-9]+)',
    rf'((?:{IPV4}\.){{3}}{IPV4}:\d{{1,5}}(?:[:][^:\s]+[:][^:\s]+)?)'
]

AD_KEYWORDS = [
    'join', 'channel', 'عضویت', 'کانال', 'ادمین', 'خرید', 'فروش', 'تبلیغ',
    'instagram.com', 'اینستاگرام', 'آموزش', 'tutorial', 'support',
    'telegram.me/join', 't.me/join', 'click', 'لینک عضویت'
]

MAX_PROXIES_PER_POST = 20
MAX_MESSAGES_PER_CHANNEL = 2
KEEP_HOURS = 168
DB_PATH = "sent_proxies.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sent_proxies (
            proxy_hash TEXT PRIMARY KEY,
            proxy TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS dead_cache (
            url TEXT PRIMARY KEY,
            failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_PATH}")

def clean_old_proxies():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=KEEP_HOURS)
    c.execute("DELETE FROM sent_proxies WHERE sent_at < ?", (cutoff,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info(f"Cleaned {deleted} old proxies.")

def get_sent_proxy_hashes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT proxy_hash FROM sent_proxies")
    rows = c.fetchall()
    conn.close()
    sent_count = len(rows)
    logger.info(f"Loaded {sent_count} previously sent proxies from database")
    return {row[0] for row in rows}

def mark_as_sent(proxy):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    proxy_hash = hashlib.md5(proxy.encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO sent_proxies (proxy_hash, proxy, sent_at) VALUES (?, ?, ?)",
              (proxy_hash, proxy, datetime.now()))
    conn.commit()
    conn.close()
    logger.info(f"Marked proxy as sent: {proxy[:50]}...")

def mark_as_sent_batch(proxies):
    if not proxies:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now()
    data = [(hashlib.md5(p.encode()).hexdigest(), p, now) for p in proxies]
    c.executemany("INSERT OR IGNORE INTO sent_proxies (proxy_hash, proxy, sent_at) VALUES (?, ?, ?)", data)
    conn.commit()
    conn.close()
    logger.info(f"Marked {len(proxies)} proxies as sent.")

def get_dead_cache():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT url FROM dead_cache")
    rows = c.fetchall()
    conn.close()
    return {row[0] for row in rows}

def add_to_dead_cache(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO dead_cache (url, failed_at) VALUES (?, ?)",
              (url, datetime.now()))
    conn.commit()
    conn.close()

def remove_from_dead_cache(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM dead_cache WHERE url = ?", (url,))
    conn.commit()
    conn.close()

def clean_dead_cache():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=24)
    c.execute("DELETE FROM dead_cache WHERE failed_at < ?", (cutoff,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted:
        logger.info(f"Cleaned {deleted} old dead cache entries.")

class MTProtoSocksExtractor:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
            'Accept-Language': 'en-US,en;q=0.5',
        })
        self.sent_hashes = get_sent_proxy_hashes()
        self.dead_cache = get_dead_cache()
        self.failed_counter = {}

    def should_skip_channel(self, url: str) -> bool:
        if url in self.dead_cache:
            return True
        return False

    def update_dead_cache(self, url: str):
        self.failed_counter[url] = self.failed_counter.get(url, 0) + 1
        if self.failed_counter[url] >= 3:
            add_to_dead_cache(url)
            self.dead_cache.add(url)

    def is_proxy_already_sent(self, proxy: str) -> bool:
        proxy_hash = hashlib.md5(proxy.encode()).hexdigest()
        if proxy_hash in self.sent_hashes:
            return True
        return False

    def has_ad_keywords(self, text: str) -> bool:
        t = text.lower()
        for k in AD_KEYWORDS:
            if k in t:
                return True
        return False

    def extract_from_text(self, text: str) -> List[str]:
        out = []
        for p in PROXY_PATTERNS:
            out += re.findall(p, text, re.IGNORECASE)
        return list(set(out))

    def extract_proxy_buttons(self, soup) -> List[str]:
        proxies = []
        buttons = soup.find_all("a", href=True)
        for btn in buttons:
            href = btn.get("href", "").strip()
            if not href:
                continue
            href_lower = href.lower()
            if "joinchat" in href_lower:
                continue
            if "/+" in href:
                continue
            if (
                href.startswith("tg://proxy?")
                or href.startswith("tg://socks?")
                or href.startswith("https://t.me/proxy?")
                or href.startswith("https://t.me/socks?")
                or href.startswith("mtproto://")
                or href.startswith("socks5://")
            ):
                proxies.append(self.normalize_proxy(href))
        return list(set(proxies))

    def normalize_proxy(self, proxy: str) -> str:
        proxy = proxy.strip()
        if proxy.startswith('https://t.me/proxy?'):
            proxy = proxy.replace('https://t.me/proxy?', 'tg://proxy?')
        elif proxy.startswith('https://t.me/socks?'):
            proxy = proxy.replace('https://t.me/socks?', 'tg://socks?')
        if re.match(r'^\d{1,3}(\.\d{1,3}){3}:\d+:[a-fA-F0-9]+$', proxy):
            a, b, c = proxy.split(':')
            proxy = f"tg://proxy?server={a}&port={b}&secret={c}"
        elif re.match(r'^\d{1,3}(\.\d{1,3}){3}:\d+$', proxy):
            a, b = proxy.split(':')
            proxy = f"socks5://{a}:{b}"
        elif re.match(r'^\d{1,3}(\.\d{1,3}){3}:\d+:[^:]+:[^:]+$', proxy):
            a, b, c, d = proxy.split(':')
            proxy = f"socks5://{c}:{d}@{a}:{b}"
        return proxy

    def fetch_page(self, url: str) -> Optional[str]:
        try:
            telegram_url = url.replace('t.me', 'telegram.me')
            r = self.session.get(telegram_url, timeout=20)
            return r.text
        except:
            return None

    def extract_proxies_from_channel(self, url: str) -> List[str]:
        if self.should_skip_channel(url):
            return []
        html = self.fetch_page(url)
        if not html:
            self.update_dead_cache(url)
            return []
        soup = BeautifulSoup(html, 'html.parser')
        message_texts = soup.find_all('div', class_='tgme_widget_message_text')[:MAX_MESSAGES_PER_CHANNEL]
        result = []
        for msg in message_texts:
            text = msg.get_text()
            if self.has_ad_keywords(text):
                continue
            found = self.extract_from_text(text)
            for f in found:
                n = self.normalize_proxy(f)
                if not self.is_proxy_already_sent(n):
                    result.append(n)
            parent = msg.find_parent('div', class_='tgme_widget_message_wrap')
            if parent:
                buttons = parent.find_all('a', href=True)
                for btn in buttons:
                    href = btn.get('href', '').strip()
                    if not href:
                        continue
                    href_lower = href.lower()
                    if "joinchat" in href_lower or "/+" in href_lower:
                        continue
                    if (href.startswith("tg://proxy?") or 
                        href.startswith("tg://socks?") or 
                        href.startswith("https://t.me/proxy?") or 
                        href.startswith("https://t.me/socks?") or 
                        href.startswith("mtproto://") or 
                        href.startswith("socks5://")):
                        n = self.normalize_proxy(href)
                        if not self.is_proxy_already_sent(n):
                            result.append(n)
        self.failed_counter[url] = 0
        remove_from_dead_cache(url)
        self.dead_cache.discard(url)
        return list(set(result))

    def collect_all_proxies(self) -> List[Tuple[str, str]]:
        allp = []
        seen = set()
        for c in CHANNELS:
            ps = self.extract_proxies_from_channel(c)
            for p in ps:
                if p not in seen:
                    seen.add(p)
                    t = "MTProto" if "proxy" in p or "mtproto" in p.lower() else "SOCKS5"
                    allp.append((p, t))
        return allp


class TelegramSender:
    def __init__(self, token: str, chat_id: int):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def send_message(self, text: str, reply_markup=None) -> bool:
        try:
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            r = requests.post(self.api + "/sendMessage", data=data, timeout=30)
            return r.status_code == 200
        except:
            return False

    def create_proxy_keyboard(self, proxies: List[Tuple[str, str]]) -> dict:
        kb = []
        row = []
        for i, (p, t) in enumerate(proxies):
            if t == "MTProto":
                label = "MTProto"
            else:
                label = "SOCKS5"
            row.append({
                "text": label,
                "url": p
            })
            if len(row) == 4:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        return {"inline_keyboard": kb}

    def create_caption(self, proxies: List[Tuple[str, str]]) -> str:
        return """ 🅿🆁🅾🆇🆈

🛜 پروکسی‌های جدید.
✅ برای اتصال به پروکسی‌ها از دکمه‌های زیر استفاده کنید.
<blockquote>👈 <a href="https://t.me/aristapanel/46625">دسترسی به جدیدترین کانفیگ‌ها</a></blockquote>
➖➖➖➖➖➖➖➖
<blockquote>@aristapanel</blockquote>
➖➖➖➖➖➖➖➖
#Arista #پروکسی #proxy #MTProto #SOCKS5"""

    def send_proxies_batch(self, proxies: List[Tuple[str, str]]) -> bool:
        if not proxies:
            return False
        return self.send_message(self.create_caption(proxies), self.create_proxy_keyboard(proxies))


class ProxyScheduler:
    def __init__(self):
        init_db()
        clean_old_proxies()
        clean_dead_cache()
        self.ext = MTProtoSocksExtractor()
        self.sender = TelegramSender(BOT_TOKEN, CHANNEL_ID)

    async def run_once(self):
        proxies = self.ext.collect_all_proxies()
        if proxies:
            sent_in_run = []
            for i in range(0, len(proxies), MAX_PROXIES_PER_POST):
                batch = proxies[i:i + MAX_PROXIES_PER_POST]
                if self.sender.send_proxies_batch(batch):
                    for p, _ in batch:
                        sent_in_run.append(p)
                await asyncio.sleep(1)
            if sent_in_run:
                mark_as_sent_batch(sent_in_run)
                for p in sent_in_run:
                    self.ext.sent_hashes.add(hashlib.md5(p.encode()).hexdigest())


def main():
    asyncio.run(ProxyScheduler().run_once())


if __name__ == "__main__":
    main()
