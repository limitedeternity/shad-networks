import argparse
from contextlib import closing, contextmanager
from enum import IntFlag
from pathlib import Path
from socket import socket, SocketIO
from subprocess import list2cmdline
from typing import Callable, Iterator, List

import docker
from docker.models.containers import Container
from docker.utils.socket import next_frame_header, read_exactly
import yaml


class Stream(IntFlag):
    STDOUT = 1
    STDERR = 2


@contextmanager
def spawn_process(container: Container, cmd: List[str]) -> Iterator[socket]:
    socketio: SocketIO
    _, socketio = container.exec_run(cmd, stdin=True, socket=True)

    # https://github.com/docker/docker-py/issues/2255
    with closing(socketio), closing(socket := socketio._sock):
        yield socket


def send_command(process: socket, cmd: List[str]) -> None:
    cmdline = list2cmdline(cmd) + "\n"
    process.sendall(cmdline.encode())


def dropwhile_frame(process: socket, predicate: Callable[[Stream], bool]) -> int:
    while True:
        stream, next_size = next_frame_header(process)

        if not predicate(stream):
            return next_size

        read_exactly(process, next_size)


def take_frame(process: socket, predicate: Callable[[Stream], bool]) -> int:
    stream, next_size = next_frame_header(process)
    assert predicate(stream)

    return next_size


def main(argv: argparse.Namespace) -> None:
    client = docker.from_env()

    topology = yaml.safe_load(argv.topology.read_text())["topology"]
    containers = {container.name: container for container in client.containers.list()}

    for node in topology["nodes"]:
        with spawn_process(containers[node], ["vtysh"]) as proc:
            # Can't open configuration file /etc/frr/vtysh.conf due to 'No such file or directory'.
            stdout_size = dropwhile_frame(proc, lambda stream: stream == Stream.STDERR)

            data = read_exactly(proc, stdout_size)
            print(data.decode())

            send_command(proc, ["exit"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-t", "--topology", type=Path, required=True, help="path to topology.yml file"
    )

    main(parser.parse_args())