#!/usr/bin/env python3
import sys
import argparse
import hashlib
import io
import asyncio
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse
from tld import get_tld
from PIL import Image
import aiohttp
from tqdm import tqdm
from tabulate import tabulate

# Configuration
DEFAULT_BATCH_SIZE = 20
DEFAULT_THREADS = 10
MAX_URL_LENGTH = 2200
BASE_URL = "https://favicon.yandex.net/favicon/"
CACHE_TTL_HOURS = 24

class FaviconAnalyzer:
    def __init__(self):
        self.results = {}
        self.seen_entries = set()
        self.cache = {}
        self.progress_bar = None
        self.session = None

    def _init_cache(self):
        self.cache = {}

    def _is_cache_valid(self, entry):
        return datetime.now() < entry['expires']

    async def _get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _cleanup(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def is_valid_domain(self, domain):
        try:
            return bool(get_tld(domain, fix_protocol=True))
        except:
            return False

    def generate_batches(self, domains, batch_size):
        batches = []
        current_batch = []
        current_length = len(BASE_URL)

        for domain in domains:  # Iteração única com filtro integrado
            if not self.is_valid_domain(domain):
                continue

            domain_len = len(domain)
            needed_length = current_length + domain_len + (1 if current_batch else 0)

            if needed_length > MAX_URL_LENGTH or len(current_batch) >= batch_size:
                if current_batch:
                    batches.append(current_batch)
                current_batch = []
                current_length = len(BASE_URL)

            current_batch.append(domain)
            current_length += domain_len + 1  # +1 para a barra

        if current_batch:
            batches.append(current_batch)
        return batches

    async def process_batch(self, batch, args):
        session = await self._get_session()
        url = f"{BASE_URL}{'/'.join(batch)}"
        
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    img_data = await response.read()
                    await self._process_image_data(img_data, batch, args)
        except Exception as e:
            print(f"Error processing batch: {str(e)}")

    async def _process_image_data(self, img_data, domains, args):
        with Image.open(io.BytesIO(img_data)) as img:
            img = img.convert('RGBA')
            w, h = img.size
            tile_height = h // len(domains)

            for idx, domain in enumerate(domains):
                cached = self.cache.get(domain)
                if cached and self._is_cache_valid(cached):
                    display_hash = cached['hash']
                else:
                    y1 = idx * tile_height
                    y2 = min(y1 + tile_height, h)
                    tile = img.crop((0, y1, w, y2))
                    display_hash = self._process_tile(domain, tile, args)
                    self.cache[domain] = {
                        'hash': display_hash,
                        'expires': datetime.now() + timedelta(hours=CACHE_TTL_HOURS)
                    }

                if display_hash:
                    self._update_results(domain, display_hash)

    def _process_tile(self, domain, tile, args):
        if self._is_white_square(tile):
            return "NULL" if args.show_white_hashes else None

        original_hash = hashlib.sha256(tile.tobytes()).hexdigest()[:8]

        if original_hash.startswith("5f70bf18"):
            return "NULL" if args.show_white_hashes else None

        return original_hash

    def _is_white_square(self, image):
        if image.mode != 'RGBA':
            image = image.convert('RGBA')
        return all(r == g == b == 255 and a == 255 for (r, g, b, a) in image.getdata())

    def _update_results(self, domain, display_hash):
        if not display_hash or (domain, display_hash) in self.seen_entries:
            return

        self.seen_entries.add((domain, display_hash))
        entry = self.results.setdefault(display_hash, {'count': 0, 'domains': []})
        entry['count'] += 1
        entry['domains'].append(domain)

    def format_results(self, output_format):
        sorted_results = sorted(self.results.items(), key=lambda x: x[1]['count'], reverse=True)
        
        if output_format == 'json':
            return json.dumps({
                hash_val: data for hash_val, data in sorted_results
            }, indent=2)
        
        if output_format == 'csv':
            csv_lines = ["Hash,Count,Domains"]
            for hash_val, data in sorted_results:
                domains = self._truncate_domains(data['domains'], 3)
                csv_lines.append(f'"{hash_val}",{data["count"]},"{domains}"')
            return '\n'.join(csv_lines)
        
        table = []
        for hash_val, data in sorted_results:
            domains = self._truncate_domains(data['domains'], 2)
            table.append([hash_val, data['count'], domains])
        
        return tabulate(table, headers=["Hash", "Count", "Domains"], tablefmt="github")

    def _truncate_domains(self, domains, max_items):
        truncated = ', '.join(domains[:max_items])
        if len(domains) > max_items:
            truncated += f", +{len(domains)-max_items} more"
        return truncated

async def main():
    parser = argparse.ArgumentParser(description="Advanced Favicon Analyzer", add_help=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-u', '--url')
    group.add_argument('-w', '--wordlist')
    parser.add_argument('-b', '--batch', type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument('-t', '--threads', type=int, default=DEFAULT_THREADS)
    parser.add_argument('-o', '--output-format', choices=['table', 'json', 'csv'], default='table')
    parser.add_argument('-dw', '--show-white-hashes', action='store_true')
    parser.add_argument('-h', '--help', action='help', help='Show help')
    
    args = parser.parse_args()
    analyzer = FaviconAnalyzer()
    analyzer._init_cache()

    try:
        if args.url:
            await analyzer.process_batch(args.url.split('/'), args)
        else:
            def domain_generator():
                with open(args.wordlist) as f:
                    for line in f:
                        domain = line.strip()
                        if analyzer.is_valid_domain(domain):
                            yield domain

            domains = domain_generator()
            batches = analyzer.generate_batches(domains, args.batch)

            semaphore = asyncio.Semaphore(args.threads)
            
            tasks = []
            for batch in batches:
                tasks.append(asyncio.create_task(
                    process_batch_with_semaphore(semaphore, analyzer, batch, args)
                ))

            with tqdm(total=len(tasks), desc="Processing batches") as pbar:
                for coro in asyncio.as_completed(tasks):
                    await coro
                    pbar.update(1)

        print("\n🔍 Analysis Results:")
        print(analyzer.format_results(args.output_format))

    finally:
        await analyzer._cleanup()

async def process_batch_with_semaphore(semaphore, analyzer, batch, args):
    async with semaphore:
        return await analyzer.process_batch(batch, args)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Operation cancelled by user")
