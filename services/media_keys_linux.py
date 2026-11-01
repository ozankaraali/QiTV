"""Linux's media keys: QiTV as an MPRIS player on the session bus (Qt's D-Bus module). GNOME,
KDE and the other desktops send the keyboard's media keys to the MPRIS player playing, and show
it in their media panel. See services.media_keys.

MPRIS: https://specifications.freedesktop.org/mpris-spec/latest/ (org.mpris.MediaPlayer2 and
org.mpris.MediaPlayer2.Player at /org/mpris/MediaPlayer2).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from PySide6.QtDBus import (
    QDBusConnection,
    QDBusError,
    QDBusMessage,
    QDBusObjectPath,
    QDBusVariant,
    QDBusVirtualObject,
)

from services.media_keys import on_command

logger = logging.getLogger(__name__)

PATH = "/org/mpris/MediaPlayer2"
SERVICE = "org.mpris.MediaPlayer2.qitv"
ROOT = "org.mpris.MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
PROPERTIES = "org.freedesktop.DBus.Properties"
_TRACK = "/org/qitv/now"  # the one "track": what the large picture plays

# Player methods and the command each is.
_METHODS = {
    "PlayPause": "toggle",
    "Play": "play",
    "Pause": "pause",
    "Stop": "stop",
    "Next": "next",
    "Previous": "previous",
}
_STATUS = {"playing": "Playing", "paused": "Paused", "": "Stopped"}

_INTROSPECTION = """
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg name="Offset" type="x" direction="in"/></method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="property_name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="properties" type="a{sv}" direction="out"/>
    </method>
    <method name="Set">
      <arg name="interface_name" type="s" direction="in"/>
      <arg name="property_name" type="s" direction="in"/>
      <arg name="value" type="v" direction="in"/>
    </method>
    <signal name="PropertiesChanged">
      <arg name="interface_name" type="s"/>
      <arg name="changed_properties" type="a{sv}"/>
      <arg name="invalidated_properties" type="as"/>
    </signal>
  </interface>
"""


def _reply(message: QDBusMessage, *values: Any) -> QDBusMessage:
    """The reply with `values` as its arguments. (PySide's createReply(list) sends the list as
    one argument, an array of variants: "av" for a "v" or an "a{sv}".)"""
    reply = message.createReply()
    reply.setArguments(list(values))
    return reply


class _Player(QDBusVirtualObject):
    """The object at /org/mpris/MediaPlayer2: every call to it comes here (on the GUI thread)."""

    def __init__(self, owner: "Mpris") -> None:
        super().__init__()
        self._owner = owner

    def introspect(self, path: str) -> str:
        return _INTROSPECTION

    def handleMessage(self, message: QDBusMessage, connection: QDBusConnection) -> bool:
        try:
            return self._owner.handle(message, connection)
        except Exception as e:  # a desktop's call must never take QiTV down
            logger.warning("MPRIS: %s.%s failed: %s", message.interface(), message.member(), e)
            connection.send(message.createErrorReply(QDBusError.ErrorType.Failed, str(e)))
            return True


class Mpris:
    """QiTV on the session bus as an MPRIS player."""

    def __init__(self, connection: Optional[QDBusConnection] = None) -> None:
        self._bus = connection if connection is not None else QDBusConnection.sessionBus()
        if not self._bus.isConnected():
            raise RuntimeError("no session bus")
        self._player = _Player(self)
        self._service = ""
        self._state, self._title, self._live = "", "", False
        self._asked: Any = None

    # ---- the system's side -------------------------------------------------------------

    def listen(self) -> None:
        if not self._bus.registerVirtualObject(PATH, self._player):
            raise RuntimeError(f"couldn't register {PATH}: {self._bus.lastError().message()}")
        # A second QiTV (another settings folder) is a second player, as MPRIS names them.
        for name in (SERVICE, f"{SERVICE}.instance{os.getpid()}"):
            if self._bus.registerService(name):
                self._service = name
                return
        self._bus.unregisterObject(PATH)
        raise RuntimeError(f"couldn't take {SERVICE}: {self._bus.lastError().message()}")

    def now_playing(self, state: str, title: str, live: bool) -> None:
        self._state, self._title, self._live = state, title, live
        changed = {
            "PlaybackStatus": _STATUS[state],
            "Metadata": self._metadata(),
            "CanPlay": bool(state),
            "CanPause": bool(state),
            "CanGoNext": bool(state),
            "CanGoPrevious": bool(state),
        }
        signal = QDBusMessage.createSignal(PATH, PROPERTIES, "PropertiesChanged")
        # The third argument must be a string array ("as"): Qt's Python binding makes one of a
        # list of strings only, an empty list would go as variants ("av"), a signal desktops
        # drop. "Position" is no property here (live TV; Qt's binding can't send it as the
        # 64-bit integer MPRIS asks for): a reader that kept one forgets it.
        signal.setArguments([PLAYER, changed, ["Position"]])
        self._bus.send(signal)

    def close(self) -> None:
        if self._service:
            self._bus.unregisterService(self._service)
            self._service = ""
        self._bus.unregisterObject(PATH)

    def report(self) -> dict:
        return {"state": self._state, "title": self._title, "service": self._service}

    def simulate(self, name: str) -> bool:
        """The method a desktop calls for `name`, through the bus on a connection of its own
        (the packaged smoke test's): it arrives as a desktop's call does."""
        member = next((m for m, n in _METHODS.items() if n == name), None)
        if member is None or not self._service:
            return False
        desktop = QDBusConnection.connectToBus(QDBusConnection.BusType.SessionBus, "qitv-desktop")
        call = QDBusMessage.createMethodCall(self._service, PATH, PLAYER, member)
        self._asked = desktop.asyncCall(call)  # kept till answered
        return True

    # ---- calls -------------------------------------------------------------------------

    def handle(self, message: QDBusMessage, connection: QDBusConnection) -> bool:
        interface, member = message.interface(), message.member()
        arguments = message.arguments()
        if interface == PROPERTIES:
            return self._properties(message, connection, member, arguments)
        if interface in (PLAYER, "") and member in _METHODS:
            on_command(_METHODS[member])
            connection.send(message.createReply())
            return True
        if interface in (ROOT, "") and member == "Raise":
            on_command("raise")
            connection.send(message.createReply())
            return True
        if interface in (PLAYER, ROOT, "") and member in ("Seek", "SetPosition", "OpenUri", "Quit"):
            connection.send(message.createReply())  # not QiTV's to do from here
            return True
        return False

    def _properties(
        self, message: QDBusMessage, connection: QDBusConnection, member: str, arguments: list
    ) -> bool:
        if member == "GetAll" and arguments:
            connection.send(_reply(message, self._all(str(arguments[0]))))
            return True
        if member == "Get" and len(arguments) >= 2:
            values = self._all(str(arguments[0]))
            name = str(arguments[1])
            if name not in values:
                connection.send(
                    message.createErrorReply(QDBusError.ErrorType.InvalidArgs, f"no {name}")
                )
            else:
                connection.send(_reply(message, QDBusVariant(values[name])))
            return True
        if member == "Set":
            connection.send(
                message.createErrorReply(QDBusError.ErrorType.PropertyReadOnly, "read only")
            )
            return True
        return False

    def _all(self, interface: str) -> dict[str, Any]:
        if interface == ROOT:
            return {
                "CanQuit": False,
                "CanRaise": True,
                "HasTrackList": False,
                "Identity": "QiTV",
                "DesktopEntry": "qitv",
                "SupportedUriSchemes": ["http", "https"],
                "SupportedMimeTypes": ["video/mp2t"],
            }
        if interface == PLAYER:
            playing = bool(self._state)
            return {
                "PlaybackStatus": _STATUS[self._state],
                "Rate": 1.0,
                "Metadata": self._metadata(),
                "Volume": 1.0,
                "MinimumRate": 1.0,
                "MaximumRate": 1.0,
                "CanGoNext": playing,
                "CanGoPrevious": playing,
                "CanPlay": playing,
                "CanPause": playing,
                "CanSeek": False,
                "CanControl": True,
            }
        return {}

    def _metadata(self) -> dict[str, Any]:
        if not self._state:
            return {"mpris:trackid": QDBusObjectPath("/org/mpris/MediaPlayer2/TrackList/NoTrack")}
        return {"mpris:trackid": QDBusObjectPath(_TRACK), "xesam:title": self._title or "QiTV"}
