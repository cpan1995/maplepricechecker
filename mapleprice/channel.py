"""Your channel, from the game's own connection: every channel is a separate
server port (ch 3 = 8542, ch 4 = 8543, ...). Only asks Windows which
connections the game has open; the game itself is never touched."""
import logging

import psutil

log = logging.getLogger("mapleprice")

GAME_EXE = "MapleStory.exe"
PORT_BASE = 8539   # channel = port - PORT_BASE
MAX_CHANNEL = 40   # anything past this is another server (chat on 8913, login, ...)


def channel_from_port(port):
    ch = port - PORT_BASE
    return ch if 1 <= ch <= MAX_CHANNEL else None


def channel_from_ports(ports):
    """The one channel among the game's remote ports; None when there's none
    (changing channel, cash shop) or it's ambiguous."""
    found = {channel_from_port(p) for p in ports} - {None}
    return found.pop() if len(found) == 1 else None


def read_channel():
    try:
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] == GAME_EXE:
                return channel_from_ports(c.raddr.port for c in proc.net_connections(kind="tcp")
                                          if c.raddr and c.status == psutil.CONN_ESTABLISHED)
    except psutil.Error:  # e.g. access denied: we're not admin and the game is
        log.exception("couldn't read the game's connections")
    return None
