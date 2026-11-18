"""Helper processes that go wrong, for the visualizer's tests (not a test module): each is a
spawn target in the place of services.visualizer.helper.helper_main."""

import os


def die_at_once(conn, location, detailed_log=False):
    """A child that dies before a word (as a libVLC crash would): its pipe closes with it."""
    os._exit(3)


def fail_inside(conn, location, detailed_log=False):
    """The real helper, whose reader can't be made (as with no libVLC to load)."""
    from services.visualizer import helper

    class Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no libVLC here")

    helper.FileLevels = Broken
    helper.helper_main(conn, location, detailed_log)
