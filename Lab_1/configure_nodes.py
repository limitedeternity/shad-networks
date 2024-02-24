import argparse
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing, contextmanager, ExitStack
from dataclasses import astuple, dataclass
from enum import IntFlag
import ipaddress
import itertools
from pathlib import Path
import re
from select import select
from socket import socket, SocketIO
from subprocess import list2cmdline
from typing import Callable, Dict, Iterator, List, Tuple

import docker
from docker.models.containers import Container
from docker.utils.socket import next_frame_header, read_exactly
import yaml

READ_TIMEOUT_SEC = 1.0
WRITE_TIMEOUT_SEC = 1.0

NETWORK_MAP = defaultdict(dict)


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
    stack: ExitStack, *, enter_config: bool = False
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
        lo_iface = ipaddress.IPv4Interface(f"10.10.10.{node_id + 1}/32")

        send_command(proc, ["int", "lo"])
        send_command(proc, ["ip", "addr", lo_iface.with_prefixlen])

        NETWORK_MAP[node][lo_iface.network] = (lo_iface.ip, "lo")


def setup_router_subnets(config: Path, vtysh_processes: Dict[str, socket]) -> None:
    subnet_counter = 1

    for fst, snd in yield_links(config):
        assert len(fst) == 1
        assert len(snd) == 1

        fst_node, fst_adapter = next(iter(fst.items()))
        snd_node, snd_adapter = next(iter(snd.items()))

        if re.match(r"^PC\d+$", fst_node) or re.match(r"^PC\d+$", snd_node):
            continue

        assert subnet_counter < 256

        fst_node_id = int(re.match(r"^R(\d+)$", fst_node).group(1))
        snd_node_id = int(re.match(r"^R(\d+)$", snd_node).group(1))

        fst_node_iface = ipaddress.IPv4Interface(
            f"192.168.{subnet_counter}.{fst_node_id + 1}/24"
        )
        snd_node_iface = ipaddress.IPv4Interface(
            f"192.168.{subnet_counter}.{snd_node_id + 1}/24"
        )

        send_command(vtysh_processes[fst_node], ["int", fst_adapter])
        send_command(
            vtysh_processes[fst_node], ["ip", "addr", fst_node_iface.with_prefixlen]
        )

        send_command(vtysh_processes[snd_node], ["int", snd_adapter])
        send_command(
            vtysh_processes[snd_node], ["ip", "addr", snd_node_iface.with_prefixlen]
        )

        NETWORK_MAP[fst_node][fst_node_iface.network] = (fst_node_iface.ip, fst_adapter)
        NETWORK_MAP[snd_node][snd_node_iface.network] = (snd_node_iface.ip, snd_adapter)

        subnet_counter += 1


def setup_pc_links(config: Path, vtysh_processes: Dict[str, socket]) -> None:
    for fst, snd in yield_links(config):
        assert len(fst) == 1
        assert len(snd) == 1

        fst_node, fst_adapter = next(iter(fst.items()))
        snd_node, snd_adapter = next(iter(snd.items()))

        pc_node, pc_node_adapter = None, None
        router_node, router_node_adapter = None, None

        if re.match(r"^PC\d+$", fst_node):
            pc_node = fst_node
            pc_node_adapter = fst_adapter

        elif re.match(r"^R\d+$", fst_node):
            router_node = fst_node
            router_node_adapter = fst_adapter

        if re.match(r"^PC\d+$", snd_node):
            pc_node = snd_node
            pc_node_adapter = snd_adapter

        elif re.match(r"^R\d+$", snd_node):
            router_node = snd_node
            router_node_adapter = snd_adapter

        if pc_node is None or router_node is None:
            continue

        router_node_id = int(re.match(r"^R(\d+)$", router_node).group(1))

        pc_node_iface = ipaddress.IPv4Interface(
            f"172.25.{router_node_id + 1}.{router_node_id + 2}/24"
        )
        router_node_iface = ipaddress.IPv4Interface(
            f"172.25.{router_node_id + 1}.{router_node_id + 1}/24"
        )

        send_command(vtysh_processes[router_node], ["int", router_node_adapter])
        send_command(
            vtysh_processes[router_node],
            ["ip", "addr", router_node_iface.with_prefixlen],
        )

        send_command(vtysh_processes[pc_node], ["int", pc_node_adapter])
        send_command(
            vtysh_processes[pc_node], ["ip", "addr", pc_node_iface.with_prefixlen]
        )

        NETWORK_MAP[pc_node][pc_node_iface.network] = (
            pc_node_iface.ip,
            pc_node_adapter,
        )
        NETWORK_MAP[router_node][router_node_iface.network] = (
            router_node_iface.ip,
            router_node_adapter,
        )


def commit_configs(vtysh_processes: Dict[str, socket]) -> None:
    for _, proc in vtysh_processes.items():
        send_command(proc, ["do", "wr"])
        send_command(proc, ["exit"])


def show_command_output(vtysh_processes: Dict[str, socket], cmd: List[str]) -> None:
    for node, proc in vtysh_processes.items():
        stderr_size = dropwhile_frame(proc, lambda stream: stream == Stream.STDOUT)
        assert stderr_size == 0

        send_command(proc, cmd)

        print("\n---------")
        print(f"{node} [{list2cmdline(cmd)}]:")
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


@dataclass
class RouteLink:
    node: str
    adapter: str

    def __iter__(self):
        return iter(astuple(self))


def build_routing_graph(config: Path) -> Mapping[str, RouteLink]:
    routing_graph = defaultdict(list)

    for fst, snd in yield_links(config):
        assert len(fst) == 1
        assert len(snd) == 1

        fst_node, fst_adapter = next(iter(fst.items()))
        snd_node, snd_adapter = next(iter(snd.items()))

        routing_graph[fst_node].append(RouteLink(snd_node, fst_adapter))
        routing_graph[snd_node].append(RouteLink(fst_node, snd_adapter))

    for node in routing_graph:
        if re.match(r"^PC\d+$", node):
            continue

        routing_graph[node].append(RouteLink(node, "lo"))

    routing_graph.default_factory = None

    return routing_graph


def dijkstra(graph: Mapping[str, RouteLink], source: str, dest: str) -> List[str]:
    distances = {vertex: float("inf") for vertex in graph}
    prev_vertices = {vertex: None for vertex in graph}
    distances[source] = 0
    vertices = list(graph)

    while vertices:
        curr_vertex = min(vertices, key=lambda v: distances[v])

        if distances[curr_vertex] == float("inf"):
            break

        for connection in graph[curr_vertex]:
            neighbour, _ = connection

            alt_route = distances[curr_vertex] + 1
            curr_distance = distances[neighbour]

            if alt_route < curr_distance:
                distances[neighbour] = alt_route
                prev_vertices[neighbour] = curr_vertex

        vertices.remove(curr_vertex)

    path, curr_vertex = [], dest

    while prev_vertices[curr_vertex] is not None:
        path.insert(0, curr_vertex)
        curr_vertex = prev_vertices[curr_vertex]

    if path:
        path.insert(0, curr_vertex)

    return path


def setup_static_routing(config: Path, vtysh_processes: Dict[str, socket]) -> None:
    NETWORK_MAP.default_factory = None
    routing_graph = build_routing_graph(config)

    for from_node, to_node in itertools.product(routing_graph, repeat=2):
        if from_node == to_node:
            continue

        path = dijkstra(routing_graph, from_node, to_node)
        assert path

        target_subnet = next(iter(NETWORK_MAP[path[-1]]))
        knows_target_subnet = path[-1]

        for node in itertools.islice(reversed(path), 1, None):
            if target_subnet in NETWORK_MAP[node]:
                knows_target_subnet = node

                continue

            coverage_intersection = set(NETWORK_MAP[node]) & set(
                NETWORK_MAP[knows_target_subnet]
            )
            assert coverage_intersection

            where_is_he = next(iter(coverage_intersection))
            his_address, _ = NETWORK_MAP[knows_target_subnet][where_is_he]
            _, our_adapter = NETWORK_MAP[node][where_is_he]

            send_command(
                vtysh_processes[node],
                [
                    "ip",
                    "route",
                    format(target_subnet),
                    format(his_address),
                    our_adapter,
                ],
            )

            knows_target_subnet = node


def main(argv: argparse.Namespace) -> None:
    with ExitStack() as stack:
        vtysh_processes = create_vtysh_processes(stack, enter_config=True)

        setup_router_loopbacks(vtysh_processes)
        setup_router_subnets(argv.topology, vtysh_processes)
        setup_pc_links(argv.topology, vtysh_processes)
        commit_configs(vtysh_processes)

        show_command_output(vtysh_processes, ["do", "sh", "int", "brief"])

        setup_static_routing(argv.topology, vtysh_processes)
        commit_configs(vtysh_processes)

        show_command_output(vtysh_processes, ["sh", "ip", "route"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-t", "--topology", type=Path, required=True, help="path to topology.yml file"
    )

    main(parser.parse_args())
