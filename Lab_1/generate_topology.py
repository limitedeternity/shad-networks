import argparse
import re
import sys

import yaml


CONFIG_STATE = {
    "name": "static_routing",
    "prefix": "",
    "topology": {"nodes": {}, "links": []},
}


def nat_type(x: str, /) -> int:
    x = int(x)

    if x < 1:
        raise argparse.ArgumentTypeError("Natural number is expected")

    return x


def create_routers(amount: int) -> None:
    config = {
        "kind": "linux",
        "image": "quay.io/frrouting/frr:9.1.0",
        "binds": ["./.init/daemons:/etc/frr/daemons"],
    }

    for i in range(amount):
        CONFIG_STATE["topology"]["nodes"][f"R{i}"] = config


def create_devices(amount: int) -> None:
    config = {
        "kind": "linux",
        "image": "frrouting/frr-debian:latest",
        "binds": ["./.init/daemons:/etc/frr/daemons"],
    }

    for i in range(amount):
        CONFIG_STATE["topology"]["nodes"][f"PC{i}"] = config


def create_nodes(subnets: int) -> None:
    create_routers(subnets)
    create_devices(subnets)


def create_links(subnets: int) -> None:
    link_data = {node: [0] for node in CONFIG_STATE["topology"]["nodes"]}

    for node in link_data:
        if re.match(r"^PC\d+$", node):
            continue

        node_id = int(re.match(r"^R(\d+)$", node).group(1))
        next_node = f"R{(node_id + 1) % subnets}"

        node_adapter = max(link_data[node]) + 1
        next_node_adapter = max(link_data[next_node]) + 1

        link_data[node].append(node_adapter)
        link_data[next_node].append(next_node_adapter)

        CONFIG_STATE["topology"]["links"].append(
            {
                "endpoints": [
                    f"{node}:eth{node_adapter}",
                    f"{next_node}:eth{next_node_adapter}",
                ]
            }
        )

    for node in link_data:
        if re.match(r"^R\d+$", node):
            continue

        node_id = int(re.match(r"^PC(\d+)$", node).group(1))
        router_node = f"R{node_id % subnets}"

        node_adapter = max(link_data[node]) + 1
        router_node_adapter = max(link_data[router_node]) + 1

        link_data[node].append(node_adapter)
        link_data[router_node].append(router_node_adapter)

        CONFIG_STATE["topology"]["links"].append(
            {
                "endpoints": [
                    f"{node}:eth{node_adapter}",
                    f"{router_node}:eth{router_node_adapter}",
                ]
            }
        )


def populate_config(argv: argparse.Namespace) -> None:
    create_nodes(argv.subnets)
    create_links(argv.subnets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subnets", type=nat_type, default=3)

    populate_config(parser.parse_args())
    sys.stdout.write(yaml.safe_dump(CONFIG_STATE, indent=4))
