import argparse
from contextlib import closing, contextmanager, ExitStack
from enum import IntFlag
import itertools
from pathlib import Path
import re
from select import select
from socket import socket, SocketIO
from subprocess import list2cmdline
import sys
from typing import Callable, Dict, Iterator, List, Tuple

import docker
from docker.models.containers import Container
from docker.utils.socket import next_frame_header, read_exactly
import yaml

READ_TIMEOUT_SEC = 1.0
WRITE_TIMEOUT_SEC = 1.0


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
    if (
        sum(1 for _ in itertools.chain(*select([], [process], [], WRITE_TIMEOUT_SEC)))
        == 0
    ):
        raise TimeoutError

    cmdline = list2cmdline(cmd) + "\n"
    process.sendall(cmdline.encode())


def dropwhile_frame(process: socket, predicate: Callable[[Stream], bool]) -> int:
    while (
        sum(1 for _ in itertools.chain(*select([process], [], [], READ_TIMEOUT_SEC)))
        == 1
    ):
        stream, next_size = next_frame_header(process)

        if not predicate(stream):
            return next_size

        read_exactly(process, next_size)

    return 0


def takewhile_frame(
    process: socket, predicate: Callable[[Stream], bool]
) -> Iterator[int]:
    while (
        sum(1 for _ in itertools.chain(*select([process], [], [], READ_TIMEOUT_SEC)))
        == 1
    ):
        stream, next_size = next_frame_header(process)

        if not predicate(stream):
            break

        yield next_size


def yield_links(config: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    topology = yaml.safe_load(config.read_text())["topology"]

    for link in topology["links"]:
        endpoints = link["endpoints"]
        assert len(endpoints) == 2

        yield tuple(
            map(
                lambda components: dict([components]),
                map(lambda endpoint: tuple(endpoint.split(":")), endpoints),
            )
        )


def create_vtysh_processes(
    stack: ExitStack, enter_config: bool = True
) -> Dict[str, socket]:
    client = docker.from_env()
    vtysh_processes = {}

    for container in client.containers.list():
        proc = stack.enter_context(spawn_process(container, ["vtysh"]))

        # Can't open configuration file /etc/frr/vtysh.conf due to 'No such file or directory'.
        stdout_size = dropwhile_frame(proc, lambda stream: stream == Stream.STDERR)
        assert stdout_size > 0

        # Hello, this is FRRouting (version 8.4_git).
        read_exactly(proc, stdout_size)

        if enter_config:
            # Enter config mode
            send_command(proc, ["conf"])

        vtysh_processes[container.name] = proc

    return vtysh_processes


def setup_router_loopbacks(vtysh_processes: Dict[str, socket]) -> None:
    for node, proc in vtysh_processes.items():
        if re.match(r"^PC\d+$", node):
            continue

        node_id = int(re.match(r"^R(\d+)$", node).group(1))

        send_command(proc, ["int", "lo"])
        send_command(proc, ["ip", "addr", f"10.10.10.{node_id + 1}/32"])


def setup_router_subnets(config: Path, vtysh_processes: Dict[str, socket]) -> None:
    subnet_counter = 1

    for fst, snd in yield_links(config):
        assert len(fst) == 1
        assert len(snd) == 1

        fst_node, fst_iface = next(iter(fst.items()))
        snd_node, snd_iface = next(iter(snd.items()))

        if re.match(r"^PC\d+$", fst_node) or re.match(r"^PC\d+$", snd_node):
            continue

        assert subnet_counter < 256

        fst_proc = vtysh_processes[fst_node]
        snd_proc = vtysh_processes[snd_node]

        fst_node_id = int(re.match(r"^R(\d+)$", fst_node).group(1))
        snd_node_id = int(re.match(r"^R(\d+)$", snd_node).group(1))

        send_command(fst_proc, ["int", fst_iface])
        send_command(
            fst_proc, ["ip", "addr", f"192.168.{subnet_counter}.{fst_node_id + 1}/24"]
        )

        send_command(snd_proc, ["int", snd_iface])
        send_command(
            snd_proc, ["ip", "addr", f"192.168.{subnet_counter}.{snd_node_id + 1}/24"]
        )

        subnet_counter += 1


def setup_pc_links(config: Path, vtysh_processes: Dict[str, socket]) -> None:
    for fst, snd in yield_links(config):
        assert len(fst) == 1
        assert len(snd) == 1

        fst_node, fst_iface = next(iter(fst.items()))
        snd_node, snd_iface = next(iter(snd.items()))

        pc_node, pc_iface = None, None
        router_node, router_iface = None, None

        if re.match(r"^PC\d+$", fst_node):
            pc_node = fst_node
            pc_iface = fst_iface

        elif re.match(r"^R\d+$", fst_node):
            router_node = fst_node
            router_iface = fst_iface

        if re.match(r"^PC\d+$", snd_node):
            pc_node = snd_node
            pc_iface = snd_iface

        elif re.match(r"^R\d+$", snd_node):
            router_node = snd_node
            router_iface = snd_iface

        if pc_node is None or router_node is None:
            continue

        pc_proc = vtysh_processes[pc_node]
        router_proc = vtysh_processes[router_node]

        router_node_id = int(re.match(r"^R(\d+)$", router_node).group(1))

        send_command(router_proc, ["int", router_iface])
        send_command(
            router_proc,
            ["ip", "addr", f"172.25.{router_node_id + 1}.{router_node_id + 1}/24"],
        )

        send_command(pc_proc, ["int", pc_iface])
        send_command(
            pc_proc,
            ["ip", "addr", f"172.25.{router_node_id + 1}.{router_node_id + 2}/24"],
        )


def commit_configs(vtysh_processes: Dict[str, socket]) -> None:
    for _, proc in vtysh_processes.items():
        send_command(proc, ["do", "wr"])

        # Leave config-if mode
        send_command(proc, ["exit"])


def main(argv: argparse.Namespace) -> None:
    with ExitStack() as stack:
        vtysh_processes = create_vtysh_processes(stack)

        setup_router_loopbacks(vtysh_processes)
        setup_router_subnets(argv.topology, vtysh_processes)
        setup_pc_links(argv.topology, vtysh_processes)
        commit_configs(vtysh_processes)

        for node, proc in vtysh_processes.items():
            # Leave config mode
            send_command(proc, ["exit"])

            stderr_size = dropwhile_frame(proc, lambda stream: stream == Stream.STDOUT)
            assert stderr_size == 0

            send_command(proc, ["sh", "int", "brief"])

            # Leave main mode
            send_command(proc, ["exit"])

            print("---------")
            print(f"{node} configuration:")
            print("---------\n")

            skip_counter = 0

            for stdout_size in takewhile_frame(
                proc, lambda stream: stream == Stream.STDOUT
            ):
                data = read_exactly(proc, stdout_size).decode()

                if skip_counter < 1:
                    skip_counter += 1

                    continue

                print(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-t", "--topology", type=Path, required=True, help="path to topology.yml file"
    )

    main(parser.parse_args())
