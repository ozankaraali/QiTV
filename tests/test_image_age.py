"""2.8: TMDB's images are fetched again once older than the details kept with them (TMDB's
terms allow six months at most); every other image stays as long as the cache has room."""

import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from services import image_manager
from services.image_manager import ImageManager

TMDB = "https://image.tmdb.org/t/p/w342/made-up.jpg"
OTHER = "https://posters.invalid/made-up.jpg"


class Body:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class Response:
    status = 200

    def __init__(self, body, length=True):
        self.body = body
        self.content = Body(body)
        self.content_length = len(body) if length else None  # a chunked answer says none

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Session:
    def __init__(self, body=b"fresh", length=True):
        self.asked = []
        self.body, self.length = body, length

    def get(self, url, timeout=None):
        self.asked.append(url)
        return Response(self.body, self.length)


class ImageAgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = ImageManager(SimpleNamespace(get_config_dir=lambda: directory.name))

    def fetch(self, session, url):
        return asyncio.run(self.manager.cache_image_from_url(session, url, False, 1))

    def age(self, path, seconds):
        then = time.time() - seconds
        os.utime(path, (then, then))

    def test_a_tmdb_image_older_than_its_details_is_fetched_again(self):
        session = Session()
        path = self.fetch(session, TMDB)
        self.fetch(session, TMDB)
        self.assertEqual(len(session.asked), 1)  # fresh: from the disk
        self.age(path, image_manager.TMDB_IMAGE_MAX_AGE_S + 60)
        self.assertEqual(self.fetch(session, TMDB), path)
        self.assertEqual(len(session.asked), 2)
        self.assertLess(time.time() - os.path.getmtime(path), 60)  # its age starts again

    def test_other_images_stay_however_old(self):
        session = Session()
        path = self.fetch(session, OTHER)
        self.age(path, 400 * 24 * 3600)
        self.fetch(session, OTHER)
        self.assertEqual(len(session.asked), 1)

    def test_an_image_larger_than_the_limit_is_never_kept(self):  # a list names any host
        for length in (True, False):  # declared, or found as it comes
            with self.subTest(length=length), mock.patch.object(image_manager, "LOGO_MAX", 4):
                self.assertIsNone(self.fetch(Session(b"a picture", length), OTHER))
        self.assertEqual(os.listdir(self.manager.cache_dir), [])
        self.assertIsNotNone(self.fetch(Session(b"logo"), OTHER))

    def test_only_a_web_address_is_fetched(self):
        session = Session()
        for url in ("file:///etc/hosts", "file://server/share/a.png", "smb://server/a.png", ""):
            with self.subTest(url=url):
                self.assertIsNone(self.fetch(session, url))
        self.assertEqual(session.asked, [])

    def test_tmdb_s_image_age_is_within_its_terms(self):
        self.assertLessEqual(image_manager.TMDB_IMAGE_MAX_AGE_S, 183 * 24 * 3600)


if __name__ == "__main__":
    unittest.main()
