import asyncio
from datetime import datetime, timezone
from io import BytesIO
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, features

import main


class ArtworkUrlTests(unittest.TestCase):
    def test_supported_image_url_is_used_directly(self):
        url = "https://cdn.example.com/episode.jpg"

        self.assertEqual(main.artworkUrl(url), url)

    @patch("main.cache.registerArtworkSource", return_value="a" * 64)
    def test_webp_image_uses_local_jpeg_url(self, register_artwork):
        url = "https://cdn.example.com/episode.webp"

        result = main.artworkUrl(url)

        register_artwork.assert_called_once_with(url)
        self.assertEqual(
            result,
            f"{main.PODIMO_PROTOCOL}://{main.PODIMO_HOSTNAME}/artwork/{'a' * 64}.jpg",
        )

    @patch("main.cache.registerArtworkSource", return_value="b" * 64)
    def test_extensionless_image_uses_local_jpeg_url(self, register_artwork):
        result = main.artworkUrl("https://cdn.example.com/image/123")

        self.assertTrue(result.endswith(f"/{'b' * 64}.jpg"))
        register_artwork.assert_called_once()

    def test_invalid_image_url_is_omitted(self):
        self.assertIsNone(main.artworkUrl("file:///etc/passwd"))
        self.assertIsNone(main.artworkUrl(None))


class ArtworkConversionTests(unittest.TestCase):
    @unittest.skipUnless(features.check("webp"), "Pillow has no WebP support")
    def test_webp_is_converted_to_rgb_jpeg(self):
        source = BytesIO()
        Image.new("RGBA", (32, 24), (255, 0, 0, 128)).save(source, format="WEBP")

        result = main.artworkToJpeg(source.getvalue())

        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (32, 24))


class ArtworkRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_artwork_returns_404(self):
        client = main.app.test_client()

        response = await client.get(f"/artwork/{'c' * 64}.jpg")

        self.assertEqual(response.status_code, 404)

    @patch("main.cache.getArtwork", return_value=b"jpeg-data")
    async def test_cached_artwork_is_served_as_jpeg(self, get_artwork):
        client = main.app.test_client()

        response = await client.get(f"/artwork/{'d' * 64}.jpg")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(await response.get_data(), b"jpeg-data")

    @patch("main.cache.insertArtwork")
    @patch("main.cache.getArtworkFailure", return_value=None)
    @patch("main.cache.getArtworkSource", return_value="https://cdn.example.com/image.webp")
    @patch("main.cache.getArtwork", return_value=None)
    @patch("main.fetchArtwork", new_callable=AsyncMock)
    async def test_uncached_artwork_is_converted_and_cached(
        self, fetch_artwork, get_artwork, get_source, get_failure, insert_artwork
    ):
        source = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(source, format="PNG")
        fetch_artwork.return_value = source.getvalue()
        client = main.app.test_client()

        response = await client.get(f"/artwork/{'e' * 64}.jpg")

        self.assertEqual(response.status_code, 200)
        result = await response.get_data()
        self.assertTrue(result.startswith(b"\xff\xd8"))
        insert_artwork.assert_called_once_with("e" * 64, result)


class PublicResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_connector_rejects_literal_private_address(self):
        connector = main.PublicConnector(resolver=main.PublicResolver())
        self.addAsyncCleanup(connector.close)

        with self.assertRaisesRegex(OSError, "non-public"):
            await connector._resolve_host("127.0.0.1", 80)

    async def test_private_address_is_rejected(self):
        resolver = main.PublicResolver()
        resolver.resolver.resolve = AsyncMock(
            return_value=[{"host": "127.0.0.1"}]
        )

        with self.assertRaisesRegex(OSError, "non-public"):
            await resolver.resolve("example.com", 443)

    async def test_public_address_is_returned(self):
        resolver = main.PublicResolver()
        addresses = [{"host": "93.184.216.34"}]
        resolver.resolver.resolve = AsyncMock(return_value=addresses)

        self.assertEqual(await resolver.resolve("example.com", 443), addresses)


class FeedArtworkTests(unittest.IsolatedAsyncioTestCase):
    @patch("main.urlHeadInfo", new_callable=AsyncMock, return_value=("123", "audio/mpeg"))
    @patch("main.cache.registerArtworkSource", return_value="f" * 64)
    async def test_webp_artwork_does_not_prevent_feed_generation(
        self, register_artwork, url_head_info
    ):
        data = {
            "podcast": {
                "title": "Test podcast",
                "description": "Description",
                "images": {"coverImageUrl": "https://cdn.example.com/show.webp"},
                "language": "nl-NL",
                "authorName": "Author",
            },
            "episodes": [
                {
                    "id": "episode-1",
                    "title": "Episode one",
                    "description": "Episode description",
                    "publishDatetime": datetime(2026, 7, 22, tzinfo=timezone.utc),
                    "imageUrl": "https://cdn.example.com/episode.webp",
                    "audio": {
                        "url": "https://cdn.example.com/episode.mp3",
                        "duration": 60,
                    },
                    "streamMedia": None,
                    "podcastName": "Test podcast",
                    "artist": "Author",
                }
            ],
        }

        feed = await main.podcastsToRss("podcast-id", data, "nl-NL")
        feed_text = feed.decode("utf-8")

        self.assertIn("Episode one", feed_text)
        self.assertIn(f"/artwork/{'f' * 64}.jpg", feed_text)
        self.assertNotIn(".webp", feed_text)
        url_head_info.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
