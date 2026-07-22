# Copyright 2022 Thijs Raymakers
#
# Licensed under the EUPL, Version 1.2 or – as soon they
# will be approved by the European Commission - subsequent
# versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the
# Licence.
# You may obtain a copy of the Licence at:
#
# https://joinup.ec.europa.eu/software/page/eupl
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the Licence is
# distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.
# See the Licence for the specific language governing
# permissions and limitations under the Licence.

import asyncio
from io import BytesIO
from ipaddress import ip_address
import re
import sys
import logging
from weakref import WeakValueDictionary
from os import getenv
from podimo.client import PodimoClient
from feedgen.feed import FeedGenerator
from mimetypes import guess_type
from aiohttp import ClientSession, CookieJar, ClientTimeout, TCPConnector
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from quart import Quart, Response, render_template, request
from hashlib import sha256
from hypercorn.config import Config
from hypercorn.asyncio import serve
from urllib.parse import quote
from urllib.parse import urlsplit
from podimo.config import *
from podimo.utils import generateHeaders, randomHexId
import podimo.cache as cache
import cloudscraper
import traceback
from PIL import Image

MAX_ARTWORK_SIZE = 10 * 1024 * 1024
MAX_ARTWORK_DIMENSION = 3000
MAX_ARTWORK_PIXELS = 20_000_000
artwork_downloads = asyncio.Semaphore(2)
artwork_locks = WeakValueDictionary()


def ensurePublicAddresses(addresses):
    if any(not ip_address(address["host"]).is_global for address in addresses):
        raise OSError("Artwork host resolves to a non-public address")
    return addresses


class PublicResolver(AbstractResolver):
    def __init__(self):
        self.resolver = DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        addresses = await self.resolver.resolve(host, port, family)
        return ensurePublicAddresses(addresses)

    async def close(self):
        await self.resolver.close()


class PublicConnector(TCPConnector):
    async def _resolve_host(self, host, port, traces=None):
        addresses = await super()._resolve_host(host, port, traces)
        return ensurePublicAddresses(addresses)

# Setup Quart, used for serving the web pages
app = Quart(__name__)
proxies = dict()

#Setup logging
logging.basicConfig(
    format="%(levelname)s | %(asctime)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
)

def example():
    return f"""Example
------------
Username: example@example.com
Password: this-is-my-password
Podcast ID: 12345-abcdef

The URL will be
https://example%40example.com:this-is-my-password@{PODIMO_HOSTNAME}/feed/12345-abcdef.xml

Note that the username and password should be URL encoded. This can be done with
a tool like https://gchq.github.io/CyberChef/#recipe=URL_Encode(true)
"""

@app.after_request
def allow_cors(response):
    response.headers.set('Access-Control-Allow-Origin', '*')
    response.headers.set('Access-Control-Allow-Methods', 'GET, POST')
    response.headers.set('Cache-Control', 'max-age=900')
    logging.debug(f"Incoming {request.method} request for '{request.url}' from User-Agent {request.user_agent} at {request.remote_addr}.")
    return response

def authenticate():
    return Response(
        f"""401 Unauthorized.
You need to login with the correct credentials for Podimo.

{example()}""",
        401,
        {
            "Content-Type": "text/plain",
            "WWW-Authenticate": "Basic realm='Podimo credentials'"
        },
    )

def initialize_client(username: str, password: str, region: str, locale: str) -> PodimoClient:
    client = PodimoClient(username, password, region, locale)

    # Check if there is an authentication token already in memory. If so, use that one.
    # If it is expired, request a new token.
    key = client.key
    client.token = cache.getCacheEntry(key, cache.TOKENS)

    # Check if we previously created a cookie jar
    if key not in cache.cookie_jars:
        cache.cookie_jars[key] = CookieJar()
    client.cookie_jar = cache.cookie_jars[key]
    return client

async def check_auth(username, password, region, locale, scraper):
    try:
        client = initialize_client(username, password, region, locale)
        if client.token:
            return client

        await client.podimoLogin(scraper)
        cache.insertIntoTokenCache(client.key, client.token)
        return client

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        if DEBUG:
            traceback.print_exc()
    return None

podcast_id_pattern = re.compile(r"[0-9a-fA-F\-]+")

@app.route("/", methods=["POST", "GET"])
async def index():
    error = ""
    if request.method == "POST":
        form = await request.form
        email = form.get("email")
        password = form.get("password")
        podcast_id = form.get("podcast_id")
        region = form.get("region")
        locale = form.get("locale")

        if not LOCAL_CREDENTIALS:
            if email is None or email == "":
                error += "Email is required"
            if password is None or password == "":
                error += "Password is required"
        if podcast_id is None or podcast_id == "":
            error += "Podcast ID is required"
        elif podcast_id_pattern.fullmatch(podcast_id) is None:
            error += "Podcast ID is not valid"
        if region is None or region == "":
            error += "Region is required"
        elif region not in [region_code for (region_code, _) in REGIONS]:
            error += "Region is not valid"
        if locale is None or locale == "":
            error += "Locale is required"
        elif locale not in LOCALES:
            error += "Locale is not valid"

        if error == "":
            podcast_id = quote(str(podcast_id), safe="")
            region = quote(str(region), safe="")
            locale = quote(str(locale), safe="")
            
            if LOCAL_CREDENTIALS:
                url = f"{PODIMO_PROTOCOL}://{PODIMO_HOSTNAME}/feed/{podcast_id}.xml?{randomHexId(10)}&region={region}&locale={locale}"
            else:
                email = quote(str(email), safe="")
                comma = quote(',', safe="")
                username = f"{email}{comma}{region}{comma}{locale}"
                password = quote(str(password), safe="")             
                url = f"{PODIMO_PROTOCOL}://{username}:{password}@{PODIMO_HOSTNAME}/feed/{podcast_id}.xml?{randomHexId(10)}&region={region}&locale={locale}"
            
            logging.debug(f"Created an URL: {url}.")
            return await render_template("feed_location.html", url=url)

    return await render_template("index.html", error=error, locales=LOCALES, regions=REGIONS, need_credentials=not(LOCAL_CREDENTIALS))


@app.errorhandler(404)
async def not_found(error):
    return Response(
        f"404 Not found.\n\n{example()}", 404, {"Content-Type": "text/plain"}
    )


@app.route("/feed/<string:podcast_id>.xml")
async def serve_basic_auth_feed(podcast_id):
    if LOCAL_CREDENTIALS:
        args = request.args
        region = args.get("region")
        locale = args.get("locale")
        return await serve_feed(PODIMO_EMAIL, PODIMO_PASSWORD, podcast_id, region, locale)
    else:
        auth = request.authorization
        if not auth:
            return authenticate()
        else:
            username, region, locale = split_username_region_locale(auth.username)
            return await serve_feed(username, auth.password, podcast_id, region, locale)


def artworkUrl(image_url):
    if not image_url:
        return None

    parsed_url = urlsplit(image_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        logging.warning("Skipping invalid artwork URL")
        return None

    if image_url.endswith((".jpg", ".png")):
        return image_url

    key = cache.registerArtworkSource(image_url)
    return f"{PODIMO_PROTOCOL}://{PODIMO_HOSTNAME}/artwork/{key}.jpg"


def artworkToJpeg(data):
    with Image.open(BytesIO(data)) as source:
        if source.width * source.height > MAX_ARTWORK_PIXELS:
            raise ValueError("Artwork exceeds maximum pixel count")
        source.load()
        source.thumbnail(
            (MAX_ARTWORK_DIMENSION, MAX_ARTWORK_DIMENSION), Image.Resampling.LANCZOS
        )

        if source.mode in ("RGBA", "LA") or "transparency" in source.info:
            rgba = source.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = source.convert("RGB")

        output = BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
        return output.getvalue()


async def fetchArtwork(image_url):
    timeout = ClientTimeout(total=20)
    connector = PublicConnector(resolver=PublicResolver())
    async with ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(
            image_url, allow_redirects=True, max_redirects=5
        ) as response:
            response.raise_for_status()
            if response.content_length and response.content_length > MAX_ARTWORK_SIZE:
                raise ValueError("Artwork exceeds maximum download size")

            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_ARTWORK_SIZE:
                    raise ValueError("Artwork exceeds maximum download size")
            return bytes(data)


@app.route("/artwork/<string:key>.jpg")
async def serve_artwork(key):
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        return Response("Artwork not found", 404, {})

    artwork = await asyncio.to_thread(cache.getArtwork, key)
    if artwork is None and await asyncio.to_thread(cache.getArtworkFailure, key):
        return Response("Something went wrong while fetching artwork", 502, {})

    if artwork is None:
        lock = artwork_locks.setdefault(key, asyncio.Lock())
        async with lock:
            artwork = await asyncio.to_thread(cache.getArtwork, key)
            if artwork is None:
                if await asyncio.to_thread(cache.getArtworkFailure, key):
                    return Response("Something went wrong while fetching artwork", 502, {})
                source_url = await asyncio.to_thread(cache.getArtworkSource, key)
                if source_url is None:
                    return Response("Artwork not found", 404, {})

                try:
                    async with artwork_downloads:
                        source = await fetchArtwork(source_url)
                        artwork = await asyncio.to_thread(artworkToJpeg, source)
                    await asyncio.to_thread(cache.insertArtwork, key, artwork)
                except Exception as error:
                    await asyncio.to_thread(cache.insertArtworkFailure, key)
                    logging.error(f"Error while fetching artwork {key}: {error}")
                    return Response("Something went wrong while fetching artwork", 502, {})

    return Response(artwork, mimetype="image/jpeg")


def split_username_region_locale(string):
    s = string.split(',')
    if len(s) == 3:
        return tuple(s)
    else:
        return (s[0], 'nl', 'nl-NL')


def token_key(username, password):
    key = sha256(
        b"~".join([username.encode("utf-8"), password.encode("utf-8")])
    ).hexdigest()
    return key


@app.route("/feed/<string:username>/<string:password>/<string:podcast_id>.xml")
async def serve_feed(username, password, podcast_id, region, locale):
    
    logging.debug(f"Feed request for podcast {podcast_id} from IP {request.remote_addr} with User-Agent:{request.user_agent}.")
    
    # Check if it is a valid podcast id string
    if podcast_id_pattern.fullmatch(podcast_id) is None:
        return Response("Invalid podcast id format", 400, {})
   
    if region not in [region_code for (region_code, _) in REGIONS]:
        return Response("Invalid region", 400, {})
    if locale not in LOCALES:
        return Response("Invalid locale", 400, {})

    # Check if url contains unique ID or podcastID in blocked list. If so, return HTTP code 410 GONE
    if any(item in request.url for item in BLOCKED):
        logging.debug(f"Blocked! Podcast {podcast_id} is on local block list")
        return Response("Podcast is gone", 410, {}) 
    
    with cloudscraper.create_scraper() as scraper:
        scraper.proxies = proxies
        client = await check_auth(username, password, region, locale, scraper)
        if not client:
            return authenticate()

        # Get a list of valid podcasts
        try:
            podcasts = await podcastsToRss(
                podcast_id, await client.getPodcasts(podcast_id, scraper), locale
            )
        except Exception as e:
            exception = str(e)
            if "Podcast not found" in exception:
                return Response(
                    "Podcast not found. Are you sure you have the correct ID?", 404, {}
                )
            logging.error(f"Error while fetching podcasts: {exception}")
            return Response("Something went wrong while fetching the podcasts", 500, {})
        return Response(podcasts, mimetype="text/xml")


async def urlHeadInfo(session, id, url, locale):
    url = normalize_audio_url(url)
    cache_key = sha256(url.encode("utf-8")).hexdigest()
    entry = cache.getHeadEntry(cache_key)
    if entry:
        return entry

    retries = 3  # Number of retries
    timeout = ClientTimeout(total=10)  # 10 seconds timeout for each try

    for attempt in range(retries):
        try:
            logging.debug(f"HEAD request to {url} (Attempt {attempt + 1})")
            async with session.head(url, allow_redirects=True,
                                    headers=generateHeaders(None, locale),
                                    timeout=timeout) as response:
                response.raise_for_status()
                content_length = response.headers.get('content-length', '0')
                content_type = response.headers.get('content-type')
                if content_type:
                    content_type = content_type.split(';', 1)[0]
                else:
                    content_type = guess_type(url)[0] or 'audio/mpeg'
                cache.insertIntoHeadCache(cache_key, content_length, content_type)
                return (content_length, content_type)

        except asyncio.TimeoutError:
            if attempt < retries - 1:
                logging.info(f"Retrying HEAD request to {url} (Attempt {attempt + 2})")
                await asyncio.sleep(1)  # Wait for 1 second before retrying
            else:
                logging.error(f"All retries failed for HEAD request to {url}")
                raise  # Re-raise the last exception if all retries fail



def normalize_audio_url(url):
    parsed_url = urlsplit(url)
    signed_audio_hosts = {
        "media-cdn-episodes.podimo.com",
        "media-cdn-video-episodes.podimo.com",
    }
    audio_path = re.fullmatch(
        r"/audios/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.mp3",
        parsed_url.path,
        re.IGNORECASE,
    )
    if parsed_url.hostname in signed_audio_hosts and audio_path:
        return f"https://cdn.podimo.com/audios/{audio_path.group(1)}.mp3"

    if "hls-media" in url and "/main.m3u8" in url:
        url = url.replace("hls-media", "audios")
        url = url.replace("/main.m3u8", ".mp3")
    return url


def extract_audio_url(episode):
    audio = episode.get("audio") or {}
    stream_media = episode.get("streamMedia") or {}
    duration = audio.get("duration") or stream_media.get("duration") or 0

    candidates = [
        episode.get("audioUrl"),
        audio.get("url"),
        stream_media.get("url"),
    ]
    fallback = None

    for url in candidates:
        if not url:
            continue
        url = normalize_audio_url(url)
        if ".m3u8" not in url.split("?", 1)[0].lower():
            return url, duration
        if fallback is None:
            fallback = url

    return fallback, duration


async def addFeedEntry(fg, episode, session, locale):
    fe = fg.add_entry()
    fe.guid(episode["id"])
    fe.title(episode["title"])
    fe.description(episode["description"])
    fe.pubDate(episode.get("publishDatetime", episode.get("datetime")))
    image = artworkUrl(episode.get("imageUrl"))
    if image:
        fe.podcast.itunes_image(image)

    url, duration = extract_audio_url(episode)
    if url is None:
        return 
    original_url = url
    url = normalize_audio_url(url)
    if url != original_url:
        logging.debug(f"Normalized audio URL for episode {episode['id']} to {url}")
    logging.debug(f"Found podcast '{episode['title']}'")
    fe.podcast.itunes_duration(duration)
    content_length, content_type = await urlHeadInfo(session, episode['id'], url, locale)
    fe.enclosure(url, content_length, content_type)

def chunks(x, n):
    for i in range(0, len(x), n):
        yield x[i:i + n]

async def podcastsToRss(podcast_id, data, locale):
    fg = FeedGenerator()
    fg.load_extension("podcast")

    podcast = data["podcast"]
    episodes = data["episodes"]

    if len(episodes) > 0:
        last_episode = episodes[0]
        title = podcast["title"]
        if podcast["title"] is None:
            title = last_episode["podcastName"]
        fg.title(title)

        if podcast["description"]:
            fg.description(podcast["description"])
        else:
            fg.description(title)

        fg.link(href=f"https://podimo.com/shows/{podcast_id}", rel="alternate")

        image = podcast["images"]["coverImageUrl"]
        if image is None:
            image = last_episode['imageUrl']
        image = artworkUrl(image)
        if image:
            fg.image(image)

        language = podcast["language"]
        if language is None:
            language = locale
        fg.language(language)

        artist = podcast["authorName"]
        if artist is None:
            artist = last_episode["artist"]
        fg.podcast.itunes_author(artist)

        if not PUBLIC_FEEDS:
            fg.podcast.itunes_block(True)

    async with ClientSession() as session:
        for chunk in chunks(episodes, 5):
            await asyncio.gather(
                *[addFeedEntry(fg, episode, session, locale) for episode in chunk]
            )

    feed = fg.rss_str(pretty=True)
    return feed


async def spawn_web_server():
    config = Config()
    config.bind = [PODIMO_BIND_HOST]
    config.read_timeout = 60
    config.graceful_timeout = 5
    config.backlog = 1000
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    await serve(app, config)

async def main():
    if HTTP_PROXY:
        global proxies
        logging.info(f"Running with https proxy defined in environmental variable HTTP_PROXY: {HTTP_PROXY}")
        proxies['https'] = HTTP_PROXY
    tasks = [spawn_web_server()]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    if DEBUG:
        logging.info(f"""Spawning server on {PODIMO_BIND_HOST}
Configuration: 
- DEBUG: {DEBUG}
- LOCAL CREDENTIALS: {LOCAL_CREDENTIALS} ({PODIMO_EMAIL})
- PODIMO_HOSTNAME: {PODIMO_HOSTNAME}
- PODIMO_BIND_HOST: {PODIMO_BIND_HOST}
- PODIMO_PROTOCOL: {PODIMO_PROTOCOL}
- PUBLIC_FEEDS: {PUBLIC_FEEDS}
- HTTP_PROXY: {HTTP_PROXY}
- ZENROWS_API: {ZENROWS_API}
- SCRAPER_API: {SCRAPER_API}
- CACHE_DIR: {CACHE_DIR}
- STORE_TOKENS_ON_DISK: {STORE_TOKENS_ON_DISK}
- TOKEN_CACHE_TIME: {TOKEN_CACHE_TIME} sec
- PODCAST_CACHE_TIME: {PODCAST_CACHE_TIME} sec
- HEAD_CACHE_TIME: {HEAD_CACHE_TIME} sec
- BLOCKING: {BLOCKED}
""")
    asyncio.run(main())
