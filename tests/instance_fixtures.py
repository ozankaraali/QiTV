"""A stand-in for the first QiTV, run in a process of its own by the single-instance tests and
main.py's: `python first.py <server name> <mode>` (the script is FIRST, written to a file).

It listens, says "ready", and once asked to come to the front says "activated" and ends
("answer"). With "hang" it listens and never answers. With "files" it prints
`opened <path as JSON>` for each file a start handed over, and ends. With "slow" it listens
and starts answering only 4 s later, as "files" (a QiTV still starting: no event loop yet).
With "old" it is QiTV 2.10's server: it answers "shown" to "show" and hangs up at once.
"""

FIRST = r"""
import json, sys, time
from PySide6.QtCore import QCoreApplication, QTimer
app = QCoreApplication([])
mode = sys.argv[2]
if mode == "old":
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    read = {}
    def on_read(socket):
        data = read.get(id(socket), b"") + bytes(socket.readAll().data())
        if b"\n" not in data:
            read[id(socket)] = data[-64:]
            return
        read.pop(id(socket), None)
        if data.startswith(b"show\n"):
            print("activated", flush=True)
            QTimer.singleShot(300, app.quit)
            socket.write(b"shown\n")
            socket.flush()
        socket.disconnectFromServer()
    def on_connection():
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: on_read(s))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                on_read(socket)
    server.newConnection.connect(on_connection)
    assert server.listen(sys.argv[1])
else:
    from services.single_instance import InstanceServer
    server = InstanceServer(sys.argv[1])
    assert server.listen()
print("ready", flush=True)
if mode == "hang":
    time.sleep(8)
    sys.exit(0)
if mode == "slow":
    time.sleep(4)  # still starting: a start that connected meanwhile waits in the backlog
    mode = "files"
if mode == "files":
    def on_opened(paths):
        for path in paths:
            print("opened " + json.dumps(path), flush=True)
        QTimer.singleShot(200, app.quit)
    server.opened.connect(on_opened)
elif mode == "answer":
    server.activated.connect(lambda: print("activated", flush=True))
    server.activated.connect(lambda: QTimer.singleShot(200, app.quit))
QTimer.singleShot(8000, app.quit)
app.exec()
"""
